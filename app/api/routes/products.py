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

# The tag itself lives on the model, beside the property that reads it.
from app.models.product import PLACEHOLDER_PRICE_TAG  # noqa: E402

PLACEHOLDER_PRICE = 1.0


def _is_placeholder_priced(product: Product) -> bool:
    return product.is_placeholder


def _clear_placeholder_tag(product: Product) -> None:
    """A real price has been set, so the row is no longer a placeholder."""
    product.tags = [t for t in (product.tags or []) if t != PLACEHOLDER_PRICE_TAG]


def _refuse_if_placeholder_priced(product: Product) -> None:
    """
    Do not let a product go on sale at its placeholder price.

    The placeholder exists so the full catalogue can be demonstrated and
    managed before the client has priced everything; it is deliberately not a
    price, and selling at it would be selling a cake for a rupee. One click on
    the availability toggle is all that would take, so the refusal lives here
    rather than in a convention nobody can enforce.

    This is not a trap: setting any real price clears the tag in the same
    request, and the product publishes normally afterwards.
    """
    if _is_placeholder_priced(product):
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{product.name}' is still at its placeholder price. Set the "
                f"real price first - saving a price clears the placeholder and "
                f"lets the product be published."
            ),
        )


# ─── PUBLIC ───────────────────────────────────────────

@router.get("", response_model=list[ProductRead])
def list_products(
    category: str | None = None,
    available_only: bool = True,
    include_placeholders: bool = False,
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

    `include_placeholders` additionally returns products the client has not
    priced yet, so the full catalogue can be reviewed before every price is in.
    They come back with `is_placeholder: true` and `is_available: false` - the
    flag is for display, and the availability is what stops them being ordered.
    It is opt-in: a caller that does not ask for them sees exactly what it saw
    before.
    """
    q = db.query(Product)
    # Availability is NOT relaxed here. Placeholders are added back after the
    # filter by their own flag, so "available" keeps meaning "orderable" and no
    # other unavailable product - one the owner has paused, say - comes with
    # them.
    if available_only and not include_placeholders:
        q = q.filter(Product.is_available == True)
    if category:
        q = q.filter(Product.category == category)
    # There was no ORDER BY at all, so the database was free to return rows in
    # any order and to change that order between identical requests. With
    # offset pagination layered on top, that can repeat a product on one page
    # and skip it on the next. id breaks ties so the order is total, not merely
    # grouped by sort_order.
    q = q.order_by(Product.sort_order, Product.id)

    if available_only and include_placeholders:
        # Whether a product is a placeholder is a fact about its tags, and the
        # JSONB containment operator that would express it in SQL is
        # PostgreSQL-only. Selecting in Python keeps one behaviour across both
        # databases; the catalogue is ~50 rows, and this branch only runs when
        # a caller explicitly asks for placeholders.
        rows = [p for p in q.all() if p.is_available or p.is_placeholder]
        end = None if limit is None else skip + limit
        return rows[skip:end]

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

    fields = data.model_dump(exclude_unset=True)

    # A real price has been supplied, so this is no longer a placeholder.
    # Done before the availability check below, so one save can both price a
    # product and publish it - which is what an admin filling in a price
    # actually wants.
    if "base_price" in fields and fields["base_price"] != PLACEHOLDER_PRICE:
        _clear_placeholder_tag(product)

    # An explicit `tags` in the same request is the admin's own list and wins;
    # clearing the marker by hand is a legitimate way to say "this Rs 1 is
    # real".
    for field, value in fields.items():
        setattr(product, field, value)

    if fields.get("is_available") is True:
        _refuse_if_placeholder_priced(product)

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
    # Taking a product OFF sale is always allowed; only putting one ON sale at
    # a placeholder price is refused.
    if not product.is_available:
        _refuse_if_placeholder_priced(product)
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
