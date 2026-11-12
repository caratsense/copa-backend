"""
The one place stock moves.

Before this there were two, and they did not agree with each other. A
decrement sat inside create_order's item loop:

    if addon and addon.stock is not None:
        addon.stock = max(0, addon.stock - item_data.quantity)

— no check beforehand, so two customers could both buy the last topper and the
count simply floored at zero. The second one paid for a thing that was not
there and nothing anywhere said so. The matching restore lived two hundred
lines away in `_restore_addon_stock`, ran only on cancellation, and covered
only add-ons; products and extras had no count to restore.

Everything that reads, reserves, releases or sets a count goes through this
module, and every change writes a StockMovement. That ledger is half the
point: a count with no history behind it cannot answer the question anybody
actually asks, which is where the other four went.

WHEN STOCK IS TAKEN
-------------------
At order creation, not at payment. The alternative is tempting — reserving
only once the money lands means an abandoned checkout never holds anything —
but it moves the problem somewhere worse: you cannot refuse an order after
taking the customer's money for it. Taking the units up front means the shop
never oversells, at the cost of a dead attempt sitting on a topper until
somebody cancels it.

That cost is paid down by making it visible rather than by expiring it on a
timer. `snapshot` reports what each item is holding for orders nobody has paid
for, so a squat shows up as a number beside the count with an order number
attached, and cancelling the attempt gives the units back. Nothing here
silently alters an order on its own.
"""

from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.extra import Extra
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.pricing import AddonRule
from app.models.product import Product
from app.models.stock_movement import StockMovement

# ── The kinds of thing that can run out ──────────────────────────────────
#
# A string key rather than three code paths. The inventory screen reads all
# three tables as one list, and the ledger has to point at any of them, so the
# mapping from a kind to its model lives here and nowhere else.
KIND_PRODUCT = "product"
KIND_EXTRA = "extra"
KIND_ADDON = "addon"

_MODELS = {
    KIND_PRODUCT: Product,
    KIND_EXTRA: Extra,
    KIND_ADDON: AddonRule,
}

# What each kind is called when it is spoken about to a person. "add-on" and
# "extra" are different things in this product and both appear on the same
# screen, so the labels have to be exact.
KIND_LABELS = {
    KIND_PRODUCT: "Product",
    KIND_EXTRA: "Extra",
    KIND_ADDON: "Add-on",
}

KINDS = tuple(_MODELS)

# ── Why stock moved ──────────────────────────────────────────────────────
REASON_SOLD = "SOLD"              # reserved by an order
REASON_RELEASED = "RELEASED"      # given back when an order was cancelled
REASON_SET = "SET"                # a count typed in directly
REASON_RESTOCK = "RESTOCK"        # more arrived / more were made
REASON_WASTE = "WASTE"            # dropped, burnt, went off
REASON_CORRECTION = "CORRECTION"  # the count was wrong; this is the fix
REASON_TRACKING_ON = "TRACKING_ON"
REASON_TRACKING_OFF = "TRACKING_OFF"
REASON_MARKED_OUT = "MARKED_OUT"      # the sold-out switch, on
REASON_MARKED_BACK = "MARKED_BACK"    # the sold-out switch, off

# Reasons a person can choose when adjusting by hand. SOLD and RELEASED are
# not here: an order causes those, and a human typing one would be writing
# history that did not happen.
MANUAL_REASONS = (REASON_RESTOCK, REASON_WASTE, REASON_CORRECTION)

# What counts as nearly gone. One number, used by the dashboard tile, the
# inventory screen's sort and the low-stock query, because three definitions of
# "low" is how a screen ends up disagreeing with the badge on it.
LOW_STOCK_AT = 3


def model_for(kind: str):
    """The table a kind lives in, or a 400 naming the ones that exist."""
    model = _MODELS.get(kind)
    if model is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown stock kind '{kind}'. Expected one of: {', '.join(KINDS)}.",
        )
    return model


def resolve(db: Session, kind: str, item_id: int):
    """Fetch one tracked row, or raise 404 saying which kind was looked in."""
    model = model_for(kind)
    row = db.query(model).filter(model.id == item_id).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {KIND_LABELS[kind].lower()} with id {item_id}.",
        )
    return row


# ── The ledger ───────────────────────────────────────────────────────────


def record(
    db: Session,
    kind: str,
    row,
    delta: int,
    reason: str,
    *,
    order_id: int | None = None,
    user_id: int | None = None,
    note: str | None = None,
) -> StockMovement:
    """
    Write one line of history.

    Added to the session but not committed: a movement belongs to whatever
    transaction caused it. A sale that rolls back must not leave a ledger entry
    claiming it happened.
    """
    movement = StockMovement(
        kind=kind,
        item_id=row.id,
        item_name=getattr(row, "name", f"{kind} {row.id}"),
        delta=delta,
        balance=row.stock,
        reason=reason,
        order_id=order_id,
        user_id=user_id,
        note=(note or None),
    )
    db.add(movement)
    return movement


