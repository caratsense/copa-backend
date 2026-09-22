"""
Build a Cake: sponge, then flavour, then size — and the way out when it does
not fit.

The flow has exactly two endings. A cake the shop sells goes through the same
cart, pricing engine and checkout as everything else; anything else becomes an
enquiry, which is a conversation and never becomes an order on its own.

"Anything else" is a cake over 3.5kg or a customisation with no rule. Both have
the same problem - there is no price to calculate - and the separation exists
so nobody invents one.

Two details worth stating because they are easy to get wrong:

  * A flavour's sponge is stored, not read off its name. "White Chocolate
    Raspberry Pistachio" is a VANILLA-sponge flavour; so is "Vanilla Orange
    Chocolate Crumble". A name rule misclassifies both.
  * Over 3.5kg is refused by the PRICING ENGINE, not only by this flow: the
    size simply is not an active rule, so no request can order one whatever it
    posts.
"""

from app.models.cake_enquiry import CakeEnquiry
from app.models.pricing import AddonRule, DesignRule, FlavorRule, RushRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate
from app.services.order_service import create_order
from app.services.pricing_engine import calculate_item_price

from tests.conftest import auth, make_user

from fastapi import HTTPException

import pytest


VANILLA_PER_KG = 2000.0
CHOCOLATE_PER_KG = 2200.0


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


@pytest.fixture
def builder(db):
    """The catalogue the builder reads, as setup_build_a_cake leaves it."""
    db.add_all([
        Product(name="Vanilla Base Cake", category="vanilla-base",
                base_price=VANILLA_PER_KG, pricing_unit="kg",
                is_customizable=True, is_available=True),
        Product(name="Chocolate Base Cake", category="chocolate-base",
                base_price=CHOCOLATE_PER_KG, pricing_unit="kg",
                is_customizable=True, is_available=True),
        # Present in the catalogue but NOT a sponge: premium is the base sponge
        # plus a premium flavour's surcharge.
        Product(name="Vanilla Premium Cake", category="vanilla-premium",
                base_price=2250.0, pricing_unit="kg",
                is_customizable=True, is_available=True),
    ])
    for name, mult in (("500g", 0.5), ("1kg", 1.0), ("1.5kg", 1.5), ("2kg", 2.0),
                       ("2.5kg", 2.5), ("3kg", 3.0), ("3.5kg", 3.5)):
        db.add(SizeRule(name=name, multiplier=mult, is_active=True))
    # Over the limit, and switched off for exactly that reason.
    db.add(SizeRule(name="5kg", multiplier=5.0, is_active=False))

    db.add_all([
        FlavorRule(name="Vanilla Mango", extra_cost=0.0, sponge="vanilla", is_active=True),
        # The two that a name-based rule would misclassify as chocolate.
        FlavorRule(name="White Chocolate Raspberry Pistachio", extra_cost=250.0,
                   sponge="vanilla", is_active=True),
        FlavorRule(name="Vanilla Orange Chocolate Crumble", extra_cost=250.0,
                   sponge="vanilla", is_active=True),
        FlavorRule(name="Plain Chocolate", extra_cost=0.0, sponge="chocolate", is_active=True),
        FlavorRule(name="Belgian Chocolate Hazelnut", extra_cost=200.0,
                   sponge="chocolate", is_active=True),
        # Not part of the sponge flow at all.
        FlavorRule(name="Legacy Flavour", extra_cost=0.0, sponge=None, is_active=True),
    ])
    db.add(DesignRule(name="Basic Cream Finish", cost=0.0, is_active=True))
    db.add(AddonRule(name="Cake Topper", cost=100.0, is_active=True))
    db.add(RushRule(name="Standard (24hr+)", cost=0.0, is_active=True))
    db.commit()


def _options(client):
    res = client.get("/build-a-cake/options")
    assert res.status_code == 200, res.text
    return res.json()


# ─── 1. SPONGE SELECTION ─────────────────────────────

def test_exactly_two_sponges_are_offered(client, db, builder):
    sponges = _options(client)["sponges"]

    assert [s["sponge"] for s in sponges] == ["vanilla", "chocolate"]
    assert [s["label"] for s in sponges] == ["Vanilla Sponge", "Chocolate Sponge"]


def test_the_premium_product_is_not_offered_as_a_sponge(client, db, builder):
    """Premium is the base sponge plus a premium flavour, not a third choice."""
    sponges = _options(client)["sponges"]

    assert "Vanilla Premium Cake" not in [s["label"] for s in sponges]
    assert len(sponges) == 2


def test_a_sponge_carries_the_product_its_price_comes_from(client, db, builder):
    sponges = {s["sponge"]: s for s in _options(client)["sponges"]}

    assert sponges["vanilla"]["base_price"] == VANILLA_PER_KG
    assert sponges["chocolate"]["base_price"] == CHOCOLATE_PER_KG
    assert sponges["vanilla"]["product_id"] != sponges["chocolate"]["product_id"]


