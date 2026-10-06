"""`raigolmid` — the daemon entrypoint."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .daemon import AlreadyRunning, Daemon
from .definitions import SearchPaths
from .paths import Paths

logger = logging.getLogger("raigolmid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="raigolmid", description="RaiGolmi control plane")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(),
                        help="where faces/, toolbelts/ and bodies/ are looked for "
                             "(overridden per-kind by RAIGOLMID_*_PATH)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from .runtime.docker_runtime import DockerRuntime
    try:
        runtime = DockerRuntime()
    except Exception as exc:                           # noqa: BLE001
        logger.error("cannot reach the container runtime: %s", exc)
        logger.error("raigolmid needs the Docker daemon's socket reachable from this shell.")
        return 2

    paths = Paths.from_env()
    search = SearchPaths.defaults(args.repo_root, paths.private)
    try:
        daemon = Daemon(runtime, paths, search)
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        return 3

    logger.info("raigolmid epoch %d listening on %s", daemon.epoch, paths.api_socket)
    return daemon.run_forever()


if __name__ == "__main__":
    sys.exit(main())
