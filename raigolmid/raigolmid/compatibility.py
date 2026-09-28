"""The compatibility model.

Two rules, evaluated pairwise: body↔toolbelt and toolbelt↔face. Each returns
`(compatible, reason)`, and the reason is written for whoever is choosing: the agent opening
a sandbox with a toolbelt, which is refused with it, and the user reading why.

The user's own rows, faces and bodies, do not constrain each other, so their selection can
never trap them. The toolbelts are judged against a whole selection, because "may a
sandbox open with this" only has an answer relative to the body and the face.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from . import labels
from .definitions import Body, Catalogue, Face, Toolbelt

UNKNOWN_RUNTIME_WARNING = "compatibility unknown: this image carries no runtime label"


@dataclass(frozen=True, slots=True)
class Selection:
    """What the user selects: the face they see with and the body whose tab has the
    face. The toolbelt is not theirs: an agent names one when it opens a sandbox."""
    face: str | None = None
    body: str | None = None

    def without(self, kind: labels.Kind) -> "Selection":
        return Selection(
            face=None if kind is labels.Kind.FACE else self.face,
            body=None if kind is labels.Kind.BODY else self.body,
        )

    def with_(self, kind: labels.Kind, item_id: str | None) -> "Selection":
        return Selection(
            face=item_id if kind is labels.Kind.FACE else self.face,
            body=item_id if kind is labels.Kind.BODY else self.body,
        )

    def get(self, kind: labels.Kind) -> str | None:
        return {labels.Kind.FACE: self.face, labels.Kind.BODY: self.body}[kind]


@dataclass(frozen=True, slots=True)
class Verdict:
    selectable: bool
    reason: str = ""
    warning: str = ""


def runtime_matches(runtime: str | None, patterns: tuple[str, ...]) -> bool:
    """`python:3.*` against `python:3.12`. A toolbelt with no `supports` claims nothing
    and is therefore never refused on that ground."""
    if not patterns:
        return True
    if runtime is None:
        return False
    return any(fnmatch.fnmatch(runtime, p) for p in patterns)


def body_toolbelt(body: Body, toolbelt: Toolbelt) -> Verdict:
    if not toolbelt.supports:
        return Verdict(True, warning=f"{toolbelt.name} does not say what it supports")
    if body.runtime is None:
        # Unlabeled bodies stay selectable, with the uncertainty said out loud.
        return Verdict(True, warning=UNKNOWN_RUNTIME_WARNING)
    if runtime_matches(body.runtime, toolbelt.supports):
        return Verdict(True)
    return Verdict(
        False,
        f"{toolbelt.name} supports {', '.join(toolbelt.supports)}; "
        f"{body.name} is {body.runtime}",
    )


def toolbelt_face(toolbelt: Toolbelt | None, face: Face) -> Verdict:
    """`requires_toolbelt_capabilities` constrains which toolbelt may be *paired* with a
    face, not whether the face may be selected at all.

    With no toolbelt there is no pairing to judge, and that state is named outright:
    face with its editor, no language intelligence. Refusing it would
    make a state the design calls meaningful unreachable, and would trap a user who
    selected the face first. So the missing capability is a warning here and a refusal
    only against a toolbelt that actually lacks it, pairwise.
    """
    required = set(face.requires_toolbelt_capabilities)
    if not required:
        return Verdict(True)
    if toolbelt is None:
        return Verdict(
            True,
            warning=f"{face.name} wants {', '.join(sorted(required))} from a toolbelt; "
                    "with none selected its editor runs without language intelligence",
        )
    missing = required - set(toolbelt.capabilities)
    if missing:
        return Verdict(
            False,
            f"{face.name} needs {', '.join(sorted(missing))}; "
            f"{toolbelt.name} provides {', '.join(toolbelt.capabilities) or 'nothing'}",
        )
    return Verdict(True)


def evaluate(catalogue: Catalogue, selection: Selection) -> dict[str, dict[str, Verdict]]:
    """Every item in the catalogue with a verdict for the given selection.

    Faces and bodies do not constrain each other, so both rows are always selectable. The
    toolbelts' verdicts are what a sandbox on this selection may open with: judged
    against the selected body and the face that will run its tools.
    """
    sel_body = catalogue.bodies.get(selection.body) if selection.body else None
    sel_face = catalogue.faces.get(selection.face) if selection.face else None
    return {
        "faces": {face_id: Verdict(True) for face_id in catalogue.faces},
        "bodies": {body_id: Verdict(True) for body_id in catalogue.bodies},
        "toolbelts": {tb_id: sandbox_toolbelt(sel_body, sel_face, toolbelt)
                      for tb_id, toolbelt in catalogue.toolbelts.items()},
    }


def sandbox_toolbelt(body: Body | None, face: Face | None, toolbelt: Toolbelt) -> Verdict:
    """Whether a sandbox of `body` (None: the no-body `/work`) may open with `toolbelt`, its
    tools shown by `face` (None for a sandbox the face is not on)."""
    verdict = Verdict(True) if body is None else body_toolbelt(body, toolbelt)
    if verdict.selectable and face is not None:
        verdict = toolbelt_face(toolbelt, face)
    return verdict


# --- what a selection means ---------------------------------------------------------

STATE_MEANINGS = {
    (False, False): "Bare host: selector and AI terminal, the machine tab on /work with no "
                    "body. Idle, and the state everything else falls back to.",
    (True, False):  "The face's desktop, on /work with no body.",
    (False, True):  "The body's tab on its working copy, worked on through the AI "
                    "terminal.",
    (True, True):   "The face with its editor on the body's working copy.",
}


def describe(selection: Selection, catalogue: Catalogue | None = None) -> str:
    key = (selection.face is not None, selection.body is not None)
    meaning = STATE_MEANINGS[key]
    if key == (True, False) and catalogue is not None and selection.face:
        face = catalogue.faces.get(selection.face)
        if face is not None and face.editor is None:
            return "The face's desktop."
    return meaning
