"""The door: how the active sandbox's ports reach the host.

Several bodies run at once, one per body tab, and two may listen on the same port, so no
anchor publishes anything. One door container publishes the active sandbox's ports and
forwards each to its anchor (`door.py`). A selection that moves the face replaces the door,
never an anchor: replacing an anchor takes its view down with it, which would end
another tab's shells and servers.

The door carries what it forwards to in a label, so showing the same thing again is a no-op
and a daemon restart adopts the door that is already right.
"""
from __future__ import annotations

from pathlib import Path

from . import hostimages, labels, naming
from .events import EventLog
from .runtime import ContainerRuntime, ContainerSpec, Mount

SCRIPT_IN_DOOR = "/door.py"


class DoorError(RuntimeError):
    pass


class Door:
    def __init__(self, runtime: ContainerRuntime, events: EventLog, epoch: int) -> None:
        self.runtime = runtime
        self.events = events
        self.epoch = epoch

    def show(self, instance_id: str | None, ports: tuple[int, ...]) -> None:
        """The door forwards `ports` to `instance_id`'s anchor, or is closed with nothing to
        forward. Raises with the reason when the ports could not be published."""
        name = naming.door()
        current = self.runtime.inspect(name)
        if not ports or instance_id is None:
            if current is not None:
                self.runtime.remove(name, force=True)
                self.events.emit("door.closed", instance=current.labels.get(labels.DOOR))
            return
        anchor = self.runtime.inspect(naming.anchor(instance_id))
        if anchor is None or not anchor.running or anchor.ip is None:
            raise DoorError(f"the anchor of {instance_id} is not running on the network, so "
                            "there is nothing for its ports to reach")
        want = f"{instance_id} {anchor.ip} {','.join(map(str, ports))}"
        if current is not None and current.running and current.labels.get(labels.DOOR) == want:
            return
        if current is not None:
            self.runtime.remove(name, force=True)
        # On the anchors' network: a bridge reaches only its own members.
        self.runtime.ensure_network(naming.network(), {labels.MANAGED: "true"})
        self.runtime.run(ContainerSpec(
            name=name,
            image=hostimages.ensure(self.runtime, hostimages.door()),
            command=("python3", SCRIPT_IN_DOOR, anchor.ip, *map(str, ports)),
            labels={labels.MANAGED: "true", labels.ROLE: labels.Role.DOOR,
                    labels.EPOCH: str(self.epoch), labels.DOOR: want},
            mounts=(Mount(source=str(Path(__file__).resolve().parent / "door.py"),
                          target=SCRIPT_IN_DOOR, read_only=True),),
            ports={p: p for p in ports},
            network=naming.network(),
            # A door forwards bytes. It needs no privilege to bind: Docker publishes the ports.
            cap_drop=("ALL",),
        ))
        self.events.emit("door.opened", instance=instance_id, ports=list(ports))
