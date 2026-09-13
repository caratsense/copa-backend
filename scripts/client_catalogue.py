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

# DRAFT ROWS AND THE PLACEHOLDER PRICE
# ------------------------------------
# products.base_price is NOT NULL, so a draft row has to carry some number.
# Which number matters, because the number is publicly readable: an
# is_available=false product is still returned by GET /products/{id} and by
# GET /products?available_only=false, and POST /pricing/calculate will happily
# quote it - none of those three check availability. What a draft row CANNOT do
# is be ordered: create_order rejects an unavailable product with a 400, and
# that is the only route to an order.
#
# So the placeholder must fail in the safe direction if anyone ever flips
# is_available (one admin toggle does it). 0.0 is the dangerous choice - it is a
# real price meaning "free", the engine would total the order at zero, and
# PayU's callback check (paid != owed) is satisfied by 0 == 0. A deliberately
# absurd number cannot be mistaken for a real price, cannot be ordered by
# accident, and errs towards overcharging rather than giving cakes away.
DRAFT_PRICE_SENTINEL = 999999.0

# Stamped on any row created without a real price, so drafts can be found again
# without relying on the sentinel number alone.
DRAFT_TAG = "draft-no-price"

# Tags with a fixed meaning. Anything else is a free descriptive tag, but these
# spellings are the only accepted way to say these particular things - an
# unvalidated "contains egg" would simply stop rendering its badge.
CONTROLLED_TAGS = {
    DRAFT_TAG,
    "contains-egg",
    "eggless",
    "contains-alcohol",
    "pack-of-6",
    "sugar-free",
    "dog-treat",
}
# Words that must only ever appear inside a controlled tag, so a near-miss
# spelling is caught rather than silently accepted as a descriptive tag.
RESERVED_WORDS = ("egg", "alcohol", "pack", "sugar", "draft", "price")

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
    # Names this product has previously been written to the database under.
    # Draft rows are matched by (section, name), so a rename here would
    # otherwise stop matching its own existing row and create a duplicate on
    # the next run. Listing the old name makes the match hold; the row itself
    # is NOT renamed - the script reports the difference and leaves it alone.
    previous_names: tuple[str, ...] = field(default_factory=tuple)


def _kg(name, category, tags=(), description=None, previous_names=()):
    return CatalogueProduct(name=name, category=category, pricing_unit="kg",
                            tags=tuple(tags), description=description,
                            previous_names=tuple(previous_names))


def _fixed(name, category, tags=(), description=None, previous_names=()):
    return CatalogueProduct(name=name, category=category, pricing_unit="fixed",
                            tags=tuple(tags), description=description,
                            previous_names=tuple(previous_names))


# ── SIZES THE CLIENT SELLS EACH CAKE IN ──────────────────────────────────
# Documentation only. Nothing reads this at runtime and no schema field exists
# for it: sizes are the global SizeRule table, which is shared by every per-kg
# product and currently offers 500g / 1kg / 1.5kg / 2kg / 3kg / 5kg.
#
# Two entries do not fit that table and need a decision before these cakes go
# on sale - see the report:
#   * Blueberry Lemon Curd Cake starts at 700g, and no 700g SizeRule exists.
#   * Belgian Chocolate Coffee Cake starts at 1kg, but nothing stops a customer
#     choosing 500g, because SizeRule is global rather than per-product.
SUPPLIED_SIZES: dict[str, tuple[str, ...]] = {
    "Belgian Chocolate Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Belgian Chocolate Orange Crumble Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Belgian Chocolate Coffee Cake With Cinnamon Roll": ("1 kg", "1.5 kg", "2 kg and above"),
    "Belgian Chocolate Hazelnut Brownie Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Belgian Chocolate Salted Caramel Cake With Roasted Pecan & Crumble": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Tiramisu Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Vanilla Salted Caramel Cake With Plain Crumble": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Raspberry Pistachio White Chocolate Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Vanilla Cookie Cream Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Vanilla Pineapple Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Vanilla Chocolate Pineapple Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Blueberry Lemon Curd Cake": ("700 g", "1 kg", "1.5 kg", "2 kg and above"),
    "Vanilla Butterscotch Cake": ("500 g", "1 kg", "1.5 kg", "2 kg and above"),
}


# ── THE CATALOGUE ────────────────────────────────────────────────────────
# Keyed by exact MenuSection.name. Order within each tuple IS the sort_order,
# numbered from 1. Sections with no products supplied are absent entirely
# rather than present-and-empty, so the script never implies it manages them.

