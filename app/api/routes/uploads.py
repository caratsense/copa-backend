"""
File Upload Routes — product images, etc.
Files are stored locally. Replace with S3 when ready.
"""

import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.product import Product
from app.core.auth import require_admin
from app.config import get_settings

router = APIRouter(prefix="/uploads", tags=["Uploads"])
settings = get_settings()


def _ensure_upload_dir():
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)


@router.post("/product-image/{product_id}")
def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Upload an image for a product. Overwrites existing image."""

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Validate file type
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail=f"File type not allowed. Use: JPEG, PNG, or WebP")

    # Validate size
    contents = file.file.read()
    if len(contents) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"File too large. Max: {settings.MAX_UPLOAD_MB}MB")

    # Save file
    _ensure_upload_dir()
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"product_{product_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(settings.UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(contents)

    # Update product
    product.image_url = f"/media/{filename}"
    db.commit()
    db.refresh(product)

    return {"image_url": product.image_url, "filename": filename}
