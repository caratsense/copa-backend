"""
Product Routes
- Public: list/get available products
- Admin: create, update, toggle availability, delete
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.product import Product
from app.core.auth import require_admin
from app.schemas import ProductCreate, ProductUpdate, ProductRead

router = APIRouter(prefix="/products", tags=["Products"])


# ─── PUBLIC ───────────────────────────────────────────

@router.get("", response_model=list[ProductRead])
def list_products(
    category: str | None = None,
    available_only: bool = True,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """List products — public. Defaults to only available products."""
    q = db.query(Product)
    if available_only:
        q = q.filter(Product.is_available == True)
    if category:
        q = q.filter(Product.category == category)
    return q.offset(skip).limit(limit).all()


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
