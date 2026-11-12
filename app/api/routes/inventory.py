"""
Inventory — what the shop has, what it is out of, and what happened to it.

There was no screen for this. The only count in the system was an add-on's,
and it lived in a tab of the product editor behind a button marked "Set Stock",
which is why nobody saw a topper run down to zero until a customer asked where
the photo prints had gone. Products had an on/off switch that removed them
from the menu, and extras had nothing at all.

Every write here goes through app/services/stock_service.py, which is also
what create_order uses. That is deliberate: a count changed by one path and
not recorded by another is how a ledger stops being worth reading.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas import (
    InventoryItem,
    SoldOutUpdate,
    StockAdjust,
    StockLevelUpdate,
    StockMovementRead,
)
from app.services import stock_service

router = APIRouter(prefix="/admin/inventory", tags=["Admin — Inventory"])


@router.get("", response_model=list[InventoryItem])
def list_inventory(
    include_inactive: bool = Query(
        False,
        description="Also return rows that are switched off entirely.",
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Everything that can run out, in the order it wants attention.

    Out of stock first, then nearly out, then the rest of what is counted,
    then everything uncounted. The screen does not re-sort — this order IS the
    answer to the question the screen is asking, and a sort control would only
    let somebody hide the thing they most need to see.
    """
    return stock_service.snapshot(db, include_inactive=include_inactive)


@router.get("/movements", response_model=list[StockMovementRead])
def list_movements(
    kind: str | None = Query(None, description="product | extra | addon"),
    item_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    The ledger, newest first.

    Without `item_id` this is the whole shop's stock history, which is what
    you want when a count looks wrong and you do not yet know which item is
    the problem.
    """
    if kind:
        stock_service.model_for(kind)       # 400s on a kind that does not exist
    return stock_service.movements(db, kind=kind, item_id=item_id, limit=limit)


@router.patch("/{kind}/{item_id}/level", response_model=InventoryItem)
def set_level(
    kind: str,
    item_id: int,
    data: StockLevelUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Set a count, or send `stock: null` to stop counting this item.

    Not counting is a real answer and the right one for most of the catalogue:
    a cake is made to order, so a number on it is a number somebody has to
    keep true for no benefit — and a count nobody maintains is worse than
    none, because the menu starts refusing orders the kitchen could have made.
    """
    row = stock_service.set_level(
        db, kind, item_id, data.stock, user_id=admin.id,
    )
    return _one(db, kind, row)


@router.post("/{kind}/{item_id}/adjust", response_model=InventoryItem)
def adjust(
    kind: str,
    item_id: int,
    data: StockAdjust,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Add to or take from a count: six more arrived, four were dropped.

    The reason is required because the ledger is the point. "The count went
    from 12 to 8" answers nothing on its own; "four went in the bin" is the
    line that makes next month's wastage visible.
    """
    row = stock_service.adjust(
        db, kind, item_id, data.delta, data.reason,
        user_id=admin.id, note=data.note,
    )
    return _one(db, kind, row)


@router.patch("/{kind}/{item_id}/sold-out", response_model=InventoryItem)
def set_sold_out(
    kind: str,
    item_id: int,
    data: SoldOutUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Mark something sold out, or put it back on sale.

    For the things that have no count — a cake she cannot do today because the
    red velvet sponge is gone. It stays sold out until she clears it: a
    delivery of cream does not tell us she is ready to take the order again,
    and guessing that she is would put a cake back on the menu she cannot make.
    """
    row = stock_service.set_sold_out(
        db, kind, item_id, data.sold_out, user_id=admin.id, note=data.note,
    )
    return _one(db, kind, row)


def _one(db: Session, kind: str, row) -> dict:
    """
    The item as the list serves it, so a write and a read agree.

    Built from the snapshot rather than from the row, because `held_by_unpaid`
    is a query across the ledger and the screen replaces its row with whatever
    comes back from the write. Returning a bare row would blank that number
    and make a squat disappear the moment somebody touched the count.
    """
    for item in stock_service.snapshot(db, include_inactive=True):
        if item["kind"] == kind and item["id"] == row.id:
            return item
    # A pricing row, or one that snapshot deliberately leaves out. Answer with
    # the row's own state rather than a 404 for something that was just written.
    return stock_service._row_state(kind, row, {})
