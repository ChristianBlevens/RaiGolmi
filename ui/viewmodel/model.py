"""What the selector shows, and what pressing a key does.

Two rows — Faces and Bodies — at most one selection per row, any item deselectable.
The toolbelt is not the user's to select: it is a line naming the active sandbox's, or that
none is open. Incompatible items are greyed out, **not hidden**, with the reason on focus.

No compatibility is computed here. The model asks raigolmid and renders the answer, so the
selector and the daemon can never disagree about whether something is selectable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

ROWS = ("faces", "bodies")
KIND_OF_ROW = {"faces": "face", "bodies": "body"}


@dataclass(frozen=True, slots=True)
class InstanceBadge:
    instance: str
    health: str
    reason: str

    def render(self) -> str:
        parts = []
        if self.health == "degraded":
            parts.append(f"degraded — {self.reason}")
        elif self.health != "ok":
            parts.append(self.health)
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class RowItem:
    id: str
    name: str
    selected: bool
    selectable: bool
    reason: str
    warning: str
    instances: tuple[InstanceBadge, ...] = ()
    # A body's tab: selecting the body goes to it rather than opening another.
    tab: str | None = None
    capabilities: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()

    @property
    def badges(self) -> str:
        """What follows the name in a row: the body's tab, and its sandbox's health."""
        parts = [self.tab] if self.tab else []
        parts += [text for b in self.instances if (text := b.render())]
        return " ".join(parts)

    @property
    def marker(self) -> str:
        return "●" if self.selected else "○"

    @property
    def detail(self) -> str:
        """What the status line shows when this item has focus."""
        if not self.selectable:
            return f"unavailable — {self.reason}"
        if self.warning:
            return f"⚠ {self.warning}"
        if self.capabilities:
            return "provides " + ", ".join(self.capabilities)
        if self.requires:
            return "wants " + ", ".join(self.requires) + " from a toolbelt"
        return ""


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    items: tuple[RowItem, ...]

    @property
    def title(self) -> str:
        return self.name.capitalize()

    @property
    def kind(self) -> str:
        return KIND_OF_ROW[self.name]


@dataclass(slots=True)
class SelectorModel:
    call: Callable[..., Any]
    rows: dict[str, Row] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)

    def refresh(self) -> "SelectorModel":
        return self.apply(self.fetch())

    def fetch(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The daemon's answers, without touching the model — so a UI can ask from a worker
        and `apply` on its own thread."""
        return self.call("list_items"), self.call("status")

    def apply(self, fetched: tuple[dict[str, Any], dict[str, Any]]) -> "SelectorModel":
        items, self.status = fetched
        self.rows = {
            name: Row(name=name, items=tuple(_item(raw) for raw in items.get(name, [])))
            for name in ROWS
        }
        return self

    def toggle(self, row_name: str, item_id: str) -> str:
        message, request = self.toggle_request(row_name, item_id)
        if request is not None:
            method, params = request
            self.call(method, **params)
        return message

    def toggle_request(self, row_name: str, item_id: str
                       ) -> tuple[str, tuple[str, dict[str, Any]] | None]:
        """What toggling the item means: the message it reports and the daemon call that
        does it, or None when there is nothing to call. Split from `toggle` so a UI can make
        the call off its drawing thread.

        Selecting the selected item deselects it: any item can be deselected, and
        one key for both directions means there is no state the user can reach and not
        leave."""
        row = self.rows[row_name]
        item = next((i for i in row.items if i.id == item_id), None)
        if item is None:
            return f"no such item '{item_id}'", None
        if item.selected:
            return f"deselected {item.name}", ("deselect", {"kind": row.kind})
        if not item.selectable:
            return item.reason, None
        return f"selected {item.name}", ("select", {"kind": row.kind, "id": item_id})

    @property
    def meaning(self) -> str:
        """Every state is intentional, so the selector always says what the current one
        means."""
        return self.status.get("session", {}).get("meaning", "")

    @property
    def toolbelt(self) -> str:
        """The active sandbox's toolbelt, shown in place of a toolbelt row."""
        active = self.status.get("session", {}).get("toolbelt")
        return f"Toolbelt: {active}" if active else "Toolbelt: none open"

    @property
    def definition_errors(self) -> list[str]:
        return self.status.get("definition_errors", [])


def _item(raw: dict[str, Any]) -> RowItem:
    return RowItem(
        id=raw["id"],
        name=raw["name"],
        selected=raw["selected"],
        selectable=raw["selectable"],
        reason=raw.get("reason", ""),
        warning=raw.get("warning", ""),
        instances=tuple(
            InstanceBadge(instance=i["instance"], health=i["health"],
                          reason=i.get("reason", ""))
            for i in raw.get("instances", [])
        ),
        tab=raw.get("tab"),
        capabilities=tuple(raw.get("capabilities", [])),
        requires=tuple(raw.get("requires", [])),
    )
