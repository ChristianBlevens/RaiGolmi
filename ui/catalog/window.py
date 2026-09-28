"""The catalog window: Garry's Mod's mod list, for faces, bodies and toolbelts.

A window of its own, centred on the screen. Opening it closes the selector, as opening any
host surface closes the others (`ui/surfaces.py`). Hovering off it does not close it: a
click outside it, the × at its top right, or Escape does.

It is resident like the selector: started hidden, opened and closed through its socket. The
surface covers the whole output so that a click beside the panel lands on it, and it is
unmapped while closed, so at rest it takes no input at all. What it shows is decided in
`model.py`; this file draws it and sends the buttons to the daemon, never on the GTK thread.

A document opens in the editor, which takes the panel's place until Save, Cancel or Close; it
lives outside what `draw` rebuilds, so the two-second reload never touches an edit. A save
refused because the document moved on (`StaleDocument`) shows the newer text and puts the user's
edit on the clipboard, so nothing they typed is lost. Closing the window keeps the editor as it is.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ui import surfaces, theme  # noqa: E402
from ui import copyable  # noqa: E402
from ui.catalog.model import (DOCUMENTS, SECTIONS, STATES, THOUGHTS, CatalogModel,  # noqa: E402
                              buttons, documents, note, sections)
from ui.layershell import LayerShellUnavailable, overlay, set_keyboard  # noqa: E402

logger = logging.getLogger(__name__)

NAMESPACE = "raigolmi-catalog"
REFRESH_SECONDS = 2

CSS = """
window { background-color: rgba(0, 0, 0, $catalog_backdrop); color: $text; }
.panel { background-color: $bg; border: 1px solid $border; border-radius: 10px; padding: 14px; }
.title { font-weight: bold; color: $accent; font-size: 1.3em; }
.close { background: none; color: $muted; font-size: 1.4em; padding: 0 10px; }
.close:hover { background-color: $raised; color: $text; }
.heading { background: none; border: none; box-shadow: none; padding: 2px 4px; }
.heading label { font-weight: bold; color: $accent; font-size: 1.1em; }
.card { background-color: $surface; border: 1px solid $border; border-radius: 8px; padding: 8px; }
.thumb { background-color: $raised; border-radius: 6px; }
.name { font-weight: bold; }
.author, .detail, .state { color: $muted; font-size: 0.85em; }
.installed { color: $ok; }
.problem { color: $bad; font-size: 0.85em; }
.act { background-color: $raised; color: $text; padding: 2px 8px; }
.act:hover { background-color: $accent; color: $bg; }
.act:disabled { background-color: $surface; color: $dim; }
.confirm { background-color: $surface; border: 1px solid $accent_bg; border-radius: 8px;
           padding: 10px; }
.group { color: $muted; font-weight: bold; margin-top: 6px; }
.doc { background: none; border: none; box-shadow: none; padding: 2px 8px; }
.doc:hover { background-color: $raised; }
.absent { color: $dim; }
.editor { background-color: $surface; border: 1px solid $border; border-radius: 8px; }
.editor text { background-color: $surface; color: $text; font-family: monospace; }
"""


def _label(text: str, css: str, lines: int = 1) -> Gtk.Label:
    label = copyable.label(text, xalign=0)
    label.add_css_class(css)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if lines > 1:
        label.set_wrap(True)
        label.set_lines(lines)
    # A label asks for its longest line however it wraps; this makes it take the width given.
    label.set_max_width_chars(1)
    label.set_hexpand(True)
    return label


def _outcome(message: str, request: tuple[str, dict], answer: dict) -> str:
    """What happened: a first Delete takes an installed layer's images and keeps it."""
    if request[0] == "catalog_delete" and answer.get("state") == "downloaded":
        return (f"{message.rsplit(':', 1)[0]}: its images are deleted and it is kept; Delete "
                "again removes it")
    return message


