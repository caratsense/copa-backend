"""
Sync → async WebSocket bridge.

Route handlers here are plain `def`, so Starlette runs them in a worker thread.
That thread has no event loop, which means `asyncio.get_event_loop()` raises and
any `ensure_future` call is lost. We capture the server's real loop once at
startup and hand coroutines to it with `run_coroutine_threadsafe`.

Usage:
    # in lifespan
    set_main_loop(asyncio.get_running_loop())

    # from any sync service function
    broadcast_sync({"type": "STATUS_CHANGED", "order_id": 42})
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def broadcast_sync(message: dict) -> None:
    """Fan a message out to all connected order-feed sockets. Best effort."""
    if _main_loop is None:
        logger.warning("[Broadcast] No event loop captured — dropping %s", message.get("type"))
        return

    try:
        from app.api.routes.websocket import manager
        asyncio.run_coroutine_threadsafe(manager.broadcast(message), _main_loop)
    except Exception as e:
        logger.error("[Broadcast] Failed to dispatch %s: %s", message.get("type"), e)
