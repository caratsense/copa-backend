"""
Product Routes
- Public: list/get available products
- Admin: create, update, toggle availability, delete
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.product import Product
from app.core.auth import require_admin
from app.models.product_option import ProductOption
from app.schemas import (
    ProductCreate, ProductUpdate, ProductRead,
    ProductOptionCreate, ProductOptionRead, ProductOptionUpdate,
)

router = APIRouter(prefix="/products", tags=["Products"])


# ─── PUBLIC ───────────────────────────────────────────

@router.get("", response_model=list[ProductRead])
def list_products(
    category: str | None = None,
    available_only: bool = True,
    skip: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=0),
    db: Session = Depends(get_db),
):
    """
    List products — public. Defaults to only available products.

    `limit` defaults to no limit: the whole matching catalogue is returned.
    It used to default to 50, which silently truncated every caller that did
    not think to ask for more - the admin product list and the cake builder
    both fetch this with no parameters, and the catalogue passed fifty products
    some time ago. A page that quietly shows the first fifty of fifty-three is
    worse than one that fails, because nothing about it looks wrong.

    Paging still works exactly as before for anyone who asks for it: pass
    `limit` (with `skip`) and you get that page, ordered deterministically by
    sort_order then id so pages cannot repeat or drop a row.
    """
    q = db.query(Product)
    if available_only:
        q = q.filter(Product.is_available == True)
    if category:
        q = q.filter(Product.category == category)
    # There was no ORDER BY at all, so the database was free to return rows in
    # any order and to change that order between identical requests. With
    # offset pagination layered on top, that can repeat a product on one page
    # and skip it on the next. id breaks ties so the order is total, not merely
    # grouped by sort_order.
    q = q.order_by(Product.sort_order, Product.id)

    q = q.offset(skip)
    if limit is not None:
        # SQLAlchemy treats .limit(None) as "no limit" anyway; spelling it out
        # so the default cannot be mistaken for an oversight.
        q = q.limit(limit)
    return q.all()


@router.get("/{product_id}", response_model=ProductRead)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


# ─── ADMIN ────────────────────────────────────────────

@router.post("", response_model=ProductRead, status_code=201)
def create_product(data: ProductCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    product = Product(**data.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@router.patch("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int,
    data: ProductUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return product


@router.patch("/{product_id}/toggle-availability", response_model=ProductRead)
def toggle_availability(
    product_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Toggle a product in/out of stock."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_available = not product.is_available
    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(product)
    db.commit()


# ─── PRODUCT OPTIONS (per-product sizes) ─────────────
# The global SizeRule table cannot say "this cake starts at 700g" or "this one
# is a Rs 800 loaf or a Rs 1,700 round". A product's own options can, and they
# replace the global sizes for that product entirely.


def _get_product(db: Session, product_id: int) -> Product:
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _normalised(label: str) -> str:
    return " ".join((label or "").split()).lower()


@router.get("/{product_id}/options", response_model=list[ProductOptionRead])
def list_product_options(
    product_id: int,
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """Public — the sizes this product is sold in."""
    product = _get_product(db, product_id)
    return [o for o in product.options if o.is_active or not active_only]


@router.post("/{product_id}/options", response_model=ProductOptionRead, status_code=201)
def create_product_option(
    product_id: int,
    data: ProductOptionCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = _get_product(db, product_id)

    # Compared case- and space-insensitively because that is how the pricing
    # engine matches a customer's choice; two options differing only in case
    # would make one of them unreachable.
    if any(_normalised(o.label) == _normalised(data.label) for o in product.options):
        raise HTTPException(
            status_code=409,
            detail=f"'{product.name}' already has an option called '{data.label}'",
        )

    option = ProductOption(product_id=product.id, **data.model_dump())
    db.add(option)
    db.commit()
    db.refresh(option)
    return option


@router.patch("/{product_id}/options/{option_id}", response_model=ProductOptionRead)
def update_product_option(
    product_id: int,
    option_id: int,
    data: ProductOptionUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    option = (
        db.query(ProductOption)
        .filter(ProductOption.id == option_id, ProductOption.product_id == product_id)
        .first()
    )
    if not option:
        raise HTTPException(status_code=404, detail="Option not found")

    fields = data.model_dump(exclude_unset=True)
    if "label" in fields:
        clash = any(
            o.id != option.id and _normalised(o.label) == _normalised(fields["label"])
            for o in option.product.options
        )
        if clash:
            raise HTTPException(
                status_code=409,
                detail=f"another option is already called '{fields['label']}'",
            )
    for field_name, value in fields.items():
        setattr(option, field_name, value)

    # Still exactly one pricing rule after the edit. Sending price to an option
    # that had a multiplier has to clear the multiplier, or the row would carry
    # both and the engine would silently prefer one.
    if "price" in fields and fields["price"] is not None:
        option.multiplier = None
    if "multiplier" in fields and fields["multiplier"] is not None:
        option.price = None
    if (option.price is None) == (option.multiplier is None):
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="an option needs exactly one of price or multiplier",
        )

    db.commit()
    db.refresh(option)
    return option


@router.delete("/{product_id}/options/{option_id}", status_code=204)
def delete_product_option(
    product_id: int,
    option_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Remove an option.

    Deleting the last option does not break the product: it falls back to the
    global sizes if it is per-kg, or to its flat base_price if it is fixed.
    Past orders are unaffected - the option they were sold under is snapshotted
    into the order line's price_breakdown.
    """
    option = (
        db.query(ProductOption)
        .filter(ProductOption.id == option_id, ProductOption.product_id == product_id)
        .first()
    )
    if not option:
        raise HTTPException(status_code=404, detail="Option not found")
    db.delete(option)
    db.commit()
