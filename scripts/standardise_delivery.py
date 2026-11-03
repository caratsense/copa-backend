"""
Put the live delivery zones onto the standard price ladder.

WHY THIS IS A SCRIPT AND NOT AUTOMATIC
--------------------------------------
Delivery zones are ordinary rows that the admin screen edits. A price list
applied at every boot would silently undo whatever Shriya changed that morning,
and it would do it on a redeploy she did not ask for. So the new list lives in
scripts/seed.py for a fresh database and here for the live one, and applying it
is a deliberate step somebody takes once.

THE LADDER
----------
Nothing paid sits below MIN_DELIVERY_CHARGE. The rungs follow the shop's own
estimated_time readings outward from the kitchen, with the three areas that
were priced directly — Gomtinagar, Vrindavan Yojna, South City — dropped in
where they belong.

WHAT IT DOES
------------
1. Re-prices every zone in the table below, matching on area name, ignoring
   case and spacing ("Gomti Nagar" finds the row spelled "Gomtinagar").
2. Creates the rows that do not exist yet, including the founder option.
3. Raises any OTHER active paid zone that is under the floor up to it, so no
   route is left below the minimum just because it is not named here.

WHAT IT WILL NOT DO
-------------------
- It never deletes a zone. Order.delivery_zone_id is a real foreign key, and a
  deleted zone takes the delivery area off every past order that used it. A
  zone that should disappear from checkout gets paused on the admin screen.
- It never touches an order, a price already charged, or anything outside the
  delivery_zones table.
- It is idempotent. The second run reports no changes.

USAGE
-----
    # look, change nothing (default)
    python -m scripts.standardise_delivery

    # actually apply it
    python -m scripts.standardise_delivery --apply

DATABASE_URL is read from the environment. On Railway this must run *inside*
the container — `railway run` executes locally with the env injected, and
postgres.railway.internal does not resolve from a workstation:

    railway ssh --service copa-backend "python -m scripts.standardise_delivery"
    railway ssh --service copa-backend "python -m scripts.standardise_delivery --apply"
"""

import sys

from app.db import SessionLocal
from app.models.delivery import DeliveryZone
from app.services.pricing_engine import FOUNDER_DELIVERY_ZONE, MIN_DELIVERY_CHARGE

# area_name -> (charge, estimated_time in minutes)
LADDER: dict[str, tuple[float, int]] = {
    "Pickup (Self)": (0, 0),
    # ── base rung: 25-30 minutes out ──
    "Hazratganj": (100, 25),
    "Lucknow Central": (100, 30),
    "Aminabad": (100, 30),
    # ── middle rung: 35-40 minutes ──
    "Indira Nagar": (150, 35),
    "Mahanagar": (150, 35),
    "Aliganj": (150, 40),
    # ── priced directly by the shop ──
    "Gomtinagar": (200, 35),
    "Lucknow Outskirts": (300, 75),
    "Vrindavan Yojna": (350, 55),
    "South City": (400, 60),
    # ── not an area at all ──
    FOUNDER_DELIVERY_ZONE: (10000, 90),
}


def _key(name: str) -> str:
    return "".join((name or "").lower().split())


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()
    changes: list[str] = []

    try:
        existing = db.query(DeliveryZone).all()
        by_key = {_key(z.area_name): z for z in existing}

        for name, (charge, minutes) in LADDER.items():
            zone = by_key.get(_key(name))
            if zone is None:
                changes.append(f"  CREATE  {name:34s} Rs {charge:<7.0f} ~{minutes} min")
                if apply:
                    db.add(DeliveryZone(area_name=name, charge=charge,
                                        estimated_time=minutes, is_active=True))
                continue

            if float(zone.charge) != float(charge) or int(zone.estimated_time) != int(minutes):
                changes.append(
                    f"  REPRICE {zone.area_name:34s} "
                    f"Rs {float(zone.charge):<7.0f} -> Rs {charge:<7.0f} "
                    f"(~{zone.estimated_time} -> ~{minutes} min)"
                )
                if apply:
                    zone.charge = charge
                    zone.estimated_time = minutes

        # Anything else still on sale and under the floor. Named zones are
        # already handled above, so this only catches rows added since.
        named = {_key(n) for n in LADDER}
        for zone in existing:
            if _key(zone.area_name) in named:
                continue
            if zone.is_active and 0 < float(zone.charge) < MIN_DELIVERY_CHARGE:
                changes.append(
                    f"  FLOOR   {zone.area_name:34s} "
                    f"Rs {float(zone.charge):<7.0f} -> Rs {MIN_DELIVERY_CHARGE:<7.0f}"
                )
                if apply:
                    zone.charge = MIN_DELIVERY_CHARGE

        if not changes:
            print("Delivery zones are already on the standard ladder. Nothing to do.")
            return 0

        print(f"{len(changes)} change{'s' if len(changes) != 1 else ''}:\n")
        print("\n".join(changes))

        if apply:
            db.commit()
            print("\nApplied.")
        else:
            print("\nDRY RUN - nothing was written. Re-run with --apply.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
