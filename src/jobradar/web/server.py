"""Serves the dashboard with Uvicorn on `web.host`, port `web.port` or the next free one."""

from __future__ import annotations

import errno
import logging
import socket

from jobradar.config import Settings
from jobradar.db.repo import Repo
from jobradar.queue.tasks import TaskQueue

log = logging.getLogger(__name__)

PORT_TRIES = 10  # 8080 busy: try 8081 … 8089


class NoFreePort(Exception):
    pass


def _bindable(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host.strip("[]"), port))
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return False
            raise
    return True


def free_port(host: str, port: int) -> int:
    """`port` if nothing uses it, else the next free one within PORT_TRIES."""
    for candidate in range(port, min(port + PORT_TRIES, 65536)):
        if _bindable(host, candidate):
            return candidate
    raise NoFreePort(
        f"ports {port}-{port + PORT_TRIES - 1} on {host} are all in use; "
        "set web.port in config.yaml to a free one"
    )


async def serve(settings: Settings, repo: Repo, queue: TaskQueue | None = None) -> None:
    import uvicorn

    from jobradar.web.app import create_app

    web = settings.config.web
    port = free_port(web.host, web.port)
    if port != web.port:
        log.warning("Port %d is in use; the dashboard took %d instead", web.port, port)
    host = f"[{web.host}]" if ":" in web.host else web.host
    log.info("Dashboard: http://%s:%d", host, port)
    config = uvicorn.Config(
        create_app(settings, repo, queue=queue),
        host=web.host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
        server_header=False,
    )
    await uvicorn.Server(config).serve()
