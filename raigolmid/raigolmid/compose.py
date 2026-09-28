"""Generated Compose projects for bodies.

Each body instance is one generated Compose project whose single service joins the
anchor's namespaces. Compose buys lifecycle, labels, networks and multi-body later; the
daemon triggers rebuilds itself, so Compose Watch is never run — it would replace the body
without the daemon's rebuild sequence, under a view that still holds its mounts.

A body's own `develop.watch` block is still written into the generated file and read back
as configuration, because it is the project's own statement of which files are
dependencies. It is documentation that the daemon acts on, not a Compose feature in use.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import labels, localtime, naming
from .definitions import Body
from .runtime import RuntimeError_


class ComposeError(RuntimeError_):
    """A Compose invocation failed. Carries the command and its stderr — the user or the
    agent has to act on the actual message, not on a summary of it."""


@dataclass(frozen=True, slots=True)
class BodyPlacement:
    """Everything about *this* instance of a body that the definition does not know:
    where its working copy is and whose namespaces it joins."""
    instance: str
    namespace_ref: str        # "container:<anchor id>"
    working_copy: Path
    image: str
    # `uid:gid` of the working copy's owner: what the body writes in /work is the user's, as the
    # view's is, never root's.
    user: str
    toolbelt: str | None = None
    tab: str | None = None


def render(body: Body, placement: BodyPlacement, epoch: int,
           definition_digest: str) -> dict:
    service_labels = {
        labels.MANAGED: "true",
        labels.ROLE: str(labels.Role.BODY),
        labels.INSTANCE: placement.instance,
        labels.EPOCH: str(epoch),
        labels.DEFINITION_DIGEST: definition_digest,
    }
    if body.ports:
        service_labels[labels.BODY_PORTS] = ",".join(str(p) for p in sorted(body.ports))
    if placement.tab:
        service_labels[labels.TAB] = placement.tab

    service: dict = {
        "image": placement.image,
        # The anchor owns both namespaces; the body joins, never owns.
        "pid": placement.namespace_ref,
        "network_mode": placement.namespace_ref,
        "labels": service_labels,
        "volumes": [f"{placement.working_copy}:/work",
                    *(f"{m.source}:{m.target}:ro" for m in localtime.mounts(localtime.LOCALTIME))],
        "working_dir": "/work",
        "user": placement.user,
        # No restart policy: a body that dies is a fact the daemon surfaces and the
        # rebuild sequence responds to, not something to paper over with a retry loop.
        "restart": "no",
    }
    if body.command:
        service["command"] = list(body.command)
    # The user's timezone, as every container they read a clock in has it; a body naming its
    # own wins.
    environment = {**localtime.environment(localtime.LOCALTIME), **body.environment}
    if environment:
        service["environment"] = environment
    if body.read_only:
        service["read_only"] = True
        service["tmpfs"] = ["/tmp"]
    if body.watch:
        service["develop"] = {
            "watch": [{"path": r.path, "action": r.action} for r in body.watch]
        }

    return {"services": {naming.body_service(): service}}


def write(body: Body, placement: BodyPlacement, epoch: int, definition_digest: str,
          directory: Path) -> Path:
    """Compose files are written under the daemon's own state, never into the project.
    A body definition is the user's file; the generated project is the daemon's."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "compose.yaml"
    path.write_text(_compose_file(render(body, placement, epoch, definition_digest)),
                    encoding="utf-8")
    return path


class ComposeCLI:
    """`docker compose` as a subprocess. The Python SDK has no
    Compose support, and shelling out is what the Compose team supports."""

    def __init__(self, binary: tuple[str, ...] = ("docker", "compose")) -> None:
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary[0]) is not None

    def _run(self, project: str, file: Path, *args: str,
             timeout: int = 600) -> subprocess.CompletedProcess[str]:
        cmd = [*self.binary, "-p", project, "-f", str(file), *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ComposeError(f"{self.binary[0]} is not installed: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ComposeError(f"{' '.join(cmd)} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise ComposeError(
                f"{' '.join(cmd)} exited {proc.returncode}\n{proc.stderr.strip()}"
            )
        return proc

    def config(self, project: str, file: Path) -> str:
        return self._run(project, file, "config").stdout

    def up(self, project: str, file: Path, force_recreate: bool = False) -> None:
        args = ["up", "-d", "--no-build"]
        if force_recreate:
            args.append("--force-recreate")
        self._run(project, file, *args)

    def down(self, project: str, file: Path, volumes: bool = False) -> None:
        args = ["down", "--remove-orphans"]
        if volumes:
            args.append("-v")
        self._run(project, file, *args)

    def ps(self, project: str, file: Path) -> list[dict]:
        out = self._run(project, file, "ps", "--format", "json").stdout.strip()
        if not out:
            return []
        # Compose emits one JSON object per line in recent versions and a JSON array in
        # older ones. Accept both rather than pinning a version the user may not have.
        if out.startswith("["):
            return json.loads(out)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def container_id(self, project: str, file: Path, service: str) -> str | None:
        out = self._run(project, file, "ps", "-q", service).stdout.strip()
        return out.splitlines()[0] if out else None


# --- the file Compose is handed ------------------------------------------------------------
#
# JSON, which a YAML parser reads as the same data, so there is no quoting rule of this
# module's own to get wrong. Compose interpolates `$` in every value and reads `$$` as a
# literal one, so each string value has its `$` doubled; keys are not interpolated. What
# YAML cannot hold raw — DEL, the C1 controls, and the separators a YAML 1.1 parser breaks a
# line at — is escaped, and JSON escapes the rest. Non-ASCII stays raw: JSON would escape a
# character past U+FFFF as a surrogate pair, which YAML refuses.

_UNPRINTABLE = re.compile("[\x7f-\x9f\u2028\u2029\ufffe\uffff]")


def _literal(data):
    if isinstance(data, str):
        return data.replace("$", "$$")
    if isinstance(data, dict):
        return {key: _literal(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_literal(item) for item in data]
    return data


def _compose_file(data) -> str:
    text = json.dumps(_literal(data), indent=2, ensure_ascii=False)
    return _UNPRINTABLE.sub(lambda m: f"\\u{ord(m.group()):04x}", text) + "\n"
