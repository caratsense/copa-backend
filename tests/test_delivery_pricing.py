"""
Delivery is priced off a standard ladder with a floor under it.

Three things are being held in place here.

The floor. Every paid zone charges at least MIN_DELIVERY_CHARGE. It is enforced
where zones are WRITTEN rather than where they are read, because the checkout
dropdown is populated from these same rows: coercing on read would show one
number to the customer and charge another.

The founder option. It is stored as an ordinary delivery zone — that is the
whole point of it, since it means the charge flows through the one pricing path
everything else uses instead of a second one nobody maintains. These tests say
so explicitly, so that someone later "tidying up" the row into a special case
finds out here rather than from an order that was never charged for it.

The ladder itself. scripts/standardise_delivery.py and scripts/seed.py each
carry a copy of the price list — one for the live database, one for a fresh
one. They have to agree, or a developer's machine prices delivery differently
from production.
"""

import pytest

from app.models.delivery import DeliveryZone
from app.models.user import UserRole
from app.services.pricing_engine import (
    FOUNDER_DELIVERY_ZONE,
    MIN_DELIVERY_CHARGE,
    lookup_delivery_charge,
)

from tests.conftest import auth, make_user


@pytest.fixture
def admin(db):
    return make_user(db, "Shriya Admin", UserRole.ADMIN)


# ─── THE FLOOR ───────────────────────────────────────

def test_a_zone_cannot_be_created_below_the_minimum(client, admin):
    res = client.post(
        "/admin/delivery-zones",
        json={"area_name": "Chinhat", "charge": 60, "estimated_time": 40},
        headers=auth(admin),
    )
    assert res.status_code == 422, res.text
    assert "100" in res.text


def test_a_zone_cannot_be_edited_down_below_the_minimum(client, admin, db):
    zone = DeliveryZone(area_name="Chinhat", charge=150, estimated_time=40, is_active=True)
    db.add(zone)
    db.commit()
    db.refresh(zone)

    res = client.patch(
        f"/admin/delivery-zones/{zone.id}",
        json={"area_name": "Chinhat", "charge": 60, "estimated_time": 40},
        headers=auth(admin),
    )
    assert res.status_code == 422, res.text

    db.refresh(zone)
    assert zone.charge == 150, "the refused edit must not have been applied"


def test_exactly_the_minimum_is_allowed(client, admin):
    res = client.post(
        "/admin/delivery-zones",
        json={"area_name": "Hazratganj", "charge": MIN_DELIVERY_CHARGE, "estimated_time": 25},
        headers=auth(admin),
    )
    assert res.status_code == 201, res.text
    assert res.json()["charge"] == MIN_DELIVERY_CHARGE


def test_a_free_zone_is_still_allowed_because_that_is_self_pickup(client, admin):
    """Zero is not a cheap delivery, it is the absence of one."""
    res = client.post(
        "/admin/delivery-zones",
        json={"area_name": "Pickup (Self)", "charge": 0, "estimated_time": 0},
        headers=auth(admin),
    )
    assert res.status_code == 201, res.text


# ─── THE FOUNDER OPTION ──────────────────────────────

def test_the_founder_option_prices_through_the_ordinary_zone_lookup(db):
    db.add(DeliveryZone(area_name=FOUNDER_DELIVERY_ZONE, charge=10000,
                        estimated_time=90, is_active=True))
    db.commit()

    assert lookup_delivery_charge(db, FOUNDER_DELIVERY_ZONE) == 10000.0


def test_the_founder_option_is_not_an_area_but_is_still_a_real_zone(db):
    """
    It carries no geography, so nothing may try to route by it — but it must
    remain a row, because that is what makes the ₹10,000 reach the total with
    no second pricing path to keep in step.
    """
    db.add(DeliveryZone(area_name=FOUNDER_DELIVERY_ZONE, charge=10000,
                        estimated_time=90, is_active=True))
    db.commit()

    zone = db.query(DeliveryZone).filter(
        DeliveryZone.area_name == FOUNDER_DELIVERY_ZONE
    ).one()
    assert zone.is_active
    assert zone.charge >= MIN_DELIVERY_CHARGE


def test_an_unknown_zone_is_still_refused_rather_than_delivered_free(db):
    with pytest.raises(Exception) as exc:
        lookup_delivery_charge(db, "Mumbai")
    assert "don't deliver" in str(exc.value)


# ─── THE LADDER ──────────────────────────────────────

def test_every_paid_rung_of_the_ladder_clears_the_floor():
    from scripts.standardise_delivery import LADDER

    below = {
        name: charge for name, (charge, _) in LADDER.items()
        if 0 < charge < MIN_DELIVERY_CHARGE
    }
    assert below == {}, f"these rungs are under the ₹{MIN_DELIVERY_CHARGE:g} floor: {below}"


def test_the_ladder_only_gives_away_delivery_for_self_pickup():
    from scripts.standardise_delivery import LADDER

    free = [name for name, (charge, _) in LADDER.items() if charge == 0]
    assert free == ["Pickup (Self)"]


def test_the_ladder_includes_the_three_areas_priced_by_the_shop():
    from scripts.standardise_delivery import LADDER

    assert LADDER["Gomtinagar"][0] == 200
    assert LADDER["Vrindavan Yojna"][0] == 350
    assert LADDER["South City"][0] == 400


def test_the_live_ladder_and_the_fresh_database_seed_agree():
    """
    Two copies of one price list. seed.py builds a new database,
    standardise_delivery.py repairs the live one; if they drift, a developer's
    machine charges different delivery from production and nothing says so.
    """
    import ast
    from pathlib import Path

    from scripts.standardise_delivery import LADDER

    source = Path(__file__).resolve().parents[1] / "scripts" / "seed.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    seeded: dict[str, float] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DeliveryZone"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        name_node = kw.get("area_name")
        if isinstance(name_node, ast.Constant):
            name = name_node.value
        elif isinstance(name_node, ast.Name) and name_node.id == "FOUNDER_DELIVERY_ZONE":
            name = FOUNDER_DELIVERY_ZONE
        else:
            pytest.fail(f"unreadable area_name in seed.py: {ast.dump(name_node)}")
        seeded[name] = float(ast.literal_eval(kw["charge"]))

    assert seeded, "found no DeliveryZone rows in seed.py — has it been restructured?"

    expected = {name: float(charge) for name, (charge, _) in LADDER.items()}
    assert seeded == expected
