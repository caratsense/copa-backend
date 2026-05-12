"""
Menu Sections Routes
=====================
Admin creates sections like "Birthday Cakes", "Wedding Collection", etc.
Products are assigned to sections.
Public can view active sections with their products.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import os, uuid

from app.db import get_db
from app.models.menu_section import MenuSection
from app.models.product import Product
from app.models.user import User
from app.core.auth import require_admin
from app.config import get_settings

settings = get_settings()
router = APIRouter(tags=["Menu Sections"])


# ─── Schemas ──────────────────────────────────────────

class SectionCreate(BaseModel):
    name: str
    description: Optional[str] = None
    sort_order: int = 0

class SectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

class AssignProduct(BaseModel):
    product_id: int
    section_id: Optional[int] = None  # None = unassign


# ─── PUBLIC ───────────────────────────────────────────

@router.get("/menu/sections")
def get_menu_sections(db: Session = Depends(get_db)):
    """
    Public — returns active sections with their products.
    Used by the customer-facing menu page.
    """
    sections = (
        db.query(MenuSection)
        .filter(MenuSection.is_active == True)
        .order_by(MenuSection.sort_order.asc(), MenuSection.id.asc())
        .all()
    )

    result = []
    for sec in sections:
        products = [
            {
                "id": p.id,
                "name": p.name,
                "category": p.category,
                "description": p.description,
                "base_price": p.base_price,
                "image_url": p.image_url,
                "is_customizable": p.is_customizable,
                "is_available": p.is_available,
                "tags": p.tags or [],
                "sort_order": p.sort_order,
            }
            for p in sec.products
            if p.is_available
        ]
        products.sort(key=lambda x: x["sort_order"])

        result.append({
            "id": sec.id,
            "name": sec.name,
            "description": sec.description,
            "image_url": sec.image_url,
            "sort_order": sec.sort_order,
            "products": products,
        })

    # Also include products not in any section
    orphan_products = (
        db.query(Product)
        .filter(Product.section_id == None, Product.is_available == True)
        .order_by(Product.sort_order.asc(), Product.id.asc())
        .all()
    )
    if orphan_products:
        result.append({
            "id": None,
            "name": "Other Cakes",
            "description": None,
            "image_url": None,
            "sort_order": 999,
            "products": [
                {
                    "id": p.id, "name": p.name, "category": p.category,
                    "description": p.description, "base_price": p.base_price,
                    "image_url": p.image_url, "is_customizable": p.is_customizable,
                    "is_available": p.is_available, "tags": p.tags or [],
                    "sort_order": p.sort_order,
                }
                for p in orphan_products
            ],
        })

    return result


# ─── ADMIN CRUD ───────────────────────────────────────

@router.get("/admin/sections")
def list_sections(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin — list all sections with product counts."""
    sections = db.query(MenuSection).order_by(MenuSection.sort_order.asc(), MenuSection.id.asc()).all()
    return [
        {
            "id": s.id, "name": s.name, "description": s.description,
            "image_url": s.image_url, "sort_order": s.sort_order,
            "is_active": s.is_active, "product_count": len(s.products),
        }
        for s in sections
    ]


@router.post("/admin/sections", status_code=201)
def create_section(data: SectionCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    section = MenuSection(name=data.name, description=data.description, sort_order=data.sort_order)
    db.add(section)
    db.commit()
    db.refresh(section)
    return {"id": section.id, "name": section.name, "sort_order": section.sort_order}


@router.patch("/admin/sections/{section_id}")
def update_section(section_id: int, data: SectionUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    section = db.query(MenuSection).filter(MenuSection.id == section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    if data.name is not None: section.name = data.name
    if data.description is not None: section.description = data.description
    if data.sort_order is not None: section.sort_order = data.sort_order
    if data.is_active is not None: section.is_active = data.is_active
    db.commit()
    return {"id": section.id, "name": section.name, "is_active": section.is_active}


@router.delete("/admin/sections/{section_id}", status_code=204)
def delete_section(section_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    section = db.query(MenuSection).filter(MenuSection.id == section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
    # Unassign products first
    for p in section.products:
        p.section_id = None
    db.delete(section)
    db.commit()


@router.post("/admin/sections/{section_id}/image")
def upload_section_image(
    section_id: int,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    section = db.query(MenuSection).filter(MenuSection.id == section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Use JPEG, PNG, or WebP")

    contents = file.file.read()
    ext = file.filename.split(".")[-1] if file.filename else "jpg"
    filename = f"section_{section_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(settings.UPLOAD_DIR, filename)
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(contents)

    section.image_url = f"/media/{filename}"
    db.commit()
    return {"image_url": section.image_url}


@router.post("/admin/sections/assign-product")
def assign_product_to_section(data: AssignProduct, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Assign a product to a section, or unassign (section_id=None)."""
    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if data.section_id is not None:
        section = db.query(MenuSection).filter(MenuSection.id == data.section_id).first()
        if not section:
            raise HTTPException(status_code=404, detail="Section not found")
    product.section_id = data.section_id
    db.commit()
    return {"product_id": product.id, "section_id": product.section_id}
