"""Stop work nobody is waiting for: when the browser goes away, the model call it started is cancelled too."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable

from fastapi import HTTPException, Request


async def unless_gone(request: Request, work: Awaitable[Any], poll: float = 1.0) -> Any:
    """Await `work`, cancelling it if the client disconnects first (a 499 the client never sees)."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                raise HTTPException(499, "the client went away")
    except asyncio.CancelledError:
        task.cancel()
        raise