# ── Taking stock out ─────────────────────────────────────────────────────


def _shortfall(name: str, left: int, wanted: int) -> str:
    """
    What the customer is told when there is not enough.

    It names the thing and both numbers. "Out of stock" on its own sends
    somebody back to a cart of five items with no idea which one to change.
    """
    if left <= 0:
        return f"Sorry — {name} is sold out."
    unit = "is" if left == 1 else "are"
    return (
        f"Only {left} {unit} left of {name}, and you asked for {wanted}. "
        "Lower the quantity and try again."
    )


def take(
    db: Session,
    kind: str,
    item,
    qty: int,
    *,
    order_id: int | None = None,
    allow_sold_out: bool = False,
) -> None:
    """
    Reserve `qty` units of one item, or refuse the whole thing.

    `item` is a loaded row or an id. A row that is not counted (stock IS NULL)
    passes straight through — that is what "not counted" means, and most of the
    catalogue is made to order.

    THE CHECK AND THE DECREMENT ARE ONE STATEMENT, deliberately. Reading the
    count, comparing it in Python and then writing it back is the bug this
    module was written to kill: between the read and the write another request
    can do the same, and both see enough. A single UPDATE carrying its own
    WHERE is evaluated by the database under a row lock, so of two orders
    racing for the last topper exactly one wins and the other is told.

    Raises HTTPException(400) rather than returning a flag. Every caller is
    inside a request that must fail as a whole: a cake nobody can decorate is
    not a cake worth taking money for, and the surrounding transaction rolls
    back the order with it.
    """
    if qty <= 0:
        return

    model = model_for(kind)
    row = item if not isinstance(item, int) else resolve(db, kind, item)
    name = getattr(row, "name", f"{kind} {row.id}")

    # The switch comes first. A cake marked sold out by hand has no count to
    # consult, and refusing on the count would let it through.
    if row.sold_out and not allow_sold_out:
        raise HTTPException(status_code=400, detail=f"Sorry — {name} is sold out.")

    if row.stock is None:
        return

    taken = (
        db.query(model)
        .filter(
            model.id == row.id,
            model.stock.isnot(None),
            model.stock >= qty,
        )
        .update({model.stock: model.stock - qty}, synchronize_session=False)
    )

    # synchronize_session=False leaves the loaded row holding the old number.
    # Expiring it means the next read of .stock comes from the database, which
    # matters immediately: the ledger line below records the balance, and two
    # lines of the same order can take from the same item.
    db.expire(row, ["stock"])

    if not taken:
        left = db.query(model.stock).filter(model.id == row.id).scalar()
        raise HTTPException(
            status_code=400,
            detail=_shortfall(name, int(left or 0), qty),
        )

    record(db, kind, row, -qty, REASON_SOLD, order_id=order_id)


def give_back(
    db: Session,
    kind: str,
    item,
    qty: int,
    *,
    order_id: int | None = None,
    reason: str = REASON_RELEASED,
) -> None:
    """
    Put units back. Never raises.

    Releasing inventory must not be able to block a cancellation — that was
    already the contract of the function this replaces, and it is the right
    one: an order the shop has decided not to make has to be cancellable even
    if the count cannot be written.
    """
    if qty <= 0:
        return
    try:
        model = model_for(kind)
        row = item if not isinstance(item, int) else db.query(model).filter(model.id == item).first()
        if row is None or row.stock is None:
            return
        db.query(model).filter(model.id == row.id, model.stock.isnot(None)).update(
            {model.stock: model.stock + qty}, synchronize_session=False
        )
        db.expire(row, ["stock"])
        record(db, kind, row, qty, reason, order_id=order_id)
    except Exception as e:                       # noqa: BLE001 — best effort, by design
        import logging

        logging.getLogger(__name__).error(
            "[STOCK] Could not return %s units of %s %s: %s", qty, kind, item, e
        )


