"""
Ingredients Routes
- Public: list active ingredients (for carousel/grid)
- Admin: full CRUD + image upload
"""

import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.ingredient import Ingredient
from app.core.auth import require_admin
from app.config import get_settings
from app.schemas import IngredientCreate, IngredientUpdate, IngredientRead

router = APIRouter(prefix="/ingredients", tags=["Ingredients"])
settings = get_settings()


# ─── PUBLIC ───────────────────────────────────────────

@router.get("", response_model=list[IngredientRead])
def list_ingredients(
    category: str | None = None,
    premium_only: bool = False,
    db: Session = Depends(get_db),
):
    """Public — list ingredients for the showcase carousel."""
    q = db.query(Ingredient).filter(Ingredient.is_active == True)
    if category:
        q = q.filter(Ingredient.category == category)
    if premium_only:
        q = q.filter(Ingredient.is_premium == True)
    return q.order_by(Ingredient.sort_order.asc(), Ingredient.name.asc()).all()


@router.get("/{ingredient_id}", response_model=IngredientRead)
def get_ingredient(ingredient_id: int, db: Session = Depends(get_db)):
    item = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    return item


# ─── ADMIN ────────────────────────────────────────────

@router.post("", response_model=IngredientRead, status_code=201)
def create_ingredient(data: IngredientCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = Ingredient(**data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/{ingredient_id}", response_model=IngredientRead)
def update_ingredient(ingredient_id: int, data: IngredientUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/{ingredient_id}/toggle", response_model=IngredientRead)
def toggle_ingredient(ingredient_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    item.is_active = not item.is_active
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{ingredient_id}", status_code=204)
def delete_ingredient(ingredient_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    db.delete(item)
    db.commit()


@router.post("/{ingredient_id}/image")
def upload_ingredient_image(
    ingredient_id: int,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Upload image for an ingredient."""
    item = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Ingredient not found")

    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Use JPEG, PNG, or WebP")

    contents = file.file.read()
    if len(contents) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Max {settings.MAX_UPLOAD_MB}MB")

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"ingredient_{ingredient_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(settings.UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(contents)

    item.image_url = f"/media/{filename}"
    db.commit()
    return {"image_url": item.image_url}
