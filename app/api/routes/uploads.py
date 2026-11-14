"""
File Upload Routes — product images, etc.
Files are stored locally. Replace with S3 when ready.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.product import Product
from app.core.auth import require_admin
from app.core.ratelimit import limiter
from app.core.uploads import new_reference_prefix, save_image
from app.schemas import DesignReferenceRead

router = APIRouter(prefix="/uploads", tags=["Uploads"])


@router.post("/design-reference", response_model=DesignReferenceRead)
@limiter.limit("6/minute;40/hour")
def upload_design_reference(request: Request, file: UploadFile = File(...)):
    """
    A picture of the cake the customer wants, staged before the order exists.

    Build a Cake calls this the moment the customer picks a file. The URL it
    returns travels in the cart and is claimed onto the order at checkout. The
    customer need not be signed in: this is step three of five on a page
    anybody can open, and demanding a login there would either lose the
    picture or stop the sale.

    Which is also why it is the only write in the app that is open AND has no
    row of its own to be gated by, so the gate is the shape of what it accepts.
    save_image takes the extension from the bytes rather than the filename
    (UPLOAD_DIR is served by StaticFiles, so a ".jpg" full of HTML would come
    back as a live document on this origin), the size is capped by
    MAX_UPLOAD_MB, and the rate limit above is per real client address — see
    app/core/ratelimit.py for why that is X-Real-IP on Railway and not
    request.client.host, which rotates and would never fill a bucket.

    No database, no row, nothing to read back: the response is the URL. A file
    that never gets claimed by an order is left where it is. Deleting it would
    mean guessing whether a customer who has been on the checkout page for
    twenty minutes has given up, and the cost of guessing wrong is their
    picture vanishing from an order they are about to place.
    """
    return DesignReferenceRead(url=save_image(file, new_reference_prefix()))


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
