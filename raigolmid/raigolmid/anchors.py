"""Anchor containers.

Linux kills every process in a PID namespace when that namespace's first process exits.
If the session view joined the body's PID namespace directly, every body rebuild would
SIGKILL the terminals' shells and the language servers. So each instance gets an
anchor: a tiny long-lived container whose only process is a minimal init that reaps
zombies. It owns the instance's PID and network namespaces, and the body and the view
both join *it* — neither owns them.

The anchor publishes nothing: several bodies run at once and two may listen on one port, so
the active sandbox's ports reach the host through the door instead (`doors.py`). It
is on the machine's network by the sandbox's name (`naming.host`), which is how the face and
every other anchor reach the body's and the toolbelt's ports.

In development the body's main process is not PID 1 of its
namespace, unlike in production. Signal and zombie-reaping behaviour differ slightly,
usually in a forgiving direction.
"""
from __future__ import annotations

from . import labels, naming
from .runtime import ContainerInfo, ContainerRuntime, ContainerSpec

DEFAULT_ANCHOR_IMAGE = "registry.k8s.io/pause:3.9"


class Anchors:
    def __init__(self, runtime: ContainerRuntime, epoch: int,
                 image: str = DEFAULT_ANCHOR_IMAGE) -> None:
        self.runtime = runtime
        self.epoch = epoch
        self.image = image

    def get(self, instance: str) -> ContainerInfo | None:
        return self.runtime.inspect(naming.anchor(instance))

    def ensure(self, instance: str, tab: str | None = None) -> ContainerInfo:
        name = naming.anchor(instance)
        existing = self.runtime.inspect(name)
        if existing is not None and existing.running:
            return existing
        if existing is not None:
            # An exited anchor cannot be restarted into usefulness: the body and view that
            # joined its namespaces are already dead with it. Replace it outright.
            self.runtime.remove(name, force=True)

        spec_labels = {
            labels.MANAGED: "true",
            labels.ROLE: labels.Role.ANCHOR,
            labels.INSTANCE: instance,
            labels.EPOCH: str(self.epoch),
        }
        if tab:
            spec_labels[labels.TAB] = tab

        self.runtime.ensure_network(naming.network(), {labels.MANAGED: "true"})
        return self.runtime.run(ContainerSpec(
            name=name,
            image=self.image,
            labels=spec_labels,
            network=naming.network(),
            aliases=(naming.host(instance),),
            # An anchor runs with no added capabilities. Its job is to exist.
            cap_drop=("ALL",),
        ))

    def namespace_ref(self, instance: str) -> str:
        """What the body and the view pass as `pid:` and `network_mode:`."""
        info = self.get(instance)
        if info is None or not info.running:
            raise RuntimeError(
                f"the anchor for {instance} is not running; the body and the session view "
                "have nothing to join"
            )
        return f"container:{info.id}"

    def remove(self, instance: str) -> None:
        self.runtime.remove(naming.anchor(instance), force=True)
