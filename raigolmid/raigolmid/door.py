"""The door's forwarder: run in the door container by `doors.py`,
never imported by the daemon.

`python3 door.py <address> <port>...` listens on each port on every address of the host's
network namespace and joins each connection to the same port at `address`, the active
sandbox's anchor. The body listens inside that anchor's network namespace; this is how its
ports reach the host without the anchor publishing any, so a selection replaces the door and
never an anchor.

A port it cannot listen on ends it with the reason, before it says it is open: a door that
forwards some of the ports it was given would be a door the daemon reports as whole.
"""
from __future__ import annotations

import asyncio
import sys

CHUNK = 65536


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


def _handler(address: str, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(address, port)
        except OSError as exc:
            # The body is not listening yet, or has gone: the client sees the refusal, and the
            # door stays open for the next connection.
            print(f"door: {address}:{port} refused a connection: {exc}", file=sys.stderr,
                  flush=True)
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    return handle


async def main(address: str, ports: list[int]) -> None:
    servers = [await asyncio.start_server(_handler(address, p), host=None, port=p)
               for p in ports]
    print(f"door: open on {' '.join(map(str, ports))} to {address}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], [int(p) for p in sys.argv[2:]]))
