"""
Reviews Routes
- Customer: submit review after order is delivered
- Public: see approved/featured reviews (for spotlight carousel)
- Admin: approve, feature, reply, delete
"""

import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.models.review import Review
from app.core.auth import get_current_user, require_admin
from app.config import get_settings
from app.schemas import ReviewCreate, ReviewRead, ReviewAdminAction

router = APIRouter(prefix="/reviews", tags=["Reviews"])
settings = get_settings()


# ─── PUBLIC ───────────────────────────────────────────

@router.get("/featured", response_model=list[ReviewRead])
def featured_reviews(limit: int = 10, db: Session = Depends(get_db)):
    """Public — get featured reviews for the customer spotlight carousel."""
    reviews = (
        db.query(Review)
        .filter(Review.is_approved == True, Review.is_featured == True)
        .order_by(Review.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_review_with_name(r) for r in reviews]


@router.get("/approved", response_model=list[ReviewRead])
def approved_reviews(skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    """Public — all approved reviews."""
    reviews = (
        db.query(Review)
        .filter(Review.is_approved == True)
        .order_by(Review.created_at.desc())
        .offset(skip).limit(limit)
        .all()
    )
    return [_review_with_name(r) for r in reviews]


@router.get("/product/{product_id}")
def product_reviews(product_id: int, skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    """Public — approved reviews for a specific product."""
    from sqlalchemy import func as sqlfunc
    reviews = (
        db.query(Review)
        .filter(Review.product_id == product_id, Review.is_approved == True)
        .order_by(Review.created_at.desc())
        .offset(skip).limit(limit)
        .all()
    )
    # Stats for this product
    stats_result = db.query(
        sqlfunc.count(Review.id),
        sqlfunc.coalesce(sqlfunc.avg(Review.rating), 0.0),
    ).filter(Review.product_id == product_id, Review.is_approved == True).first()

    return {
        "reviews": [_review_with_name(r) for r in reviews],
        "total": stats_result[0],
        "average_rating": round(float(stats_result[1]), 1),
    }


@router.get("/stats", response_model=dict)
def review_stats(db: Session = Depends(get_db)):
    """Public — average rating and total count."""
    from sqlalchemy import func
    result = db.query(
        func.count(Review.id),
        func.coalesce(func.avg(Review.rating), 0.0),
    ).filter(Review.is_approved == True).first()

    return {
        "total_reviews": result[0],
        "average_rating": round(float(result[1]), 1),
    }


# ─── CUSTOMER ─────────────────────────────────────────

@router.post("", response_model=ReviewRead, status_code=201)
def submit_review(
    data: ReviewCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Customer submits a review for a product or delivered order."""
    review = Review(
        user_id=user.id,
        rating=data.rating,
        comment=data.comment,
        product_id=getattr(data, 'product_id', None),
        is_approved=True,
    )

    # If order_id provided, validate it
    if data.order_id and data.order_id > 0:
        order = db.query(Order).filter(Order.id == data.order_id).first()
        if order and order.user_id == user.id:
            review.order_id = data.order_id

    db.add(review)
    db.commit()
    db.refresh(review)
    return _review_with_name(review)


@router.post("/{review_id}/image")
def upload_review_image(
    review_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Customer uploads a photo with their review."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    if review.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not your review")

    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Use JPEG, PNG, or WebP")

    contents = file.file.read()
    if len(contents) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Max {settings.MAX_UPLOAD_MB}MB")

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"review_{review_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(settings.UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(contents)

    review.image_url = f"/media/{filename}"
    db.commit()
    return {"image_url": review.image_url}


# ─── ADMIN ────────────────────────────────────────────

@router.get("/pending", response_model=list[ReviewRead])
def pending_reviews(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin — see reviews waiting for approval."""
    reviews = db.query(Review).filter(Review.is_approved == False).order_by(Review.created_at.desc()).all()
    return [_review_with_name(r) for r in reviews]


@router.get("/all", response_model=list[ReviewRead])
def all_reviews(skip: int = 0, limit: int = 50, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin — see all reviews."""
    reviews = db.query(Review).order_by(Review.created_at.desc()).offset(skip).limit(limit).all()
    return [_review_with_name(r) for r in reviews]


@router.patch("/{review_id}", response_model=ReviewRead)
def moderate_review(
    review_id: int,
    data: ReviewAdminAction,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin approves, features, or replies to a review."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    if data.is_approved is not None:
        review.is_approved = data.is_approved
    if data.is_featured is not None:
        review.is_featured = data.is_featured
    if data.admin_reply is not None:
        review.admin_reply = data.admin_reply

    db.commit()
    db.refresh(review)
    return _review_with_name(review)


@router.delete("/{review_id}", status_code=204)
def delete_review(review_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    review = db.query(Review).filter(Review.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    db.delete(review)
    db.commit()


# ─── HELPER ──────────────────────────────────────────

def _review_with_name(review: Review) -> ReviewRead:
    """Build ReviewRead with customer name resolved."""
    return ReviewRead(
        id=review.id,
        order_id=review.order_id,
        product_id=review.product_id,
        user_id=review.user_id,
        customer_name=review.user.name if review.user else "Anonymous",
        rating=review.rating,
        comment=review.comment,
        image_url=review.image_url,
        is_approved=review.is_approved,
        is_featured=review.is_featured,
        admin_reply=review.admin_reply,
        created_at=review.created_at,
    )
