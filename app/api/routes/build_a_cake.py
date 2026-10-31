"""
Build a Cake — sponge first, then flavour, then size.

Nothing here prices anything. The options endpoint reads the same rule tables
the rest of the shop uses, and the price comes from POST /pricing/calculate and
then from create_order, so a quote and a charge cannot disagree and there is no
second pricing path to keep in step.

The two ways out of the flow are deliberate:

  * a size the shop sells  -> the normal cart and checkout, unchanged
  * anything else          -> an enquiry, which is a conversation and never
                              becomes an order by itself

"Anything else" is a cake over 3.5kg, or a customisation the builder has no
rule for. Both have the same problem - no price exists - and inventing one is
the failure this separation prevents.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin
from app.core.ratelimit import limiter
from app.core.uploads import ALLOWED_TYPES, save_image
from app.db import get_db
from app.models.cake_enquiry import CakeEnquiry
from app.models.pricing import AddonRule, DesignRule, FlavorRule, RushRule, SizeRule
from app.models.product import Product
from app.models.user import User
from app.services.pricing_engine import MIN_CUSTOM_CAKE_KG
from app.schemas import (
    AddonRuleRead, BuildFlavorRead, BuildOptionsRead, BuildSizeRead,
    CakeEnquiryCreate, CakeEnquiryRead, DesignRuleRead, EnquiryImageRead,
    RushRuleRead, SpongeRead,
)

router = APIRouter(prefix="/build-a-cake", tags=["Build a Cake"])

# Above this a cake is quoted by a person. Kept beside the flow that enforces
# it; the size table is trimmed to match by scripts/setup_build_a_cake.py, so
# an oversized cake is unorderable by the pricing engine too, not just here.
MAX_ORDERABLE_KG = 3.5

# And below this a cake is not customised at all - there is not enough of it to
# carry a design. Imported from the pricing engine, which refuses a smaller one
# outright, so the sizes offered here and the sizes that can actually be bought
# come from the same number. The catalogue's own per-kg cakes keep their 500g;
# this floor applies only to the builder's sponge rows.
MIN_ORDERABLE_KG = MIN_CUSTOM_CAKE_KG

# The product each sponge is priced from. The premium products are deliberately
# absent: a premium cake is the base sponge plus a premium flavour's surcharge
# (2000 + 250 = 2250, which is exactly what "Vanilla Premium Cake" costs), so
# offering both would be two routes to one cake at one price, and a customer
# picking the premium product AND a premium flavour would be charged the
# surcharge twice.
SPONGES = (
    ("vanilla", "Vanilla Sponge", "Vanilla Base Cake"),
    ("chocolate", "Chocolate Sponge", "Chocolate Base Cake"),
)

# How long after an enquiry is created its picture may still be attached.
#
# This upload cannot ask who is calling - a customer may be enquiring before
# they have an account - so the enquiry id is the only thing between a stranger
# and someone else's request, and ids are sequential. A short window means a
# guessed id is almost always already closed. Long enough that a customer who
# picks a photo, thinks about it and sends it still gets through.
IMAGE_WINDOW_MINUTES = 30

# Saving goes through app.core.uploads.save_image, which names the file from
# what its bytes are, never from the filename the browser sent. UPLOAD_DIR is
# served by StaticFiles, which types a file by its extension: a "picture" called
# design.html would otherwise come back from /media as a page on the API's own
# origin, and design.svg as a document that can carry script.


def _sponge_products(db: Session) -> list[SpongeRead]:
    out = []
    for key, label, product_name in SPONGES:
        product = (
            db.query(Product)
            .filter(Product.name == product_name, Product.is_available == True)
            .first()
        )
        if product is None:
            # Say which sponge is unavailable rather than returning a short
            # list that looks deliberate.
            raise HTTPException(
                status_code=503,
                detail=(
                    f"The {label} is not available to build right now. "
                    f"Please contact us and we'll help."
                ),
            )
        out.append(SpongeRead(sponge=key, label=label, product_id=product.id,
                              base_price=float(product.base_price)))
    return out


@router.get("/options", response_model=BuildOptionsRead)
def build_options(db: Session = Depends(get_db)):
    """
    Public — everything the builder needs, in one call.

    Every active flavour is returned, each carrying the sponge it is made on,
    so the client filters locally and correctly. It is not filtered server-side
    by sponge because the customer changes their mind: switching sponge should
    not cost a round trip, and the answer does not depend on who is asking.

    Sizes are only those the shop actually sells. Anything heavier is not
    missing from this list by accident - it is an enquiry.
    """
    flavors = (
        db.query(FlavorRule)
        .filter(FlavorRule.is_active == True, FlavorRule.sponge.isnot(None))
        .order_by(FlavorRule.extra_cost.asc(), FlavorRule.name.asc())
        .all()
    )
    sizes = (
        db.query(SizeRule)
        .filter(SizeRule.is_active == True,
                SizeRule.multiplier >= MIN_ORDERABLE_KG,
                SizeRule.multiplier <= MAX_ORDERABLE_KG)
        .order_by(SizeRule.multiplier.asc())
        .all()
    )
    return BuildOptionsRead(
        sponges=_sponge_products(db),
        flavors=[BuildFlavorRead.model_validate(f) for f in flavors],
        sizes=[BuildSizeRead.model_validate(s) for s in sizes],
        designs=[DesignRuleRead.model_validate(d) for d in
                 db.query(DesignRule).filter(DesignRule.is_active == True).all()],
        addons=[AddonRuleRead.model_validate(a) for a in
                db.query(AddonRule).filter(AddonRule.is_active == True).all()],
        rush=[RushRuleRead.model_validate(r) for r in
              db.query(RushRule).filter(RushRule.is_active == True).all()],
        max_orderable_kg=MAX_ORDERABLE_KG,
    )


@router.get("/flavors", response_model=list[BuildFlavorRead])
def flavors_for_sponge(sponge: str, db: Session = Depends(get_db)):
    """
    Public — the flavours made on one sponge.

    A convenience over /options for callers that want the server to do the
    filtering. An unknown sponge is rejected rather than answered with an empty
    list, which would read as "no flavours" instead of "no such sponge".
    """
    known = {key for key, _, _ in SPONGES}
    if sponge not in known:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown sponge '{sponge}'. Choose one of: {', '.join(sorted(known))}.",
        )
    rows = (
        db.query(FlavorRule)
        .filter(FlavorRule.is_active == True, FlavorRule.sponge == sponge)
        .order_by(FlavorRule.extra_cost.asc(), FlavorRule.name.asc())
        .all()
    )
    return [BuildFlavorRead.model_validate(r) for r in rows]


# ─── CONTACT US ───────────────────────────────────────


@router.post("/enquiries", response_model=CakeEnquiryRead, status_code=201)
# Open to the public, and every enquiry sends each admin a WhatsApp alert -
# a business-initiated template, which Meta charges for.
@limiter.limit("5/minute;20/hour")
def create_enquiry(request: Request, data: CakeEnquiryCreate, db: Session = Depends(get_db)):
    """
    Public — a cake the shop cannot price automatically.

    Over 3.5kg, or a customisation with no rule. This creates an ENQUIRY: no
    order, no total, no payment. Someone reads it and replies.

    Not authenticated, because a customer may be asking before they have an
    account. The phone number is validated and normalised like any other, so
    the reply can actually reach them.
    """
    enquiry = CakeEnquiry(
        contact_name=data.contact_name.strip(),
        contact_phone=data.contact_phone,
        sponge=data.sponge,
        flavor=(data.flavor or "").strip() or None,
        requested_size=(data.requested_size or "").strip() or None,
        details=(data.details or "").strip() or None,
    )
    db.add(enquiry)
    db.commit()
    db.refresh(enquiry)

    _notify_admins(db, enquiry)
    return enquiry


@router.post("/enquiries/{enquiry_id}/image", response_model=EnquiryImageRead)
@limiter.limit("5/minute")
def upload_reference_image(
    request: Request,
    enquiry_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Attach the customer's reference photograph to their enquiry.

    Same mechanism as review photos: written to UPLOAD_DIR and served from
    /media, so there is one upload path and one place images live rather than a
    second one built for this flow.

    Open, because the enquiry that owns it is open - but an unauthenticated
    write has to be narrow, so this one is. One picture per enquiry, within
    half an hour of it being made, and the reply says only where the picture
    went. It cannot be used to read an enquiry back: the customer's name, phone
    number and request are not in the response, so walking the ids returns
    nothing worth walking them for. A second upload is refused rather than
    overwriting, which also stops one id being used as free storage.

    An enquiry that is missing, already has its picture, or has closed its
    window all answer the same 404, so the reply does not say which ids exist.
    """
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Use JPEG, PNG, or WebP")

    enquiry = db.query(CakeEnquiry).filter(CakeEnquiry.id == enquiry_id).first()
    closed = "Enquiry not found, or it is no longer accepting a picture."
    if not enquiry or enquiry.reference_image_url:
        raise HTTPException(status_code=404, detail=closed)

    # created_at comes back timezone-aware from Postgres and naive-UTC from
    # SQLite, so it is normalised before the arithmetic rather than assumed.
    created = enquiry.created_at
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=IMAGE_WINDOW_MINUTES):
            raise HTTPException(status_code=404, detail=closed)

    enquiry.reference_image_url = save_image(file, f"enquiry_{enquiry_id}")
    db.commit()
    return EnquiryImageRead(reference_image_url=enquiry.reference_image_url)