def test_an_unavailable_sponge_is_reported_rather_than_quietly_dropped(client, db, builder):
    db.query(Product).filter(Product.name == "Chocolate Base Cake").first().is_available = False
    db.commit()

    res = client.get("/build-a-cake/options")

    assert res.status_code == 503
    assert "Chocolate Sponge" in res.json()["detail"]


# ─── 2. FLAVOUR FILTERING ────────────────────────────

def test_every_returned_flavour_says_which_sponge_it_is_for(client, db, builder):
    flavors = _options(client)["flavors"]

    assert flavors, "no flavours returned"
    assert all(f["sponge"] in ("vanilla", "chocolate") for f in flavors)


def test_a_flavour_outside_the_sponge_flow_is_not_offered(client, db, builder):
    flavors = _options(client)["flavors"]

    assert "Legacy Flavour" not in [f["name"] for f in flavors]


def test_vanilla_sponge_offers_only_vanilla_flavours(client, db, builder):
    res = client.get("/build-a-cake/flavors?sponge=vanilla")

    assert res.status_code == 200
    names = {f["name"] for f in res.json()}
    assert names == {"Vanilla Mango", "White Chocolate Raspberry Pistachio",
                     "Vanilla Orange Chocolate Crumble"}
    assert "Plain Chocolate" not in names


def test_chocolate_sponge_offers_only_chocolate_flavours(client, db, builder):
    res = client.get("/build-a-cake/flavors?sponge=chocolate")

    names = {f["name"] for f in res.json()}
    assert names == {"Plain Chocolate", "Belgian Chocolate Hazelnut"}


def test_flavours_whose_names_would_fool_a_name_rule_are_classified_correctly(client, db, builder):
    """The reason sponge is stored rather than inferred."""
    vanilla = {f["name"] for f in client.get("/build-a-cake/flavors?sponge=vanilla").json()}

    assert "White Chocolate Raspberry Pistachio" in vanilla
    assert "Vanilla Orange Chocolate Crumble" in vanilla


def test_an_unknown_sponge_is_rejected_not_answered_with_an_empty_list(client, db, builder):
    res = client.get("/build-a-cake/flavors?sponge=strawberry")

    assert res.status_code == 422
    assert "Unknown sponge" in res.json()["detail"]


# ─── 3. SIZES AND PRICING ────────────────────────────

def test_sizes_run_up_to_3_5kg_and_no_further(client, db, builder):
    sizes = _options(client)["sizes"]

    assert [s["name"] for s in sizes] == \
        ["500g", "1kg", "1.5kg", "2kg", "2.5kg", "3kg", "3.5kg"]
    assert max(s["multiplier"] for s in sizes) == 3.5
    assert _options(client)["max_orderable_kg"] == 3.5


@pytest.mark.parametrize("size,expected", [
    ("500g", 1000.0), ("1kg", 2000.0), ("1.5kg", 3000.0),
    ("2kg", 4000.0), ("2.5kg", 5000.0), ("3kg", 6000.0), ("3.5kg", 7000.0),
])
def test_every_offered_size_prices_through_the_engine(client, db, builder, size, expected):
    product = db.query(Product).filter(Product.name == "Vanilla Base Cake").first()

    breakdown = calculate_item_price(
        db=db, product=product, customization=ItemCustomization(size=size))

    assert breakdown.line_total == expected


def test_the_flavour_surcharge_is_added_on_top_of_the_size(client, db, builder):
    product = db.query(Product).filter(Product.name == "Chocolate Base Cake").first()

    breakdown = calculate_item_price(
        db=db, product=product,
        customization=ItemCustomization(size="2kg", flavor="Belgian Chocolate Hazelnut"))

    # 2200 x 2 + 200, not (2200 + 200) x 2.
    assert breakdown.line_total == 4600.0


def test_the_quoted_price_is_what_the_order_charges(client, db, builder):
    """One pricing path, so a quote and a charge cannot disagree."""
    product = db.query(Product).filter(Product.name == "Vanilla Base Cake").first()
    quote = client.post("/pricing/calculate", json={
        "product_id": product.id, "quantity": 1,
        "customization": {"size": "3.5kg", "flavor": "Vanilla Mango"}}).json()

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = create_order(db, OrderCreate(user_id=user.id, items=[
        OrderItemCreate(product_id=product.id, quantity=1,
                        customization=ItemCustomization(size="3.5kg", flavor="Vanilla Mango"))]))

    assert order.subtotal == quote["total"] == 7000.0


# ─── 4. ABOVE 3.5 KG IS NOT ORDERABLE ────────────────

def test_an_oversized_cake_is_not_offered(client, db, builder):
    assert "5kg" not in [s["name"] for s in _options(client)["sizes"]]


def test_an_oversized_cake_cannot_be_priced(client, db, builder):
    """Refused by the engine, not merely absent from a list."""
    product = db.query(Product).filter(Product.name == "Vanilla Base Cake").first()

    with pytest.raises(HTTPException) as exc:
        calculate_item_price(db=db, product=product,
                             customization=ItemCustomization(size="5kg"))

    assert exc.value.status_code == 400