CATALOGUE: dict[str, tuple[CatalogueProduct, ...]] = {
    "Chocolate Celebration Cakes": (
        _kg("Belgian Chocolate Cake", "chocolate-celebration",
            ["chocolate", "belgian", "eggless"],
            previous_names=["Classic Belgian Chocolate"],
            description=(
                "Pure chocolate indulgence, made eggless. Rich Belgian chocolate meets "
                "soft, moist cake and a delicate cocoa finish for a dessert that is "
                "luxurious, comforting and utterly decadent.\n\n"
                "Perfect for celebrations, gifting, or simply when you're craving really "
                "good chocolate cake.\n\n"
                "Eggless."
            )),
        _kg("Belgian Chocolate Orange Crumble Cake", "chocolate-celebration",
            ["chocolate", "belgian", "orange", "eggless"],
            previous_names=["Belgian Chocolate Orange Crumble"],
            description=(
                "A decadent pairing of rich Belgian chocolate and zesty orange, brought "
                "together in a beautifully indulgent eggless cake. Finished with crumble "
                "and an assortment of chocolate, nuts and dehydrated orange for layers of "
                "texture, flavour and crunch.\n\n"
                "Deeply chocolatey, bright with citrus and wonderfully satisfying - this "
                "is a little more special than your everyday chocolate cake."
            )),
        # The client's copy names a cinnamon roll the earlier definition did not,
        # and gives a size list starting at 1kg rather than 500g. Treated as the
        # same product renamed, because there is exactly one coffee cake to map
        # onto - but worth a second pair of eyes before it goes on sale.
        _kg("Belgian Chocolate Coffee Cake With Cinnamon Roll", "chocolate-celebration",
            ["chocolate", "belgian", "coffee", "eggless"],
            previous_names=["Belgian Chocolate Coffee Cake"],
            description=(
                "A rich and indulgent eggless Belgian chocolate coffee cake, topped with a "
                "soft, pillowy cinnamon roll and finished with a generous chocolate swirl. "
                "The deep, velvety notes of Belgian chocolate and coffee pair beautifully "
                "with the warm, aromatic sweetness of cinnamon - creating a cake that is "
                "both comforting and decadent.\n\n"
                "A beautiful choice for celebrations, coffee dates and everything in "
                "between.\n\n"
                "Eggless."
            )),
        _kg("Belgian Chocolate Hazelnut Brownie Cake", "chocolate-celebration",
            ["chocolate", "belgian", "hazelnut", "eggless"],
            description=(
                "A decadent combination of rich Belgian chocolate, fudgy brownie and "
                "roasted hazelnuts, all in one irresistible eggless cake. Dense, gooey and "
                "intensely chocolatey, with the beautiful crunch of hazelnuts running "
                "through every bite and a glossy chocolate finish on top.\n\n"
                "For the ones who like their chocolate extra rich, extra fudgy and "
                "unapologetically indulgent.\n\n"
                "Eggless."
            )),
        _kg("Belgian Chocolate Salted Caramel Cake With Roasted Pecan & Crumble",
            "chocolate-celebration",
            ["chocolate", "belgian", "salted-caramel", "pecan", "eggless"],
            previous_names=["Chocolate Salted Caramel With Roasted Pecan & Crumble"],
            description=(
                "A decadent celebration of Belgian chocolate and salted caramel, layered "
                "with rich, velvety chocolate goodness and finished with roasted pecans on "
                "the outside and a delicate crumble on the inside. The deep chocolate "
                "flavour, buttery caramel, nutty crunch and hint of sea salt come together "
                "in a beautifully indulgent balance of sweet, salty and rich.\n\n"
                "An eggless chocolate cake with just the right amount of crunch and "
                "caramel - made for those who like their desserts a little more "
                "indulgent.\n\n"
                "Eggless."
            )),
        _kg("Tiramisu Cake", "chocolate-celebration",
            ["coffee", "contains-egg", "contains-alcohol"],
            description=(
                "A classic Italian-inspired indulgence, reimagined as a celebration cake. "
                "Layers of delicate coffee-soaked sponge come together with a rich, creamy "
                "mascarpone-style filling, finished with a generous dusting of cocoa and "
                "elegant chocolate accents.\n\n"
                "Infused with rum for a beautiful depth of flavour, this is a grown-up take "
                "on the timeless tiramisu - smooth, creamy, coffee-forward and irresistibly "
                "indulgent.\n\n"
                "Contains egg & alcohol (rum)."
            )),
    ),
    "Vanilla Celebration Cakes": (
        _kg("Vanilla Salted Caramel Cake With Plain Crumble", "vanilla-celebration",
            ["vanilla", "salted-caramel", "eggless"],
            previous_names=["Vanilla Salted Caramel Cake"],
            description=(
                "A delicate and indulgent eggless vanilla cake layered with smooth salted "
                "caramel and finished with a generous topping of buttery, golden crumble. "
                "The sweetness of vanilla meets the subtle saltiness of caramel, while the "
                "crumble adds a delightful crunch to every bite.\n\n"
                "Elegant, comforting and beautifully balanced - a cake that lets simple "
                "flavours shine.\n\n"
                "Eggless."
            )),
        # NOT tagged eggless. The client's own note is that the cake is eggless
        # but the macarons decorating it contain egg - so the thing that arrives
        # at the customer's door contains egg, and that is what an allergy tag
        # has to describe. Tagging both would render two contradictory badges.
        # Confirm with the client whether an egg-free decoration is offered.
        _kg("Raspberry Pistachio White Chocolate Cake", "vanilla-celebration",
            ["white-chocolate", "raspberry", "pistachio", "contains-egg"],
            description=(
                "A delicate and indulgent combination of fruity raspberry, creamy white "
                "chocolate and nutty pistachio. This eggless cake brings together layers of "
                "soft cake and rich flavours, finished with white chocolate ganache and a "
                "playful assortment of pistachio, raspberry and chocolate accents.\n\n"
                "Fresh, creamy and beautifully balanced, with the perfect contrast of sweet "
                "white chocolate, vibrant raspberry and earthy pistachio.\n\n"
                "Please note: The cake is eggless; the macarons used for decoration contain "
                "egg."
            )),
        _kg("Vanilla Cookie Cream Cake", "vanilla-celebration",
            ["vanilla", "cookie-cream", "eggless"],
            description=(
                "A soft and indulgent eggless vanilla cake layered with smooth, creamy "
                "cookie filling and finished with the irresistible crunch of cookies. "
                "Delicate vanilla, rich cream and that familiar cookie goodness come "
                "together for a cake that is comforting, creamy and wonderfully "
                "nostalgic.\n\n"
                "Simple, indulgent and impossible to stop at just one slice.\n\n"
                "Eggless."
            )),
        _kg("Vanilla Pineapple Cake", "vanilla-celebration",
            ["vanilla", "pineapple", "eggless"],
            description=(
                "A timeless favourite, our eggless Vanilla Pineapple Cake brings together "
                "soft, delicate vanilla cake with the bright, juicy sweetness of pineapple "
                "and smooth, creamy frosting. Light, fruity and wonderfully refreshing, "
                "with just the right balance of sweetness.\n\n"
                "A classic that never goes out of style - perfect for birthdays, "
                "celebrations and all the little moments worth making sweeter.\n\n"
                "Eggless."
            )),
        # Distinct from the Tea Cakes product "Vanilla Chocolate Pineapple";
        # this is the celebration cake, sold 500g and up.
        _kg("Vanilla Chocolate Pineapple Cake", "vanilla-celebration",
            ["vanilla", "pineapple", "chocolate", "eggless"],
            previous_names=["Vanilla Pineapple Chocolate"],
            description=(
                "A twist on a classic favourite - our eggless Vanilla Chocolate Pineapple "
                "Cake brings together soft vanilla sponge, chocolate and the bright, juicy "
                "sweetness of pineapple. Finished with smooth cream and delicate hand-made "
                "floral details, it's fresh, fruity, chocolatey and beautifully "
                "balanced.\n\n"
                "A little bit classic, a little bit indulgent, and perfect for celebrations "
                "of all kinds.\n\n"
                "Eggless."
            )),
        _kg("Blueberry Lemon Curd Cake", "vanilla-celebration",
            ["vanilla", "lemon", "blueberry", "contains-egg"],
            previous_names=["Vanilla Lemon Curd Blueberry Cake"],
            description=(
                "A beautifully balanced combination of fresh blueberry and zesty lemon "
                "curd, layered with soft, delicate cake and smooth cream. The sweetness of "
                "blueberries meets the bright, citrusy tang of lemon, creating a light, "
                "refreshing flavour with just the right touch of indulgence.\n\n"
                "A simple cake - that feels as beautiful as it tastes.\n\n"
                "Contains egg."
            )),
        _kg("Vanilla Butterscotch Cake", "vanilla-celebration",
            ["vanilla", "butterscotch", "eggless"],
            description=(
                "A rich and indulgent take on a classic favourite, our eggless Vanilla "
                "Butterscotch Cake brings together soft vanilla sponge and vanilla cream, "
                "finished with a generous layer of crunchy caramelised butterscotch.\n\n"
                "Buttery, creamy and delightfully crunchy, with the nostalgic sweetness of "
                "butterscotch in every bite. A timeless celebration cake that never fails "
                "to make an impression.\n\n"
                "Eggless."
            )),
        # No copy supplied by the client for this one - left exactly as it was.
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
            if p.base_price == DRAFT_PRICE_SENTINEL:
                problems.append(
                    f"{where} ({p.name!r}): base_price equals the draft sentinel "
                    f"{DRAFT_PRICE_SENTINEL}. A real price must never be that number, "
                    f"or --apply cannot tell a priced row from an unpriced one."
                )
            if DRAFT_TAG in p.tags:
                problems.append(
                    f"{where} ({p.name!r}): {DRAFT_TAG!r} is applied by the script, "
                    f"never written in the definition"
                )

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

    # A previous name must not be some other product's current name, or the
    # two would fight over the same database row.
    current = {
        p.name.strip().lower()
        for products in CATALOGUE.values() for p in products
    }
    for section_name, products in CATALOGUE.items():
        for p in products:
            for old_name in p.previous_names:
                key = old_name.strip().lower()
                if key in current:
                    problems.append(
                        f"{section_name!r} ({p.name!r}): previous_name {old_name!r} is "
                        f"another product's current name"
                    )
                if key == p.name.strip().lower():
                    problems.append(
                        f"{section_name!r} ({p.name!r}): previous_name repeats the "
                        f"current name"
                    )

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

    drafts = db.query(Product).filter(Product.base_price == DRAFT_PRICE_SENTINEL).count()
    if drafts:
        print()
        print(f"Draft rows in the database: {drafts} (unavailable, placeholder price "
              f"{DRAFT_PRICE_SENTINEL}, tagged {DRAFT_TAG!r}).")
        print("  They cannot be ordered and do not appear on the menu, in WhatsApp")
        print("  or in Build-a-Cake. --apply will price them and put them on sale.")

    print()
    print("Unresolved / pending client confirmation:")
    missing = _missing_prices()
    print(f"  - NO PRICES SUPPLIED: {len(missing)} of {total} products have base_price=None.")
    print(f"    Nothing goes on sale until every one is filled in.")
    print("  - Tea Cakes (5) treated as 'fixed' - confirm they are not sold by weight.")
    print("  - Sugar Free Ragi Chocolate Cake treated as 'kg' - confirm.")
    print("  - Button/Jumbo and With/Without-Egg are separate rows, not variants.")
    print("  - Product names are Title Cased; the client's source mixes cases.")
    print("  - No product is tagged 'eggless': the source only establishes which")
    print("    items CONTAIN egg, and silence is not a claim we can make.")


# ── WRITE (only with --apply, only when fully priced) ─────────────────────


def _is_draft_row(row: Product) -> bool:
    """A row this script created without a real price."""
    return row.base_price == DRAFT_PRICE_SENTINEL or DRAFT_TAG in (row.tags or [])


def _write(db, resolved: dict[str, MenuSection], draft: bool) -> tuple[int, int]:
    """
    Create missing catalogue products; in real mode, price up existing drafts.

    Returns (created, promoted). Never deletes, never renames, and never writes
    to a product that is not part of this catalogue.
    """
    created = promoted = 0

    for section_name, products in CATALOGUE.items():
        section = resolved[section_name]
        existing = {
            p.name.strip().lower(): p
            for p in db.query(Product).filter(Product.section_id == section.id).all()
        }

        for position, item in enumerate(products, start=1):
            match = existing.get(item.name.strip().lower())

            # Renamed since the row was written. Match it anyway so a rename
            # cannot quietly produce a second row for the same cake. The row is
            # NOT renamed here - that is a product change, and this script only
            # reports the difference.
            renamed_from = None
            if match is None:
                for old_name in item.previous_names:
                    candidate = existing.get(old_name.strip().lower())
                    if candidate is not None:
                        match, renamed_from = candidate, old_name
                        break

            if match is not None:
                # Belt and braces: a catalogue name must never resolve onto one
                # of the original seeded products.
                if match.id in PROTECTED_PRODUCT_IDS:
                    raise ValidationError(
                        f"{item.name!r} matches protected product id {match.id}. "
                        f"Refusing to touch the original seeded products."
                    )
                if renamed_from is not None:
                    print(f"  RENAMED  {section_name} / id {match.id} is still called "
                          f"{renamed_from!r} in the database; the catalogue now says "
                          f"{item.name!r}. Not renamed - no duplicate created.")
                if draft or not _is_draft_row(match):
                    if renamed_from is None:
                        print(f"  ok       {section_name} / {item.name} (id {match.id})")
                    continue

                # Real mode over a draft row: give it its price and let it be
                # sold. This is the only circumstance in which the script
                # modifies a row it did not just create.
                print(f"  PRICE    {section_name} / {item.name} (id {match.id}) "
                      f"{match.base_price} -> {item.base_price}, now available")
                match.base_price = item.base_price
                match.tags = [t for t in (match.tags or []) if t != DRAFT_TAG]
                match.is_available = True
                promoted += 1
                continue

            price = item.base_price if item.base_price is not None else DRAFT_PRICE_SENTINEL
            unpriced = item.base_price is None
            tags = list(item.tags) + ([DRAFT_TAG] if unpriced else [])

            # A draft is never available. An unpriced row must never be
            # available whatever mode we are in - though real mode cannot reach
            # here with one, since it refuses to run at all while any price is
            # missing.
            available = not draft and not unpriced

            print(f"  {'DRAFT  ' if draft else 'CREATE '} {section_name} / {item.name} "
                  f"[{item.pricing_unit}, sort {position}, "
                  f"{'NO PRICE - placeholder ' + str(price) if unpriced else 'Rs ' + str(price)}, "
                  f"available={available}]")
            db.add(Product(
                name=item.name,
                category=item.category,
                description=item.description,
                base_price=price,
                pricing_unit=item.pricing_unit,
                is_customizable=False,   # menu items, not Build-a-Cake bases
                is_available=available,
                tags=tags,
                section_id=section.id,
                sort_order=position,
            ))
            created += 1

    return created, promoted


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or apply the client catalogue.")
    parser.add_argument(
        "--apply", action="store_true",
        help="create missing catalogue products. Refused while any price is missing.",
    )
    parser.add_argument(
        "--apply-draft", action="store_true",
        help=(
            "create the catalogue as DRAFT rows: is_available=false, and any "
            "product with no price carries an obviously-wrong placeholder plus "
            "the '%s' tag. Drafts cannot be ordered - create_order rejects an "
            "unavailable product - and do not appear on the menu, in WhatsApp "
            "or in Build-a-Cake." % DRAFT_TAG
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="explicit no-op mode; this is also the default when no apply flag is given.",
    )
    args = parser.parse_args()

    modes = [args.apply, args.apply_draft, args.dry_run]
    if sum(bool(m) for m in modes) > 1:
        print("Pick one of --apply, --apply-draft or --dry-run.", file=sys.stderr)
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

        if args.apply_draft:
            print()
            print("Applying DRAFT catalogue")
            print(f"  Every row is created with is_available=false. Unpriced rows carry")
            print(f"  the placeholder {DRAFT_PRICE_SENTINEL} and the {DRAFT_TAG!r} tag - that")
            print(f"  is NOT a price, and the row cannot be ordered while it is unavailable.")
            created, _ = _write(db, resolved, draft=True)
            db.commit()
            print(f"  committed - {created} draft row(s) created")
            if missing:
                print()
                print(f"  {len(missing)} row(s) still need a real price before --apply "
                      f"can put them on sale.")
            return 0

        if not args.apply:
            print()
            print("Dry run - no database changes were made"
                  if args.dry_run else
                  "Validation only (default) - no database changes were made.")
            if missing:
                print(f"--apply would be REFUSED: {len(missing)} product(s) have no price.")
                print(f"--apply-draft would create {len(CATALOGUE) and sum(len(v) for v in CATALOGUE.values())} "
                      f"unavailable draft row(s).")
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
        created, promoted = _write(db, resolved, draft=False)
        db.commit()
        print(f"  committed - {created} created, {promoted} draft row(s) priced and put on sale")
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