def release_order(db: Session, order: Order) -> None:
    """
    Return everything an order was holding.

    Replaces order_service._restore_addon_stock, which only knew about add-ons.
    Products and extras can be counted now, so all three have to come back or
    a cancellation quietly eats whichever ones it does not know about.

    Driven off the ledger rather than off the order's lines. The ledger says
    what was actually taken; the lines say what was asked for, and the two part
    company the moment a count stops being kept, a product is deleted, or an
    order is edited. Reading back what we wrote is also what makes this safe to
    call twice — a release that has already happened is visible, and skipped.
    """
    try:
        already = (
            db.query(StockMovement.id)
            .filter(
                StockMovement.order_id == order.id,
                StockMovement.reason == REASON_RELEASED,
            )
            .first()
        )
        if already:
            return

        taken = (
            db.query(
                StockMovement.kind,
                StockMovement.item_id,
                func.sum(StockMovement.delta),
            )
            .filter(
                StockMovement.order_id == order.id,
                StockMovement.reason == REASON_SOLD,
            )
            .group_by(StockMovement.kind, StockMovement.item_id)
            .all()
        )
        for kind, item_id, total in taken:
            units = -int(total or 0)
            if units > 0:
                give_back(db, kind, item_id, units, order_id=order.id)
    except Exception as e:                       # noqa: BLE001 — best effort, by design
        import logging

        logging.getLogger(__name__).error(
            "[STOCK] Could not release order %s: %s", getattr(order, "id", "?"), e
        )


# ── Changing a count by hand ─────────────────────────────────────────────


def set_level(
    db: Session,
    kind: str,
    item_id: int,
    stock: int | None,
    *,
    user_id: int | None = None,
    note: str | None = None,
):
    """
    Set the count outright, or stop counting with None.

    The movement records the difference rather than the new figure, so the
    ledger reads the same way whether a count was corrected or restocked, and
    the balance column carries where it landed.
    """
    row = resolve(db, kind, item_id)
    if stock is not None and stock < 0:
        raise HTTPException(status_code=400, detail="A count cannot be negative.")

    before = row.stock
    row.stock = stock

    if before is None and stock is not None:
        record(db, kind, row, stock, REASON_TRACKING_ON, user_id=user_id, note=note)
    elif before is not None and stock is None:
        # Deliberately 0, not -before. Nothing left the shelf; the shop stopped
        # counting. A delta of -before would read as the stock being thrown out.
        record(db, kind, row, 0, REASON_TRACKING_OFF, user_id=user_id, note=note)
    elif before != stock:
        record(db, kind, row, (stock or 0) - (before or 0), REASON_SET,
               user_id=user_id, note=note)

    db.commit()
    db.refresh(row)
    return row


def adjust(
    db: Session,
    kind: str,
    item_id: int,
    delta: int,
    reason: str,
    *,
    user_id: int | None = None,
    note: str | None = None,
):
    """
    Move a count by a relative amount — six more arrived, two were dropped.

    Relative rather than absolute because that is how the information reaches
    her: she knows a tray held twelve and four burnt, not what the system
    thinks the total was before she started.
    """
    if reason not in MANUAL_REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"Reason must be one of: {', '.join(MANUAL_REASONS)}.",
        )
    if delta == 0:
        raise HTTPException(status_code=400, detail="Nothing to change.")

    row = resolve(db, kind, item_id)
    if row.stock is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{getattr(row, 'name', 'This item')} is not counted, so there is no "
                "number to add to. Set a count first."
            ),
        )

    # Floored at zero rather than refused. A stock count is a record of the
    # shelf, and if she says four were dropped out of a tray the system thinks
    # held three, the shelf is empty either way — refusing the correction would
    # leave the count wrong on purpose.
    row.stock = max(0, row.stock + delta)
    record(db, kind, row, delta, reason, user_id=user_id, note=note)
    db.commit()
    db.refresh(row)
    return row


def set_sold_out(
    db: Session,
    kind: str,
    item_id: int,
    sold_out: bool,
    *,
    user_id: int | None = None,
    note: str | None = None,
):
    """
    The switch, for the things nobody counts.

    Recorded with a delta of 0: no units moved, but the shop stopped selling
    something, and "why was red velvet unorderable on Saturday" is exactly the
    kind of question the ledger exists to answer.
    """
    row = resolve(db, kind, item_id)
    if bool(row.sold_out) != bool(sold_out):
        row.sold_out = bool(sold_out)
        record(
            db, kind, row, 0,
            REASON_MARKED_OUT if sold_out else REASON_MARKED_BACK,
            user_id=user_id, note=note,
        )
        db.commit()
        db.refresh(row)
    return row


# ── Reading it back ──────────────────────────────────────────────────────


