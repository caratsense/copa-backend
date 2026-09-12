"""
One-off: configure the menu section structure for the client's catalogue.

Data only. This script creates and renames rows in `menu_sections` and sets
their sort order. It never touches the schema, never moves a product between
sections, and never deletes a section — the three existing menu-only sections
(Signature Collection, Premium Belgian, Specialty) are kept and pushed to the
end of the menu rather than removed, because they still hold the Build-a-Cake
base products.

Idempotent: safe to run any number of times. Sections are matched by name
(case- and whitespace-insensitive, the same way the pricing engine matches rule
names), so a second run finds everything already in place and reports "ok".

The two renames are keyed by id rather than by name so a re-run recognises the
already-renamed row instead of creating a duplicate: id 2 may legitimately read
either "Vanilla Cakes" (before) or "Vanilla Celebration Cakes" (after).

Run:  python -m scripts.setup_menu_sections [--dry-run]
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product


# ── The target menu, in the order the client wants it ────────────────────
# Position in this list IS the sort_order (1-based). The first eleven are the
# homepage-linked categories, in homepage card order; Wedding is menu-only with
# no homepage card; the last three are the pre-existing sections.
FINAL_ORDER = [
    "Chocolate Celebration Cakes",
    "Vanilla Celebration Cakes",
    "Cheesecakes",
    "Desserts & Pudding Tubs",
    "Brownies",
    "Tea Cakes",
    "Breads & Bun Collection",
    "Cookies",
    "Healthy Collection",
    "Dog Cakes",
    "Gifting Collection",
    "Wedding",
    "Signature Collection",
    "Premium Belgian",
    "Specialty",
]

# Renames, keyed by the id that must carry them. `was` is the baseline name,
# `now` the target — a row already holding `now` is accepted as done.
RENAMES = {
    2: {"was": "Vanilla Cakes", "now": "Vanilla Celebration Cakes"},
    3: {"was": "Chocolate Cakes", "now": "Chocolate Celebration Cakes"},
}

# The baseline this script is written against. Validated before anything is
# written, so a database that is not the expected starting point fails loudly
# instead of being half-configured. Product ids are asserted unchanged at the
# end too — this script must not move a product.
BASELINE = {
    1: {"names": ["Signature Collection"], "products": [5]},
    2: {"names": ["Vanilla Cakes", "Vanilla Celebration Cakes"], "products": [1, 2]},
    3: {"names": ["Chocolate Cakes", "Chocolate Celebration Cakes"], "products": [3]},
    4: {"names": ["Premium Belgian"], "products": [4]},
    5: {"names": ["Specialty"], "products": []},
}

# Sections created by this script. Everything else in FINAL_ORDER is expected to
# exist already, so a missing one is a baseline failure rather than a create.
NEW_SECTIONS = [
    "Cheesecakes",
    "Desserts & Pudding Tubs",
    "Brownies",
    "Tea Cakes",
    "Breads & Bun Collection",
    "Cookies",
    "Healthy Collection",
    "Dog Cakes",
    "Gifting Collection",
    "Wedding",
]


def _key(name):
    """Collapse case and spacing so 'Dog  Cakes ' matches 'Dog Cakes'."""
    return " ".join((name or "").split()).lower()


class BaselineError(Exception):
    """The database is not the baseline this script was written for."""


def _product_ids(db, section_id):
    return [
        p.id for p in db.query(Product)
        .filter(Product.section_id == section_id)
        .order_by(Product.id)
        .all()
    ]


def validate_baseline(db):
    """Refuse to touch a database that does not look like the known baseline."""
    problems = []

    by_id = {s.id: s for s in db.query(MenuSection).all()}

    for sid, expect in BASELINE.items():
        section = by_id.get(sid)
        if section is None:
            problems.append(f"section id {sid} is missing (expected {expect['names'][0]!r})")
            continue
        if _key(section.name) not in [_key(n) for n in expect["names"]]:
            allowed = " or ".join(repr(n) for n in expect["names"])
            problems.append(f"section id {sid} is named {section.name!r}, expected {allowed}")
        found = _product_ids(db, sid)
        if found != expect["products"]:
            problems.append(
                f"section id {sid} holds products {found}, expected {expect['products']}"
            )

    # Duplicate names would produce two sections with the same menu anchor, so
    # the homepage deep link becomes ambiguous. Catch it before adding more.
    seen = {}
    for s in by_id.values():
        seen.setdefault(_key(s.name), []).append(s.id)
    for key, ids in seen.items():
        if len(ids) > 1:
            problems.append(f"duplicate section name {key!r} on ids {sorted(ids)}")

    # Anything in FINAL_ORDER that is neither pre-existing nor ours to create
    # means the target list and this script have drifted apart.
    known = {_key(n) for n in NEW_SECTIONS}
    for expect in BASELINE.values():
        known.update(_key(n) for n in expect["names"])
    for name in FINAL_ORDER:
        if _key(name) not in known:
            problems.append(f"{name!r} is in FINAL_ORDER but neither a baseline nor a new section")

    if problems:
        raise BaselineError(
            "Baseline check failed — nothing was written:\n  - " + "\n  - ".join(problems)
        )

    print(f"Baseline OK: {len(by_id)} existing sections, product assignments as expected.")


def configure(db, dry_run=False):
    """Rename, create and reorder. Returns the number of changes made."""
    changes = 0

    # ── Renames (by id) ──
    for sid, rename in sorted(RENAMES.items()):
        section = db.query(MenuSection).filter(MenuSection.id == sid).first()
        if _key(section.name) == _key(rename["now"]):
            print(f"  ok      id {sid}: already named {rename['now']!r}")
            continue
        print(f"  RENAME  id {sid}: {section.name!r} -> {rename['now']!r}")
        if not dry_run:
            section.name = rename["now"]
        changes += 1

    # ── Creates (by name) ──
    existing = {_key(s.name): s for s in db.query(MenuSection).all()}
    for name in NEW_SECTIONS:
        if _key(name) in existing:
            print(f"  ok      {name!r} already exists (id {existing[_key(name)].id})")
            continue
        # description and image_url are deliberately left unset: no copy or
        # imagery has been supplied for these categories yet.
        print(f"  CREATE  {name!r} (active, no description, no image)")
        if not dry_run:
            section = MenuSection(name=name, sort_order=FINAL_ORDER.index(name) + 1, is_active=True)
            db.add(section)
            db.flush()          # so the new id is available for the sort pass
            existing[_key(name)] = section
        changes += 1

    # ── Sort order ──
    # is_active is NOT touched here: the three menu-only sections keep whatever
    # state the admin has them in, they are only moved to the end.
    if not dry_run:
        for position, name in enumerate(FINAL_ORDER, start=1):
            section = existing.get(_key(name))
            if section is None:
                raise BaselineError(f"{name!r} not found when setting sort order")
            if section.sort_order != position:
                print(f"  SORT    id {section.id} {section.name!r}: {section.sort_order} -> {position}")
                section.sort_order = position
                changes += 1

    return changes


def verify(db):
    """Re-read everything and assert the result. Returns a list of problems."""
    problems = []

    sections = (
        db.query(MenuSection)
        .order_by(MenuSection.sort_order.asc(), MenuSection.id.asc())
        .all()
    )

    print()
    print("Final state")
    print(f"  {'sort':>4}  {'id':>3}  {'name':<30}  {'active':<6}  products")
    for s in sections:
        pids = _product_ids(db, s.id)
        print(f"  {s.sort_order:>4}  {s.id:>3}  {s.name:<30}  {str(s.is_active):<6}  {pids if pids else '-'}")

    if len(sections) != len(FINAL_ORDER):
        problems.append(f"expected {len(FINAL_ORDER)} sections, found {len(sections)}")

    actual_order = [s.name for s in sections]
    if actual_order != FINAL_ORDER:
        problems.append(f"order mismatch:\n      expected {FINAL_ORDER}\n      actual   {actual_order}")

    for position, s in enumerate(sections, start=1):
        if s.sort_order != position:
            problems.append(f"id {s.id} {s.name!r} has sort_order {s.sort_order}, expected {position}")

    # The ten new sections must be empty — this script moves no products.
    for name in NEW_SECTIONS:
        match = [s for s in sections if _key(s.name) == _key(name)]
        if not match:
            problems.append(f"new section {name!r} is missing")
            continue
        pids = _product_ids(db, match[0].id)
        if pids:
            problems.append(f"new section {name!r} (id {match[0].id}) unexpectedly holds products {pids}")
        if not match[0].is_active:
            problems.append(f"new section {name!r} is not active")

    # Product assignments unchanged.
    for sid, expect in BASELINE.items():
        found = _product_ids(db, sid)
        if found != expect["products"]:
            problems.append(f"section id {sid} now holds products {found}, expected {expect['products']}")
    orphans = [p.id for p in db.query(Product).filter(Product.section_id == None).all()]
    if orphans:
        problems.append(f"products are unassigned: {orphans}")

    # No duplicate names.
    seen = {}
    for s in sections:
        seen.setdefault(_key(s.name), []).append(s.id)
    for key, ids in seen.items():
        if len(ids) > 1:
            problems.append(f"duplicate section name {key!r} on ids {sorted(ids)}")

    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and roll back without writing")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        validate_baseline(db)

        print()
        print("Dry run — no changes will be committed." if args.dry_run else "Applying changes")
        changes = configure(db, dry_run=args.dry_run)
        print(f"  {changes} change(s) {'would be made' if args.dry_run else 'staged'}")

        if args.dry_run:
            db.rollback()
            print("\nRolled back (dry run). Nothing was written.")
            return 0

        db.commit()             # one commit for the whole configuration
        print("  committed")

        db.expire_all()         # force a genuine re-read for verification
        problems = verify(db)
        if problems:
            print("\nVERIFICATION FAILED:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("\nVerified: 15 sections, order correct, 10 new sections empty, "
              "product assignments unchanged, no duplicate names.")
        return 0

    except BaselineError as e:
        db.rollback()
        print(f"\nABORTED: {e}", file=sys.stderr)
        return 2
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
