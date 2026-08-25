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
    fleet_broadcast_sync({"type": "delivery_started", "order_id": 42})

Everything here is best effort: a broadcast failure must never fail the request
that triggered it, because the database change has already been committed.
"""

import asyncio
import logging
from typing import Any, Coroutine, Optional

logger = logging.getLogger(__name__)

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def _dispatch(coro: Coroutine[Any, Any, Any], label: str) -> None:
    """Hand a coroutine to the server loop from a worker thread."""
    if _main_loop is None:
        logger.warning("[Broadcast] No event loop captured — dropping %s", label)
        coro.close()
        return
    try:
        future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
    except Exception as e:
        logger.error("[Broadcast] Failed to dispatch %s: %s", label, e)
        coro.close()
        return

    def _report(fut) -> None:
        # Without this the coroutine's exception is stored on a Future nobody
        # inspects and vanishes. That matters here: if the fan-out raises
        # part-way, the customer never receives `tracking_ended` and the rider's
        # socket is never closed — with no trace of why.
        try:
            fut.result()
        except Exception as e:  # noqa: BLE001 — best effort, must never re-raise
            logger.error("[Broadcast] %s failed: %s", label, e)

    future.add_done_callback(_report)


def broadcast_sync(message: dict) -> None:
    """Fan a message out to all connected order-feed sockets. Best effort."""
    try:
        from app.api.routes.websocket import manager
    except Exception as e:
        logger.error("[Broadcast] import failed: %s", e)
        return
    _dispatch(manager.broadcast(message), message.get("type", "?"))


def fleet_broadcast_sync(message: dict) -> None:
    """Notify every admin watching the live-delivery view. Best effort."""
    try:
        from app.api.routes.websocket import fleet_manager
    except Exception as e:
        logger.error("[Broadcast] import failed: %s", e)
        return
    _dispatch(fleet_manager.broadcast(message), message.get("type", "?"))


def delivery_started_sync(order_id: int, payload: dict) -> None:
    """
    Announce a delivery that has just gone out, and let the fleet stream forward
    its positions from now on.
    """
    try:
        from app.api.routes.websocket import fleet_manager
    except Exception as e:
        logger.error("[Broadcast] import failed: %s", e)
        return

    async def _start():
        # Mutated on the event loop, not here in the worker thread: snapshots
        # rebuild this same set from the loop, and a cross-thread update can be
        # silently lost when the two interleave.
        fleet_manager.track(order_id)
        await fleet_manager.broadcast({"type": "delivery_started", **payload})

    _dispatch(_start(), "delivery_started")


def rider_reassigned_sync(order_id: int, payload: dict) -> None:
    """
    Move an in-flight delivery to a different rider.

    Closing the previous rider's GPS socket is the important part: their device
    keeps reporting every few seconds, and the next ping would re-create the
    Redis keys the reassignment just cleared — restoring their position under
    the new rider's name. A reconnect attempt is then refused, because they are
    no longer the assigned rider.
    """
    try:
        from app.api.routes.websocket import fleet_manager, rider_sockets
    except Exception as e:
        logger.error("[Broadcast] import failed: %s", e)
        return

    async def _swap():
        await rider_sockets.close_order(order_id)
        await fleet_manager.broadcast({"type": "rider_reassigned", **payload})

    _dispatch(_swap(), "rider_reassigned")


def delivery_completed_sync(order_id: int, payload: dict) -> None:
    """
    Wind a delivery down everywhere at once.

    Stops the fleet stream forwarding this order, tells admins to drop the row,
    tells the customer's map that tracking has ended, and closes the rider's GPS
    socket so no further positions can be written for a completed order.
    """
    try:
        from app.api.routes.websocket import (
            delivery_manager,
            fleet_manager,
            rider_sockets,
        )
    except Exception as e:
        logger.error("[Broadcast] import failed: %s", e)
        return

    async def _finish():
        # See delivery_started_sync: all mutation of the active set happens on
        # the event loop so it cannot race a snapshot rebuilding it.
        fleet_manager.untrack(order_id)
        await fleet_manager.broadcast({"type": "delivery_completed", **payload})
        await delivery_manager.close_order(
            order_id, {"type": "tracking_ended", "order_id": order_id}
        )
        await rider_sockets.close_order(order_id)

    _dispatch(_finish(), "delivery_completed")