@router.get("/enquiries", response_model=list[CakeEnquiryRead])
def list_enquiries(
    handled: bool | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin — the enquiries waiting for a reply."""
    q = db.query(CakeEnquiry)
    if handled is not None:
        q = q.filter(CakeEnquiry.is_handled == handled)
    return q.order_by(CakeEnquiry.created_at.desc(), CakeEnquiry.id.desc()).all()


@router.patch("/enquiries/{enquiry_id}/handled", response_model=CakeEnquiryRead)
def mark_handled(
    enquiry_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Admin — mark an enquiry as dealt with.

    Deliberately the only state change available. There is no "convert to
    order" here: agreeing a price for a cake nobody has quoted is a human
    decision, and the order that follows is placed the normal way.
    """
    enquiry = db.query(CakeEnquiry).filter(CakeEnquiry.id == enquiry_id).first()
    if not enquiry:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    enquiry.is_handled = True
    db.commit()
    db.refresh(enquiry)
    return enquiry


def _notify_admins(db: Session, enquiry: CakeEnquiry) -> None:
    """
    Tell the shop an enquiry arrived, through the existing outbox.

    Best-effort: the enquiry is already saved and visible in the admin list, so
    a WhatsApp failure must not lose the customer's request or fail their
    request back to them.
    """
    try:
        from app.models.user import UserRole
        from app.services import wa_outbox
        from app.services.wa_consent import may_message

        summary = " | ".join(part for part in (
            f"Sponge: {enquiry.sponge}" if enquiry.sponge else "",
            f"Flavour: {enquiry.flavor}" if enquiry.flavor else "",
            f"Size: {enquiry.requested_size}" if enquiry.requested_size else "",
            f"Details: {enquiry.details}" if enquiry.details else "",
            "Reference image attached" if enquiry.reference_image_url else "",
        ) if part) or "No details given"

        for admin in db.query(User).filter(User.role == UserRole.ADMIN).all():
            allowed = may_message(admin) and bool(admin.phone)
            wa_outbox.queue_and_send(
                db,
                template_key="admin_new_order",
                recipient=admin.phone or "",
                params=[
                    f"ENQ-{enquiry.id}",
                    f"{enquiry.contact_name} ({enquiry.contact_phone})",
                    summary,
                    "Quote required",
                    "Contact Us request",
                ],
                recipient_role="admin",
                event_type="cake_enquiry",
                skip_reason=None if allowed else "no opt-in on record",
            )
    except Exception as exc:  # pragma: no cover - notification must not fail the request
        import logging
        logging.getLogger(__name__).error("[ENQUIRY] notify failed: %s", exc)