class CatalogWindow:
    def __init__(self, model: CatalogModel, shown: bool) -> None:
        self.model = model
        self.start_open = shown
        self.failure = ""
        self.window: Gtk.Window | None = None
        self.panel: Gtk.Box | None = None
        self.search: Gtk.SearchEntry | None = None
        self.server: Gtk.Switch | None = None
        self.status: Gtk.Label | None = None
        self.confirm: Gtk.Box | None = None
        self.body: Gtk.Box | None = None
        self.stack: Gtk.Stack | None = None
        self.editor: Gtk.TextView | None = None
        self.editor_title: Gtk.Label | None = None
        self.editor_buttons: Gtk.Box | None = None
        self.editing: dict | None = None
        self.headings: dict[str, Gtk.Label] = {}
        self.doc_boxes: dict[str, Gtk.Box] = {}
        self.grids: dict[str, Gtk.FlowBox] = {}
        self.collapsed: set[str] = set()
        self.thumbs: dict[tuple[str, str], str | None] = {}
        self.fetching = False
        self.fetch_again = False
        self.pending: str | None = None
        self.said: tuple[str, bool] = ("", False)
        self.drawn: object = None
        self.opened = False

    # --- the frame -------------------------------------------------------------------
    def build(self, app: Gtk.Application) -> None:
        window = Gtk.ApplicationWindow(application=app)
        try:
            overlay(window, namespace=NAMESPACE, anchors=("left", "right", "top", "bottom"))
        except LayerShellUnavailable as exc:
            self.failure = str(exc)
            logger.error("%s", exc)
            app.quit()
            return
        self.window = window
        theme.apply(CSS)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        panel.add_css_class("panel")
        self.panel = panel

        top = Gtk.Box(spacing=12)
        title = copyable.label("Catalog", "title", xalign=0)
        top.append(title)
        self.search = Gtk.SearchEntry(placeholder_text="Search layers and documents",
                                      hexpand=True)
        self.search.connect("search-changed", lambda *_: self.draw())
        top.append(self.search)
        server_label = copyable.label("Server", "detail")
        top.append(server_label)
        self.server = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.server.connect("notify::active", lambda *_: self.refresh())
        top.append(self.server)
        close = copyable.button("×", self.close, valign=Gtk.Align.CENTER)
        close.add_css_class("close")
        top.append(close)
        panel.append(top)

        self.status = _label("", "detail", lines=2)
        panel.append(self.status)
        self.confirm = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.confirm.add_css_class("confirm")
        panel.append(self.confirm)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        docs = ((DOCUMENTS, "Documents"), (THOUGHTS, "Thoughts"))
        for kind, heading in (*SECTIONS, *docs):
            layer = kind not in (DOCUMENTS, THOUGHTS)
            if layer:
                child = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                    column_spacing=10, row_spacing=10, min_children_per_line=1,
                                    max_children_per_line=12, valign=Gtk.Align.START)
                self.grids[kind] = child
            else:
                self.collapsed.add(kind)
                child = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                self.doc_boxes[kind] = child
            section, self.headings[kind] = copyable.section(
                heading, child, expanded=layer,
                toggled=lambda open_, kind=kind: self._on_expanded(kind, open_))
            section.add_css_class("section")
            self.body.append(section)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scrolled.set_child(self.body)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(scrolled, "browse")
        self.stack.add_named(self._build_editor(), "edit")
        panel.append(self.stack)

        window.set_child(panel)

        # A click that lands beside the panel closes it; one on the panel is the panel's.
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        window.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        window.add_controller(keys)

        surfaces.serve(surfaces.CATALOG, self._answer)
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)
        if self.start_open:
            self._show()

    def _answer(self, verb: str) -> str:
        """On the socket's thread; the move itself on GTK's."""
        done = threading.Event()
        result: list[str] = []

        def move() -> bool:
            if verb == "open" or (verb == "toggle" and not self.opened):
                self._show()
            elif verb in ("close", "toggle"):
                self.close()
            result.append("open" if self.opened else "closed")
            done.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(move)
        if not done.wait(surfaces.ANSWER_SECONDS):
            raise surfaces.SurfaceError("the catalog's main loop did not answer")
        return result[0]

    def _show(self) -> None:
        assert self.window is not None and self.panel is not None
        surfaces.close_others(surfaces.CATALOG)
        monitors = self.window.get_display().get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        if monitor is not None:
            geometry = monitor.get_geometry()
            # Read as it opens: the guest follows the launcher's window.
            self.panel.set_size_request(int(geometry.width * theme.look().catalog_width_share),
                                        int(geometry.height * theme.look().catalog_height_share))
        self.window.set_visible(True)
        self.window.present()
        set_keyboard(self.window, True)
        self.opened = True
        self.refresh()
        assert self.search is not None
        self.search.grab_focus()

    def close(self) -> None:
        """Unmapped, never destroyed: closing the only window ends the resident process."""
        if self.window is None or not self.opened:
            return
        set_keyboard(self.window, False)
        self.window.set_visible(False)
        self.opened = False
        self._hide_confirm()

    def _on_click(self, _gesture, _n: int, x: float, y: float) -> None:
        assert self.window is not None and self.panel is not None
        picked = self.window.pick(x, y, Gtk.PickFlags.DEFAULT)
        if picked is None or not (picked is self.panel or picked.is_ancestor(self.panel)):
            self.close()

    def _on_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        if Gdk.keyval_name(keyval) != "Escape":
            return False
        if self.editing is None:
            self.close()
        elif self.editor is not None and self.editor.get_buffer().get_modified():
            self._say("Save or Cancel the edit first", True)
        else:
            self._leave_editor()
        return True

    def _on_expanded(self, kind: str, expanded: bool) -> None:
        if expanded:
            self.collapsed.discard(kind)
        else:
            self.collapsed.add(kind)
        self.drawn = None
        self.draw()

    # --- state -----------------------------------------------------------------------
    def _tick(self) -> bool:
        if self.opened:
            self.refresh()
        return GLib.SOURCE_CONTINUE

    def refresh(self) -> None:
        """On a worker: the daemon can be slow for reasons that are not the window's."""
        if self.fetching:
            self.fetch_again = True
            return
        self.fetching = True
        server = bool(self.server and self.server.get_active())
        threading.Thread(target=self._fetch, args=(server,), name="refresh", daemon=True).start()

    def _fetch(self, server: bool) -> None:
        try:
            fetched, error = self.model.fetch(server), None
        except Exception as exc:                       # noqa: BLE001
            fetched, error = None, exc
        GLib.idle_add(self._fetched, fetched, error)

    def _fetched(self, fetched, error: Exception | None) -> bool:
        self.fetching = False
        if error is not None:
            self._say(f"raigolmid: {error}", True)
        else:
            self.model.apply(fetched)
            self.draw()
        if self.fetch_again:
            self.fetch_again = False
            self.refresh()
        return GLib.SOURCE_REMOVE

    def _say(self, text: str, problem: bool) -> None:
        self.said = (text, problem)
        self._draw_status()

    def _draw_status(self) -> None:
        assert self.status is not None
        text, problem = self.said
        if self.pending is not None:
            text, problem = f"{self.pending}…", False
        elif not text and self.model.server_error and self.server and self.server.get_active():
            text, problem = f"the server: {self.model.server_error}", True
        copyable.set_text(self.status, text)
        self.status.set_visible(bool(text))
        (self.status.add_css_class if problem else self.status.remove_css_class)("problem")

    # --- drawing ---------------------------------------------------------------------
    def draw(self) -> None:
        if self.body is None:
            return
        self._draw_status()
        query = self.search.get_text() if self.search else ""
        server = bool(self.server and self.server.get_active())
        shown = sections(self.model.entries, query, server, self.collapsed)
        docs = documents(self.model.documents, query, self.collapsed)
        signature = (repr(shown), repr(docs), tuple(sorted(self.collapsed)),
                     tuple(sorted(self.thumbs.items())))
        if signature == self.drawn:
            return
        self.drawn = signature
        for kind, heading in SECTIONS:
            grid = self.grids[kind]
            while (child := grid.get_first_child()) is not None:
                grid.remove(child)
            for entry in shown[kind]:
                grid.append(self._card(entry))
            count = len(shown[kind])
            copyable.set_text(self.headings[kind],
                              heading if kind in self.collapsed else f"{heading}  ({count})")
        for kind in (DOCUMENTS, THOUGHTS):
            self._draw_documents(kind, docs[kind])

    def _card(self, entry: dict) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("card")
        card.set_size_request(theme.look().card_width, theme.look().card_width)

        thumb = self._thumbnail(entry)
        card.append(thumb)
        card.append(_label(entry["name"], "name"))
        author = entry.get("author") or ("you" if entry.get("authored") else None)
        if author:
            card.append(_label(f"by {author}", "author"))
        card.append(_label(entry.get("description") or "", "detail", lines=2))

        state = STATES[entry["state"]]
        if entry.get("downloads") is not None:
            state += f"  ·  {entry['downloads']} downloads"
        state_label = _label(state, "state")
        if entry["state"] == "installed":
            state_label.add_css_class("installed")
        card.append(state_label)
        said = note(entry)
        if said is not None:
            card.append(_label(said[0], "problem" if said[1] else "detail", lines=2))

        actions = Gtk.Box(spacing=6, valign=Gtk.Align.END, vexpand=True)
        for action in buttons(entry):
            button = copyable.button(action.capitalize(),
                                     lambda a=action, e=entry: self._act(a, e))
            button.add_css_class("act")
            if action == "delete" and entry.get("in_use"):
                button.set_sensitive(False)
            actions.append(button)
        card.append(actions)
        return card

    def _thumbnail(self, entry: dict) -> Gtk.Widget:
        """The entry's picture if it has one, else a blank square."""
        path = entry.get("thumbnail")
        key = (entry["kind"], entry["id"])
        if path is None and entry["state"] == "server":
            if key not in self.thumbs:
                self.thumbs[key] = None
                threading.Thread(target=self._fetch_thumb, args=key, daemon=True).start()
            path = self.thumbs.get(key)
        if path is None:
            blank = Gtk.Box()
            blank.add_css_class("thumb")
            blank.set_size_request(-1, theme.look().card_width // 2)
            return blank
        picture = Gtk.Picture.new_for_filename(path)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(-1, theme.look().card_width // 2)
        picture.add_css_class("thumb")
        return picture

    def _fetch_thumb(self, kind: str, layer_id: str) -> None:
        try:
            path = self.model.call("catalog_thumbnail", kind=kind, id=layer_id)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("the thumbnail of %s %s could not be fetched: %s", kind, layer_id, exc)
            return
        if path is not None:
            GLib.idle_add(self._thumb_arrived, (kind, layer_id), path)

    def _thumb_arrived(self, key, path: str) -> bool:
        self.thumbs[key] = path
        self.draw()
        return GLib.SOURCE_REMOVE

    def _draw_documents(self, kind: str, groups: list) -> None:
        box = self.doc_boxes[kind]
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        for group, docs in groups:
            if kind == DOCUMENTS:
                box.append(_label(group, "group"))
            for doc in docs:
                text = doc["title"] + ("" if doc["exists"] else "  — not written yet")
                label = _label(text, "detail" if doc["exists"] else "absent")
                button = copyable.button(label, lambda id=doc["id"]: self._open_document(id))
                button.add_css_class("doc")
                box.append(button)

    # --- the editor ------------------------------------------------------------------
    def _build_editor(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.editor_title = _label("", "name")
        page.append(self.editor_title)
        self.editor = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                   left_margin=8, right_margin=8, top_margin=6,
                                   bottom_margin=6)
        self.editor.add_css_class("editor")
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scrolled.set_child(self.editor)
        page.append(scrolled)
        self.editor_buttons = Gtk.Box(spacing=8)
        page.append(self.editor_buttons)
        return page

    def _open_document(self, id: str) -> None:
        self._call_then(("document", {"id": id}), self._show_document)

    def _show_document(self, doc: dict) -> None:
        assert self.editor is not None and self.editor_title is not None
        assert self.editor_buttons is not None and self.stack is not None
        self.editing = doc
        self._say("", False)            # what the last action said is not about this one
        where = doc["path"] or "generated by the machine"
        self.editor_title.set_text(f"{doc['title']}   ({where})")
        buffer = self.editor.get_buffer()
        buffer.set_text(doc["text"])
        buffer.set_modified(False)
        self.editor.set_editable(doc["editable"])
        self.editor.set_cursor_visible(doc["editable"])
        while (child := self.editor_buttons.get_first_child()) is not None:
            self.editor_buttons.remove(child)
        actions = (("Save", self._save), ("Cancel", self._leave_editor)) if doc["editable"] \
            else (("Close", self._leave_editor),)
        for name, fn in actions:
            button = copyable.button(name, fn)
            button.add_css_class("act")
            self.editor_buttons.append(button)
        self.stack.set_visible_child_name("edit")
        self.editor.grab_focus()

    def _save(self) -> None:
        assert self.editor is not None and self.editing is not None
        buffer = self.editor.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        doc = self.editing
        self._call_then(
            ("document_save", {"id": doc["id"], "text": text, "version": doc["version"]}),
            lambda saved: (self._leave_editor(), self._say(f"saved {saved['title']}", False),
                           self.refresh()),
            lambda error: self._refused(doc, text, error))

    def _refused(self, doc: dict, text: str, error: Exception) -> None:
        if getattr(error, "kind", None) != "StaleDocument":
            self._say(f"not saved: {error}", True)
            return
        assert self.window is not None
        self.window.get_clipboard().set(text)
        self._call_then(("document", {"id": doc["id"]}),
                        lambda newer: (self._show_document(newer),
                                       self._say(f"{error} Your edit is on the clipboard.",
                                                 True)))

    def _leave_editor(self) -> None:
        assert self.stack is not None
        self.editing = None
        self.stack.set_visible_child_name("browse")
        self._say("", False)

    # --- the buttons -----------------------------------------------------------------
    def _act(self, action: str, entry: dict) -> None:
        if action == "upload":
            # The user reads exactly what leaves the machine before it goes.
            self._call_then(("catalog_upload_files", {"kind": entry["kind"], "id": entry["id"]}),
                            lambda listing: self._show_confirm(entry, listing))
            return
        self._send(*CatalogModel.request(action, entry))

    def _show_confirm(self, entry: dict, listing: dict) -> None:
        """What would leave the machine, as a tree the user unticks from: a folder takes
        everything under it, and a file its build reads cannot be left out. What they leave out
        is kept in the layer for their next upload (`layerfiles.UPLOAD_IGNORE`)."""
        assert self.confirm is not None
        self._hide_confirm()
        files = {f["name"]: f for f in listing["files"]}
        unticked = {n for n, f in files.items() if f["excluded"]}
        folders = sorted({"/".join(n.split("/")[:depth]) for n in files
                          for depth in range(1, n.count("/") + 1)})
        under = {d: [n for n in files if n.startswith(f"{d}/")] for d in folders}
        boxes: dict[str, tuple[Gtk.CheckButton, int]] = {}
        header = _label("", "name", lines=2)

        def sync() -> None:
            for name, (box, handler) in boxes.items():
                if name in files:
                    ticked, mixed = name not in unticked, False
                else:
                    states = {n not in unticked for n in under[name]}
                    ticked, mixed = all(states), len(states) > 1
                with box.handler_block(handler):
                    box.set_active(ticked)
                box.set_inconsistent(mixed)
            going = [f for n, f in files.items() if n not in unticked]
            copyable.set_text(header, (
                f"Upload {entry['name']}: {len(going)} of {len(files)} files, "
                f"{sum(f['bytes'] for f in going)} bytes, go to the registry as a pull request "
                "from your GitHub account. What you untick stays here, and is left out again "
                "next time."))

        def toggled(name: str, active: bool) -> None:
            for n in under.get(name, [name]):
                if not files[n]["required"]:
                    (unticked.discard if active else unticked.add)(n)
            sync()

        tree = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for name in sorted([*files, *folders]):
            depth = name.count("/")
            leaf = name.rsplit("/", 1)[-1]
            if name in files:
                f = files[name]
                text = f"{leaf}  ({f['bytes']} bytes)" + (
                    "  — its build reads it" if f["required"] else "")
                locked = f["required"]
            else:
                text, locked = f"{leaf}/", all(files[n]["required"] for n in under[name])
            box, handler = copyable.check(text, lambda active, name=name: toggled(name, active),
                                          margin_start=18 * depth, sensitive=not locked)
            boxes[name] = (box, handler)
            tree.append(box)
        self.confirm.append(header)
        self.confirm.append(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
            max_content_height=360, child=tree))
        sync()
        row = Gtk.Box(spacing=8)
        send = copyable.button("Send", lambda: (
            self._hide_confirm(),
            self._send(f"{entry['name']}: uploading",
                       ("catalog_upload", {"kind": entry["kind"], "id": entry["id"],
                                           "excluded": sorted(unticked)}))))
        send.add_css_class("act")
        cancel = copyable.button("Cancel", self._hide_confirm)
        cancel.add_css_class("act")
        row.append(send)
        row.append(cancel)
        self.confirm.append(row)
        self.confirm.set_visible(True)

    def _hide_confirm(self) -> None:
        if self.confirm is None:
            return
        while (child := self.confirm.get_first_child()) is not None:
            self.confirm.remove(child)
        self.confirm.set_visible(False)

    def _send(self, message: str, request: tuple[str, dict]) -> None:
        if self.pending is not None:
            return
        self.pending = message
        self._draw_status()
        self._call_then(request, lambda answer: self._done(_outcome(message, request, answer),
                                                           False),
                        lambda error: self._done(f"raigolmid: {error}", True))

    def _done(self, message: str, problem: bool) -> None:
        self.pending = None
        self._say(message, problem)
        self.refresh()

    def _call_then(self, request, ok, failed=None) -> None:
        """`request` on a worker, then `ok(answer)` or `failed(error)` on GTK's thread."""
        method, params = request
        failed = failed or (lambda error: self._say(f"raigolmid: {error}", True))

        def once(fn, value) -> bool:
            fn(value)
            return GLib.SOURCE_REMOVE

        def run() -> None:
            try:
                answer = self.model.call(method, **params)
            except Exception as exc:                   # noqa: BLE001
                GLib.idle_add(once, failed, exc)
                return
            GLib.idle_add(once, ok, answer)

        threading.Thread(target=run, name=method, daemon=True).start()

def main(argv: list[str] | None = None) -> int:
    from raigolmid.client import ApiClient
    from raigolmid.paths import Paths

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="raigolmi-catalog", description=__doc__)
    parser.add_argument("--hidden", action="store_true",
                        help="start resident and closed, for the first ask to open")
    args = parser.parse_args(argv)
    theme.load()

    client = ApiClient(Paths.from_env().api_socket)
    window = CatalogWindow(CatalogModel(call=client.call), shown=not args.hidden)
    # Not unique, for the selector's reason (`ui/selector_native/selector.py`).
    app = Gtk.Application(application_id="os.raigolmi.catalog",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", window.build)
    # Held: the window is unmapped while closed, and an application with no visible window
    # would otherwise quit.
    app.connect("activate", lambda a: a.hold())
    status = app.run([])
    return 1 if window.failure else status


if __name__ == "__main__":
    sys.exit(main())
