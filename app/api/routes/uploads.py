"""
File Upload Routes — product images, etc.
Files are stored locally. Replace with S3 when ready.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.product import Product
from app.core.auth import require_admin
from app.core.uploads import save_image

router = APIRouter(prefix="/uploads", tags=["Uploads"])


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

    product.image_url = save_image(file, f"product_{product_id}")
    db.commit()
    db.refresh(product)

    return {"image_url": product.image_url, "filename": product.image_url.rsplit("/", 1)[-1]}