def _unpaid_holds(db: Session) -> dict[tuple[str, int], dict]:
    """
    What is being held by orders nobody has paid for.

    These are the two abandoned PayU attempts that sat at the top of the order
    queue in red — they reserved their units at creation and will hold them
    until somebody cancels them. Nothing expires them automatically, so this is
    the number that makes a squat visible instead of leaving a count that looks
    wrong for no reason.

    "Nobody has paid" mirrors is_payable: PAID, or a legacy COD row that was
    grandfathered when cash on delivery was withdrawn. Expressed in SQL rather
    than by calling is_payable per order, because this runs for the whole
    catalogue on one screen.
    """
    rows = (
        db.query(
            StockMovement.kind,
            StockMovement.item_id,
            func.sum(StockMovement.delta),
            func.count(func.distinct(StockMovement.order_id)),
            func.min(StockMovement.order_id),
        )
        .join(Order, Order.id == StockMovement.order_id)
        .filter(
            StockMovement.reason == REASON_SOLD,
            Order.status == OrderStatus.RECEIVED,
            Order.payment_status != PaymentStatus.PAID,
            or_(Order.payment_method.is_(None), func.upper(Order.payment_method) != "COD"),
        )
        .group_by(StockMovement.kind, StockMovement.item_id)
        .all()
    )
    out: dict[tuple[str, int], dict] = {}
    for kind, item_id, total, orders, first_order in rows:
        units = -int(total or 0)
        if units > 0:
            out[(kind, item_id)] = {
                "units": units,
                "orders": int(orders or 0),
                "first_order_id": first_order,
            }
    return out


def _row_state(kind: str, row, holds: dict) -> dict:
    """One item, as the inventory screen needs it."""
    hold = holds.get((kind, row.id)) or {}
    stock = row.stock
    return {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "id": row.id,
        "name": getattr(row, "name", f"{kind} {row.id}"),
        "image_url": getattr(row, "image_url", None),
        "stock": stock,
        "is_counted": stock is not None,
        "sold_out": bool(row.sold_out),
        "is_sold_out": bool(row.is_sold_out),
        # Low is about a count running down, so an uncounted row is never low
        # and neither is one that has already reached zero — that is out, which
        # is a different badge and a different kind of urgency.
        "is_low": stock is not None and 0 < stock <= LOW_STOCK_AT,
        "is_active": bool(getattr(row, "is_active", True)),
        # Only products have this, and it is the switch that takes something
        # off the menu rather than marking it sold out. Carried so the screen
        # can say which of the two is true.
        "is_available": bool(getattr(row, "is_available", True)),
        "held_by_unpaid": hold.get("units", 0),
        "held_by_orders": hold.get("orders", 0),
        "held_first_order_id": hold.get("first_order_id"),
    }


def snapshot(db: Session, *, include_inactive: bool = False) -> list[dict]:
    """
    Everything that can run out, as one list.

    Products, extras and add-ons together, because that is how the shop thinks
    about it: what am I out of. Three screens with a count on each is how the
    add-on count ended up buried in a tab of the product editor where nobody
    looked at it.

    Sorted by how much it wants attention — out first, then low, then counted,
    then everything else alphabetically. The screen does not re-sort; this
    order is the answer to the question the screen is asking.
    """
    holds = _unpaid_holds(db)
    items: list[dict] = []

    for kind, model in _MODELS.items():
        q = db.query(model)
        if not include_inactive and hasattr(model, "is_active"):
            q = q.filter(model.is_active == True)  # noqa: E712
        for row in q.all():
            # The base and premium rows Build a Cake quotes from are prices,
            # not things on a shelf. Counting them would be meaningless and
            # marking one sold out would take the whole builder down.
            if kind == KIND_PRODUCT and (row.is_pricing_row or row.is_placeholder):
                continue
            items.append(_row_state(kind, row, holds))

    def rank(item: dict) -> tuple:
        if item["is_sold_out"]:
            bucket = 0
        elif item["is_low"]:
            bucket = 1
        elif item["is_counted"]:
            bucket = 2
        else:
            bucket = 3
        return (bucket, item["name"].lower(), item["kind"])

    items.sort(key=rank)
    return items


def counts(db: Session) -> dict:
    """
    How many items are out and how many are nearly out, across all three kinds.

    For the dashboard. Computed from the same snapshot the inventory screen
    reads so the tile cannot say three while the screen shows four.
    """
    items = snapshot(db)
    return {
        "out_of_stock_items": sum(1 for i in items if i["is_sold_out"]),
        "low_stock_items": sum(1 for i in items if i["is_low"]),
        "held_by_unpaid_units": sum(i["held_by_unpaid"] for i in items),
    }


def movements(
    db: Session,
    *,
    kind: str | None = None,
    item_id: int | None = None,
    limit: int = 100,
) -> list[StockMovement]:
    """The ledger, newest first, optionally for one item."""
    q = db.query(StockMovement)
    if kind:
        q = q.filter(StockMovement.kind == kind)
    if item_id is not None:
        q = q.filter(StockMovement.item_id == item_id)
    return (
        q.order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
