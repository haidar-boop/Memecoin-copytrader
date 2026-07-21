"""Shared scaffolding for worker processes: logging, metrics, shutdown."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from app.config import Settings, get_settings
from app.logging_config import configure_logging, get_logger
from app.services.metrics import start_metrics_server

log = get_logger(__name__)


def run_worker(name: str, main: Callable[[Settings], Awaitable[None]]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.metrics_enabled:
        start_metrics_server(settings.metrics_port)
    log.info("worker_starting", worker=name, metrics_port=settings.metrics_port)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(main(settings))
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except NotImplementedError:  # pragma: no cover (non-unix)
                pass
        try:
            await task
        except asyncio.CancelledError:
            log.info("worker_stopped", worker=name)

    asyncio.run(_run())
