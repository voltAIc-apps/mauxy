"""
Fire-and-forget jobs for contact-dependent work (security review 2026-11-08, timing).

/api/subscribe and /api/selfcheck answer as soon as the input is valid; the Mautic lookup, the
throttle check, storage and the DOI mail run here, after the response is sent. So response
time and status code are the same for every address (no enumeration through timing or 503s).

Unlike Starlette BackgroundTasks (which run inside the request cycle) these are plain asyncio
tasks: the HTTP exchange is complete before they start. A strong reference is kept until the
task ends; errors are logged, never raised. drain() is awaited on shutdown and in tests.
A job lost through a hard kill only means one DOI mail is missing; the person can resubmit.
"""
import asyncio
import logging
from typing import Awaitable

logger = logging.getLogger("mauxy.jobs")
_tasks: set[asyncio.Task] = set()


def spawn(coro: Awaitable, name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error("JOB_FAILED name=%s error=%r", name, t.exception())

    task.add_done_callback(_done)


async def drain() -> None:
    """Wait for every running job (shutdown, tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
