"""
The client's menu, as data. Validates by default; writes only with --apply.

WHAT THIS IS
------------
One declarative definition of Cake O' Clock's real catalogue - 48 products
across 9 sections - plus the checks that have to pass before any of it reaches
a database. It is the single place the menu is written down, so a correction
from the client is a one-line edit here rather than a hand-written UPDATE.

NO PRICES ARE KNOWN YET
-----------------------
Every product below carries `base_price=None`, which means "not supplied by the
client", not "free". The script REFUSES to write while any price is None - a
placeholder price would otherwise be indistinguishable from a real one once it
was in the table, and the first customer to order would be charged it.

  python -m scripts.client_catalogue            # validate, change nothing
  python -m scripts.client_catalogue --apply    # refuses while prices are None

DECISIONS THAT ARE STILL TEMPORARY
----------------------------------
These are recorded here rather than in a chat thread because they change what
customers are charged, and whoever revisits this file needs to know they were
assumptions rather than instructions:

* Tea Cakes are priced as `fixed` pending client confirmation. If a tea cake
  is actually sold by weight, its pricing_unit becomes "kg" and its base_price
  changes meaning from "the price of the cake" to "the price per kg".
* Sugar Free Ragi Chocolate Cake is priced as `kg` pending client confirmation,
  because it is described as a cake rather than a tea cake.
* Button/Jumbo cookies and With/Without-Egg almond tea cakes are separate
  Product rows, not variants of one product. There is no variant model, and
  ItemCustomization is a closed five-field schema that silently drops anything
  it does not know - so a variant could not be carried onto an order line even
  if one existed here.
* Dog Cake 500gm and 1kg are likewise two `fixed` rows rather than one `kg`
  product with sizes: the global SizeRule table would offer 1.5kg/2kg/3kg/5kg
  on a dog cake, and would force the 500gm price to be exactly half.
* Cheesecakes has a section and a homepage card but no products were supplied,
  so it is deliberately absent below and stays empty.
* Product names are Title Cased for the menu. The source list mixes cases
  ("Classic Belgian chocolate", "sourdough crackers jar"); the wording is the
  client's, the capitalisation is ours and is worth confirming.

ALLERGEN CLAIMS
---------------
Only what the client actually stated. `contains-egg` and `contains-alcohol`
appear exactly where the source says so. No product is tagged `eggless`,
because the source establishes egg content for some items and says nothing
about the rest - and silence is not a claim we may make on the customer's
behalf. `eggless` stays in the vocabulary for when the client confirms it.

Note that tags do not reach an order: nothing copies them onto OrderItem, so
the baker's ticket and the WhatsApp notifications will not carry them. They are
a menu-display signal only.

IDEMPOTENCE AND SAFETY
----------------------
* A product is matched by (section, name). Running --apply twice creates
  nothing the second time.
* Products 1-5 are the original seeded Build-a-Cake bases. They are never
  touched, and the script aborts if it is ever about to write to one.
* Sections are resolved by exact name and the run fails fast if any is missing.
* Existing products outside this catalogue are never modified or deleted.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product


# The original seeded products. Never written to by this script.
PROTECTED_PRODUCT_IDS = {1, 2, 3, 4, 5}

PRICING_UNITS = {"kg", "fixed"}

# Tags with a fixed meaning. Anything else is a free descriptive tag, but these
# spellings are the only accepted way to say these particular things - an
# unvalidated "contains egg" would simply stop rendering its badge.
CONTROLLED_TAGS = {
    "contains-egg",
    "eggless",
    "contains-alcohol",
    "pack-of-6",
    "sugar-free",
    "dog-treat",
}
# Words that must only ever appear inside a controlled tag, so a near-miss
# spelling is caught rather than silently accepted as a descriptive tag.
RESERVED_WORDS = ("egg", "alcohol", "pack", "sugar")

TAG_FORMAT = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class CatalogueProduct:
    """One menu item. sort_order comes from its position in its section."""
    name: str
    category: str
    pricing_unit: str
    # None means "the client has not given us this price". Never a number.
    base_price: Optional[float] = None
    description: Optional[str] = None
    tags: tuple[str, ...] = field(default_factory=tuple)


def _kg(name, category, tags=()):
    return CatalogueProduct(name=name, category=category, pricing_unit="kg", tags=tuple(tags))


def _fixed(name, category, tags=()):
    return CatalogueProduct(name=name, category=category, pricing_unit="fixed", tags=tuple(tags))


# ── THE CATALOGUE ────────────────────────────────────────────────────────
# Keyed by exact MenuSection.name. Order within each tuple IS the sort_order,
# numbered from 1. Sections with no products supplied are absent entirely
# rather than present-and-empty, so the script never implies it manages them.

CATALOGUE: dict[str, tuple[CatalogueProduct, ...]] = {
    "Chocolate Celebration Cakes": (
        _kg("Classic Belgian Chocolate", "chocolate-celebration", ["chocolate", "belgian"]),
        _kg("Belgian Chocolate Orange Crumble", "chocolate-celebration", ["chocolate", "belgian", "orange"]),
        _kg("Belgian Chocolate Coffee Cake", "chocolate-celebration", ["chocolate", "belgian", "coffee"]),
        _kg("Belgian Chocolate Hazelnut Brownie Cake", "chocolate-celebration", ["chocolate", "belgian", "hazelnut"]),
        _kg("Chocolate Salted Caramel With Roasted Pecan & Crumble", "chocolate-celebration", ["chocolate", "salted-caramel", "pecan"]),
        _kg("Tiramisu Cake", "chocolate-celebration", ["coffee", "contains-egg", "contains-alcohol"]),
    ),
    "Vanilla Celebration Cakes": (
        _kg("Vanilla Salted Caramel Cake", "vanilla-celebration", ["vanilla", "salted-caramel"]),
        _kg("Raspberry Pistachio White Chocolate Cake", "vanilla-celebration", ["white-chocolate", "raspberry", "pistachio"]),
        _kg("Vanilla Cookie Cream Cake", "vanilla-celebration", ["vanilla", "cookie-cream"]),
        _kg("Vanilla Pineapple Cake", "vanilla-celebration", ["vanilla", "pineapple"]),
        _kg("Vanilla Pineapple Chocolate", "vanilla-celebration", ["vanilla", "pineapple", "chocolate"]),
        _kg("Vanilla Lemon Curd Blueberry Cake", "vanilla-celebration", ["vanilla", "lemon", "blueberry", "contains-egg"]),
        _kg("Vanilla Butterscotch Cake", "vanilla-celebration", ["vanilla", "butterscotch"]),
        _kg("Chiffon Fresh Fruit Milk Cake", "vanilla-celebration", ["chiffon", "fresh-fruit", "contains-egg"]),
    ),
    "Desserts & Pudding Tubs": (
        _fixed("Classic Fruit Cream Tub", "desserts-tubs", ["tub", "fresh-fruit"]),
        _fixed("Chocolate Coffee Baileys Mousse Cake Tub", "desserts-tubs", ["tub", "chocolate", "coffee", "contains-alcohol"]),
        _fixed("Banoffee Tub", "desserts-tubs", ["tub", "banoffee"]),
        _fixed("Classic Apple Pie", "desserts-tubs", ["pie", "apple"]),
        _fixed("Coffee Chocolate Hazelnut Choux Au Craquelin", "desserts-tubs", ["choux", "coffee", "hazelnut", "pack-of-6", "contains-egg"]),
        _fixed("Classic Vanilla Bean Chocolate Eclair", "desserts-tubs", ["eclair", "vanilla", "chocolate", "pack-of-6", "contains-egg"]),
    ),
    # Every brownie is sold as a pack of six; base_price will mean the pack.
    "Brownies": (
        _fixed("Chocolate Walnut Brownies", "brownies", ["chocolate", "walnut", "pack-of-6"]),
        _fixed("Cookie Cream Cakey Brownie", "brownies", ["cookie-cream", "pack-of-6"]),
        _fixed("Chocolate Biscoff Brownie", "brownies", ["chocolate", "biscoff", "pack-of-6"]),
        _fixed("Chip Chocolate Brownie", "brownies", ["chocolate", "choc-chip", "pack-of-6"]),
    ),
    "Tea Cakes": (
        _fixed("Orange Cardamom Crumble", "tea-cakes", ["orange", "cardamom"]),
        _fixed("Banana Chocolate Walnut", "tea-cakes", ["banana", "chocolate", "walnut"]),
        _fixed("Vanilla Chocolate Pineapple", "tea-cakes", ["vanilla", "chocolate", "pineapple"]),
        _fixed("Almond Tea Cake - With Egg", "tea-cakes", ["almond", "contains-egg"]),
        _fixed("Almond Tea Cake - Without Egg", "tea-cakes", ["almond"]),
    ),
    "Breads & Bun Collection": (
        _fixed("Milk Bread", "breads-buns", ["bread"]),
        _fixed("Wheat Bread", "breads-buns", ["bread", "wheat"]),
        _fixed("100% Wheat Bread", "breads-buns", ["bread", "wheat"]),
        _fixed("Brioche", "breads-buns", ["bread", "brioche"]),
        _fixed("Chilli Cheese Garlic Babka", "breads-buns", ["babka", "savoury"]),
        _fixed("Pesto Babka", "breads-buns", ["babka", "savoury", "pesto"]),
        _fixed("Burger Bun", "breads-buns", ["bun", "pack-of-6"]),
        _fixed("Pav", "breads-buns", ["bun", "pack-of-6"]),
    ),
    "Cookies": (
        _fixed("Chip Chocolate Cookie Box - Button", "cookies", ["cookie", "chocolate", "button"]),
        _fixed("Chip Chocolate Cookie Box - Jumbo", "cookies", ["cookie", "chocolate", "jumbo"]),
        _fixed("Orange Cardamom Butter Cookie - Button", "cookies", ["cookie", "orange", "cardamom", "button"]),
        _fixed("Orange Cardamom Butter Cookie - Jumbo", "cookies", ["cookie", "orange", "cardamom", "jumbo"]),
        _fixed("Chocolate Cookie", "cookies", ["cookie", "chocolate", "contains-egg"]),
        _fixed("Biscoff Cookie", "cookies", ["cookie", "biscoff", "contains-egg"]),
    ),
    "Healthy Collection": (
        # "kg" pending confirmation - see the module docstring.
        _kg("Sugar Free Ragi Chocolate Cake", "healthy", ["ragi", "chocolate", "sugar-free"]),
        _fixed("Whole Wheat Dates & Walnut Tea Cake", "healthy", ["whole-wheat", "dates", "walnut"]),
        _fixed("Sourdough Crackers Jar", "healthy", ["sourdough", "crackers", "jar"]),
    ),
    "Dog Cakes": (
        _fixed("Dog Cake - 500gm", "dog-cakes", ["dog-treat"]),
        _fixed("Dog Cake - 1kg", "dog-cakes", ["dog-treat"]),
    ),
}

# Supplied by the client with no products. Listed so the summary can say so out
# loud rather than leaving their absence looking like an oversight.
KNOWN_EMPTY_SECTIONS = ("Cheesecakes", "Gifting Collection", "Wedding")


# ── VALIDATION ───────────────────────────────────────────────────────────


class ValidationError(Exception):
    """The catalogue definition or the database is not fit to write."""


def _validate_definition() -> list[str]:
    """Check the catalogue itself. Needs no database."""
    problems: list[str] = []

    for section_name, products in CATALOGUE.items():
        if not products:
            problems.append(f"{section_name!r} is present but empty - remove it instead")

        seen: dict[str, int] = {}
        for position, p in enumerate(products, start=1):
            where = f"{section_name!r} #{position}"

            if not p.name or not p.name.strip():
                problems.append(f"{where}: name is empty")
            if not p.category or not p.category.strip():
                problems.append(f"{where}: category is empty")
            if p.pricing_unit not in PRICING_UNITS:
                problems.append(
                    f"{where} ({p.name!r}): pricing_unit {p.pricing_unit!r} is not one of "
                    f"{sorted(PRICING_UNITS)}"
                )

            # A price of 0.0 is a real price and would be accepted; None is the
            # only way to say "not supplied", which is what blocks writes.
            if p.base_price is not None and p.base_price < 0:
                problems.append(f"{where} ({p.name!r}): negative base_price")

            key = p.name.strip().lower()
            if key in seen:
                problems.append(
                    f"{section_name!r}: duplicate product name {p.name!r} "
                    f"at positions {seen[key]} and {position}"
                )
            seen[key] = position

            for tag in p.tags:
                if not TAG_FORMAT.match(tag):
                    problems.append(f"{where} ({p.name!r}): tag {tag!r} is not lower-case-kebab")
                    continue
                if tag in CONTROLLED_TAGS:
                    continue
                for word in RESERVED_WORDS:
                    if word in tag:
                        problems.append(
                            f"{where} ({p.name!r}): tag {tag!r} looks like a mis-spelled "
                            f"controlled tag. Use one of {sorted(CONTROLLED_TAGS)}"
                        )
                        break

    # sort_order is derived from position, so it is deterministic and 1-based by
    # construction. Assert it anyway - the guarantee is the point.
    for section_name, products in CATALOGUE.items():
        orders = [i for i, _ in enumerate(products, start=1)]
        if orders != list(range(1, len(products) + 1)):
            problems.append(f"{section_name!r}: sort_order is not 1..n")

    return problems


def _validate_sections(db) -> tuple[dict[str, MenuSection], list[str]]:
    """Resolve every catalogue section by exact name. Fail fast if any is missing."""
    problems: list[str] = []
    by_name = {s.name: s for s in db.query(MenuSection).all()}

    resolved: dict[str, MenuSection] = {}
    for section_name in CATALOGUE:
        section = by_name.get(section_name)
        if section is None:
            problems.append(
                f"section {section_name!r} does not exist. Run "
                f"scripts.setup_menu_sections first."
            )
            continue
        resolved[section_name] = section

    for section_name in KNOWN_EMPTY_SECTIONS:
        if section_name not in by_name:
            problems.append(f"expected empty section {section_name!r} does not exist")

    return resolved, problems


def _missing_prices() -> list[str]:
    return [
        f"{section_name} / {p.name}"
        for section_name, products in CATALOGUE.items()
        for p in products
        if p.base_price is None
    ]


# ── REPORTING ────────────────────────────────────────────────────────────


def _summary(resolved: dict[str, MenuSection], db) -> None:
    print()
    print("Catalogue")
    print(f"  {'section':<30} {'id':>3}  {'items':>5}  {'pricing':<14} {'priced':>7}  in db")

    total = 0
    for section_name, products in CATALOGUE.items():
        section = resolved.get(section_name)
        units = sorted({p.pricing_unit for p in products})
        priced = sum(1 for p in products if p.base_price is not None)
        existing = (
            db.query(Product).filter(Product.section_id == section.id).count()
            if section else 0
        )
        total += len(products)
        print(f"  {section_name:<30} {str(section.id) if section else '??':>3}  "
              f"{len(products):>5}  {'/'.join(units):<14} {priced:>3}/{len(products):<3}  {existing}")

    print(f"  {'':<30} {'':>3}  {total:>5}  total product definitions")

    print()
    print("Empty by instruction (no products supplied):")
    for section_name in KNOWN_EMPTY_SECTIONS:
        print(f"  - {section_name}")

    print()
    print("Unresolved / pending client confirmation:")
    missing = _missing_prices()
    print(f"  - NO PRICES SUPPLIED: {len(missing)} of {total} products have base_price=None.")
    print(f"    Nothing can be written until every one is filled in.")
    print("  - Tea Cakes (5) treated as 'fixed' - confirm they are not sold by weight.")
    print("  - Sugar Free Ragi Chocolate Cake treated as 'kg' - confirm.")
    print("  - Button/Jumbo and With/Without-Egg are separate rows, not variants.")
    print("  - Product names are Title Cased; the client's source mixes cases.")
    print("  - No product is tagged 'eggless': the source only establishes which")
    print("    items CONTAIN egg, and silence is not a claim we can make.")


# ── WRITE (only with --apply, only when fully priced) ─────────────────────


def _apply(db, resolved: dict[str, MenuSection]) -> int:
    """Create missing catalogue products. Never updates or deletes anything."""
    created = 0

    for section_name, products in CATALOGUE.items():
        section = resolved[section_name]
        existing = {
            p.name.strip().lower(): p
            for p in db.query(Product).filter(Product.section_id == section.id).all()
        }

        for position, item in enumerate(products, start=1):
            match = existing.get(item.name.strip().lower())
            if match is not None:
                # Belt and braces: a catalogue name must never resolve onto one
                # of the original seeded products.
                if match.id in PROTECTED_PRODUCT_IDS:
                    raise ValidationError(
                        f"{item.name!r} matches protected product id {match.id}. "
                        f"Refusing to touch the original seeded products."
                    )
                print(f"  ok      {section_name} / {item.name} (id {match.id})")
                continue

            print(f"  CREATE  {section_name} / {item.name} "
                  f"[{item.pricing_unit}, sort {position}, Rs {item.base_price}]")
            db.add(Product(
                name=item.name,
                category=item.category,
                description=item.description,
                base_price=item.base_price,
                pricing_unit=item.pricing_unit,
                is_customizable=False,   # menu items, not Build-a-Cake bases
                is_available=True,
                tags=list(item.tags),
                section_id=section.id,
                sort_order=position,
            ))
            created += 1

    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or apply the client catalogue.")
    parser.add_argument(
        "--apply", action="store_true",
        help="create missing catalogue products. Refused while any price is missing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="explicit no-op mode; this is also the default when --apply is absent.",
    )
    args = parser.parse_args()

    if args.apply and args.dry_run:
        print("--apply and --dry-run contradict each other.", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        problems = _validate_definition()
        resolved, section_problems = _validate_sections(db)
        problems += section_problems

        if problems:
            print("VALIDATION FAILED - nothing was written:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"Definition OK: {sum(len(v) for v in CATALOGUE.values())} products across "
              f"{len(CATALOGUE)} sections; every section resolved by name.")

        _summary(resolved, db)

        missing = _missing_prices()
        if not args.apply:
            print()
            print("Dry run - no database changes were made"
                  if args.dry_run else
                  "Validation only (default) - no database changes were made.")
            if missing:
                print(f"--apply would be REFUSED: {len(missing)} product(s) have no price.")
            return 0

        if missing:
            print()
            print(f"REFUSING TO WRITE: {len(missing)} product(s) have no price.")
            print("A placeholder price is indistinguishable from a real one once it is in")
            print("the table, and the first customer to order would be charged it. Fill in")
            print("every base_price in this file, then re-run with --apply.")
            for name in missing[:5]:
                print(f"  - {name}")
            if len(missing) > 5:
                print(f"  ... and {len(missing) - 5} more")
            return 1

        print()
        print("Applying")
        created = _apply(db, resolved)
        db.commit()
        print(f"  committed - {created} product(s) created")
        return 0

    except ValidationError as e:
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