def test_an_oversized_cake_cannot_be_ordered_even_by_posting_it_directly(client, db, builder):
    """The flow is not the guard; the rule table is."""
    product = db.query(Product).filter(Product.name == "Vanilla Base Cake").first()
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")

    with pytest.raises(HTTPException) as exc:
        create_order(db, OrderCreate(user_id=user.id, items=[
            OrderItemCreate(product_id=product.id, quantity=1,
                            customization=ItemCustomization(size="5kg"))]))

    assert exc.value.status_code == 400


# ─── 5. CONTACT US ───────────────────────────────────

def test_an_oversized_request_becomes_an_enquiry_not_an_order(client, db, builder):
    from app.models.order import Order

    res = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210",
        "sponge": "vanilla", "flavor": "Vanilla Mango",
        "requested_size": "4.5kg", "details": "Two tiers for a wedding"})

    assert res.status_code == 201, res.text
    assert db.query(CakeEnquiry).count() == 1
    assert db.query(Order).count() == 0, "an enquiry created an order"


def test_an_unsupported_customisation_becomes_an_enquiry(client, db, builder):
    res = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210",
        "sponge": "chocolate",
        "details": "Shaped like a football, with a photo print on top"})

    assert res.status_code == 201
    enquiry = db.query(CakeEnquiry).first()
    assert "football" in enquiry.details
    assert enquiry.sponge == "chocolate"


def test_an_enquiry_carries_the_selections_for_whoever_answers(client, db, builder):
    client.post("/build-a-cake/enquiries", json={
        "contact_name": "  Priya  ", "contact_phone": "+91 98765 43210",
        "sponge": "vanilla", "flavor": "Vanilla Mango", "requested_size": "5kg"})

    enquiry = db.query(CakeEnquiry).first()
    assert enquiry.contact_name == "Priya"
    assert enquiry.contact_phone == "+919876543210", "phone not normalised"
    assert (enquiry.sponge, enquiry.flavor, enquiry.requested_size) == \
        ("vanilla", "Vanilla Mango", "5kg")


def test_an_enquiry_has_no_price_and_no_payment(client, db, builder):
    client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210", "requested_size": "6kg"})

    enquiry = db.query(CakeEnquiry).first()
    assert not hasattr(enquiry, "total_price")
    assert not hasattr(enquiry, "payment_status")


def test_an_invalid_phone_is_rejected(client, db, builder):
    res = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "abcdefghij"})

    assert res.status_code == 422
    assert db.query(CakeEnquiry).count() == 0


def test_an_invalid_sponge_is_rejected(client, db, builder):
    res = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210", "sponge": "red-velvet"})

    assert res.status_code == 422
    assert db.query(CakeEnquiry).count() == 0


def test_marking_handled_is_the_only_state_change(client, admin, db, builder):
    client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210"})
    enquiry = db.query(CakeEnquiry).first()

    res = client.patch(f"/build-a-cake/enquiries/{enquiry.id}/handled", headers=auth(admin))

    assert res.status_code == 200
    assert res.json()["is_handled"] is True


def test_the_enquiry_list_is_admin_only(client, customer, db, builder):
    assert client.get("/build-a-cake/enquiries").status_code == 401
    assert client.get("/build-a-cake/enquiries", headers=auth(customer)).status_code == 403


# ─── 6. REFERENCE IMAGE ──────────────────────────────

def test_a_reference_image_can_be_attached(client, db, builder):
    created = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210",
        "details": "Like the picture"}).json()

    res = client.post(
        f"/build-a-cake/enquiries/{created['id']}/image",
        files={"file": ("ref.png", b"\x89PNG\r\n\x1a\nfake", "image/png")})

    assert res.status_code == 200, res.text
    assert res.json()["reference_image_url"].startswith("/media/enquiry_")


def test_a_reference_image_uses_the_existing_media_path(client, db, builder):
    """Same upload mechanism as review photos - not a second one."""
    created = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210"}).json()

    url = client.post(f"/build-a-cake/enquiries/{created['id']}/image",
                      files={"file": ("ref.jpg", b"fake", "image/jpeg")}).json()

    assert url["reference_image_url"].startswith("/media/")


def test_a_non_image_upload_is_rejected(client, db, builder):
    created = client.post("/build-a-cake/enquiries", json={
        "contact_name": "Priya", "contact_phone": "9876543210"}).json()

    res = client.post(f"/build-a-cake/enquiries/{created['id']}/image",
                      files={"file": ("payload.pdf", b"%PDF-1.4", "application/pdf")})

    assert res.status_code == 400


def test_uploading_to_a_missing_enquiry_is_a_404(client, db, builder):
    res = client.post("/build-a-cake/enquiries/99999/image",
                      files={"file": ("ref.png", b"fake", "image/png")})

    assert res.status_code == 404
