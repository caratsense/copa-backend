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
import textwrap
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product
from app.models.product_option import ProductOption


# The original seeded products. Never written to by this script.
PROTECTED_PRODUCT_IDS = {1, 2, 3, 4, 5}

PRICING_UNITS = {"kg", "fixed"}

# DRAFT ROWS AND THE PLACEHOLDER PRICE
# ------------------------------------
# products.base_price is NOT NULL, so a row for a product the client has not
# priced still has to carry some number. Rs 1 is that number: the catalogue has
# to be demonstrable and editable in the admin before every price is in, and a
# visible Rs 1 reads as obviously provisional to whoever is looking at it.
#
# Rs 1 is NOT a safe number to sell at, and nothing here pretends otherwise. It
# is not a price; it is the absence of one, written down. Three things keep it
# from ever being charged:
#
#   1. the row is created is_available=false, and create_order refuses an
#      unavailable product - that is the only route to an order;
#   2. it carries DRAFT_TAG, so "placeholder" is a fact about the row rather
#      than a guess from its value;
#   3. PATCH /products/{id} and the availability toggle refuse to publish a row
#      that is still at the placeholder price while still tagged - see
#      app/api/routes/products.py. Giving it a real price clears the tag and
#      the refusal with it, which is the normal admin flow, not a special one.
#
# That third guard is what makes this safe where it would not otherwise be.
# The previous placeholder was 999999.0, chosen to fail towards overcharging;
# Rs 1 fails the other way, so the guard carries the weight the number used to.
PLACEHOLDER_PRICE = 1.0

# Stamped on any row whose price is the placeholder rather than the client's.
# This is the authoritative marker: "is this a real price?" is answered by the
# tag, never by comparing base_price to a magic number, because an admin may
# legitimately set a product to Rs 1 one day.
DRAFT_TAG = "draft-no-price"

# Stamped on a row that HAS its real price but is deliberately not on sale yet.
# Reconciliation produces exactly that state, and without a marker for it the
# row would look finished: --apply decides what still needs publishing, and a
# priced row with no marker would be skipped and never made available. Keying
# that decision off is_available instead would be worse - it would also
# "promote" a product the owner had deliberately paused.
UNPUBLISHED_TAG = "draft-unpublished"

# Tags with a fixed meaning. Anything else is a free descriptive tag, but these
# spellings are the only accepted way to say these particular things - an
# unvalidated "contains egg" would simply stop rendering its badge.
CONTROLLED_TAGS = {
    DRAFT_TAG,
    UNPUBLISHED_TAG,
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
    # The sizes/shapes this product is sold in, as (label, price, multiplier)
    # with exactly one of price/multiplier set. Non-empty means these are the
    # ONLY sizes the product sells in - the global SizeRule table is not
    # consulted for it, which is how a cake that starts at 700g or is not sold
    # under 1kg expresses that. See app/models/product_option.py.
    options: tuple[tuple[str, Optional[float], Optional[float]], ...] = field(default_factory=tuple)


def _kg(name, category, tags=(), description=None, previous_names=(), price=None,
        options=()):
    """Priced per kilogram: `price` is the price of 1 kg, which the chosen
    SizeRule multiplies."""
    return CatalogueProduct(name=name, category=category, pricing_unit="kg",
                            base_price=price, tags=tuple(tags), description=description,
                            previous_names=tuple(previous_names), options=tuple(options))


def _fixed(name, category, tags=(), description=None, previous_names=(), price=None,
           options=()):
    """Priced as a unit: `price` is what the thing itself costs, whatever it
    weighs. No size multiplier is applied."""
    return CatalogueProduct(name=name, category=category, pricing_unit="fixed",
                            base_price=price, tags=tuple(tags), description=description,
                            previous_names=tuple(previous_names), options=tuple(options))


def _by_weight(*labels_and_multipliers):
    """Sizes of a per-kg cake. The kg price lives on the product, not here."""
    return tuple((label, None, mult) for label, mult in labels_and_multipliers)


def _priced(*labels_and_prices):
    """Sizes/shapes priced outright, because they do not scale."""
    return tuple((label, price, None) for label, price in labels_and_prices)


# ── PRICES THE CURRENT MODEL CANNOT HOLD ─────────────────────────────────
# Documentation only; nothing reads this at runtime. Every product listed here
# keeps base_price=None, so it stays an unpriced draft and cannot be put on
# sale - which is the honest outcome, because writing any single number for
# these would misprice them.
#
# A Product has exactly one base_price and one pricing_unit. That holds "Rs X
# per kg" and "Rs X per unit". It cannot hold "Rs X for this particular
# weight", and it cannot hold two prices for two shapes of the same cake.
#
# Resolving these needs either per-product sizes or a variant row per shape -
# both deliberately out of scope for now.
UNREPRESENTABLE_PRICING: dict[str, str] = {
    # Empty. Every price the client has given can now be expressed, because a
    # product can carry its own options: a weight the global size table does
    # not have (700g), a restricted list (no 500g), a price that is not per kg
    # (1.3kg for Rs 2,400) and two shapes at two prices (loaf and round).
    #
    # Kept as the place to record any future price the model cannot hold. A
    # product listed here must keep base_price=None, and validation enforces it.
}

# Fixed-price products whose single price corresponds to a specific weight the
# client quoted. base_price IS correct for these - the weight is what the
# product is, not a size the customer picks - but the weight is recorded so it
# is not lost from the menu copy.
FIXED_WEIGHTS: dict[str, str] = {
    "Whole Wheat Dates & Walnut Tea Cake": "800 g",
    "Dog Cake - 500gm": "500 g",
    "Dog Cake - 1kg": "1 kg",
}


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
            price=2400.0,
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
            price=2450.0,
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
            price=2500.0,
            # Not sold under 1kg, so 500g is simply not one of its options.
            options=_by_weight(("1kg", 1.0), ("1.5kg", 1.5), ("2kg", 2.0)),
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
            price=2400.0,
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
            price=2450.0,
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
            price=2500.0,
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
            price=2400.0,
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
        # Tagged eggless, per the client: the CAKE is eggless, and only the
        # macarons used to decorate it contain egg. The tag model is a flat list
        # with no way to qualify a claim, so tagging both would render two
        # contradictory badges on the same card. The qualification therefore
        # lives in the description, where it can be read as the sentence it is -
        # and the last paragraph below is the client's own wording for it.
        _kg("Raspberry Pistachio White Chocolate Cake", "vanilla-celebration",
            ["white-chocolate", "raspberry", "pistachio", "eggless"],
            price=2400.0,
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
            price=2380.0,
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
            price=2300.0,
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
            price=2450.0,
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
            price=2300.0,
            # Starts at 700g, which is not one of the global sizes.
            options=_by_weight(("700g", 0.7), ("1kg", 1.0), ("1.5kg", 1.5), ("2kg", 2.0)),
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
            price=2280.0,
            description=(
                "A rich and indulgent take on a classic favourite, our eggless Vanilla "
                "Butterscotch Cake brings together soft vanilla sponge and vanilla cream, "
                "finished with a generous layer of crunchy caramelised butterscotch.\n\n"
                "Buttery, creamy and delightfully crunchy, with the nostalgic sweetness of "
                "butterscotch in every bite. A timeless celebration cake that never fails "
                "to make an impression.\n\n"
                "Eggless."
            )),
        # No description supplied. Rs 2,400 is the price of the 1.3kg cake, NOT
        # a per-kg rate - so it is an outright-priced option rather than a
        # base_price, and base_price is never consulted for it. Recorded as the
        # option price so no size can scale it.
        _kg("Chiffon Fresh Fruit Milk Cake", "vanilla-celebration",
            ["chiffon", "fresh-fruit", "contains-egg"],
            price=2400.0,
            options=_priced(("1.3kg", 2400.0))),
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
        _fixed("Chocolate Walnut Brownies", "brownies", ["chocolate", "walnut", "pack-of-6"], price=750.0),
        _fixed("Cookie Cream Cakey Brownie", "brownies", ["cookie-cream", "pack-of-6"], price=750.0),
        _fixed("Chocolate Biscoff Brownie", "brownies", ["chocolate", "biscoff", "pack-of-6"], price=750.0),
        _fixed("Chip Chocolate Brownie", "brownies", ["chocolate", "choc-chip", "pack-of-6"], price=750.0),
    ),
    # Each tea cake is sold as a 500g loaf or a 1kg round at two prices that are
    # not a scaling of one another, so both are outright-priced options.
    # base_price is the loaf, which is what a menu card shows as the "from"
    # price; the option the customer picks is what they are charged.
    "Tea Cakes": (
        _fixed("Orange Cardamom Crumble", "tea-cakes", ["orange", "cardamom"],
               price=800.0, options=_priced(("500g loaf", 800.0), ("1kg round", 1700.0))),
        _fixed("Banana Chocolate Walnut", "tea-cakes", ["banana", "chocolate", "walnut"],
               price=800.0, options=_priced(("500g loaf", 800.0), ("1kg round", 1700.0))),
        _fixed("Vanilla Chocolate Pineapple", "tea-cakes", ["vanilla", "chocolate", "pineapple"],
               price=800.0, options=_priced(("500g loaf", 800.0), ("1kg round", 1700.0))),
        # The client confirmed the with-egg and without-egg versions differ in
        # price, which is why they are two products rather than one.
        _fixed("Almond Tea Cake - With Egg", "tea-cakes", ["almond", "contains-egg"],
               price=880.0, options=_priced(("500g loaf", 880.0), ("1kg round", 1850.0))),
        _fixed("Almond Tea Cake - Without Egg", "tea-cakes", ["almond"],
               price=850.0, options=_priced(("500g loaf", 850.0), ("1kg round", 1820.0))),
    ),
    "Breads & Bun Collection": (
        _fixed("Milk Bread", "breads-buns", ["bread"], price=45.0),
        _fixed("Wheat Bread", "breads-buns", ["bread", "wheat"]),
        _fixed("100% Wheat Bread", "breads-buns", ["bread", "wheat"]),
        _fixed("Brioche", "breads-buns", ["bread", "brioche"], price=250.0),
        _fixed("Chilli Cheese Garlic Babka", "breads-buns", ["babka", "savoury"], price=290.0),
        _fixed("Pesto Babka", "breads-buns", ["babka", "savoury", "pesto"], price=290.0),
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
        # "kg" pending confirmation - see the module docstring. The client has now
        # quoted this one per kg, which supports treating it as a per-kg cake.
        _kg("Sugar Free Ragi Chocolate Cake", "healthy", ["ragi", "chocolate", "sugar-free"],
            price=2500.0),
        # Rs 1,700 for an 800 g cake. Safe as a fixed price: 800 g is what this
        # product weighs, not a size the customer chooses, so base_price is
        # simply what the thing costs. The weight is kept in FIXED_WEIGHTS.
        _fixed("Whole Wheat Dates & Walnut Tea Cake", "healthy", ["whole-wheat", "dates", "walnut"],
               price=1700.0),
        # No price supplied yet.
        _fixed("Sourdough Crackers Jar", "healthy", ["sourdough", "crackers", "jar"]),
    ),
    "Dog Cakes": (
        _fixed("Dog Cake - 500gm", "dog-cakes", ["dog-treat"], price=950.0),
        _fixed("Dog Cake - 1kg", "dog-cakes", ["dog-treat"], price=1900.0),
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
            # No check against PLACEHOLDER_PRICE here: a real product may one
            # day genuinely cost Rs 1, and the tag - not the number - is what
            # distinguishes a placeholder from a price.
            for managed in (DRAFT_TAG, UNPUBLISHED_TAG):
                if managed in p.tags:
                    problems.append(
                        f"{where} ({p.name!r}): {managed!r} is applied by the script, "
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

    # Options: exactly one of price/multiplier, no duplicate labels, and a
    # product cannot be both option-priced and left unpriced.
    for section_name, products in CATALOGUE.items():
        for p in products:
            seen_labels = set()
            for label, price, mult in p.options:
                where = f"{section_name!r} ({p.name!r}) option {label!r}"
                if (price is None) == (mult is None):
                    problems.append(f"{where}: set exactly one of price or multiplier")
                if price is not None and price < 0:
                    problems.append(f"{where}: negative price")
                if mult is not None and mult <= 0:
                    problems.append(f"{where}: multiplier must be positive")
                key = " ".join(label.split()).lower()
                if key in seen_labels:
                    problems.append(f"{where}: duplicate label")
                seen_labels.add(key)
            # A multiplier option scales base_price, so base_price has to exist.
            if any(m is not None for _, _, m in p.options) and p.base_price is None:
                problems.append(
                    f"{section_name!r} ({p.name!r}): has multiplier options but no "
                    f"base_price for them to scale"
                )

    # A product whose real pricing cannot be expressed as one number must not
    # quietly acquire one. Clearing the entry from UNREPRESENTABLE_PRICING is
    # the deliberate act that unblocks it.
    for section_name, products in CATALOGUE.items():
        for p in products:
            if p.name in UNREPRESENTABLE_PRICING and p.base_price is not None:
                problems.append(
                    f"{section_name!r} ({p.name!r}): has a base_price but is listed in "
                    f"UNREPRESENTABLE_PRICING. One number cannot describe its real "
                    f"pricing - resolve the pricing model first, then remove the entry."
                )

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

    drafts = db.query(Product).filter(Product.tags.contains([DRAFT_TAG])).count()
    if drafts:
        print()
        print(f"Draft rows in the database: {drafts} (unavailable, placeholder price "
              f"{PLACEHOLDER_PRICE}, tagged {DRAFT_TAG!r}).")
        print("  They cannot be ordered and do not appear on the menu, in WhatsApp")
        print("  or in Build-a-Cake. --apply will price them and put them on sale.")

    with_options = [(s_, p) for s_, ps in CATALOGUE.items() for p in ps if p.options]
    if with_options:
        print()
        print(f"Products with their own sizes ({len(with_options)}) - these do NOT use")
        print("the global size list, so anything not listed here is not selectable:")
        for section_name, item in with_options:
            shown = ", ".join(
                f"{label} {'Rs ' + format(price, ',.0f') if price is not None else f'x{mult}'}"
                for label, price, mult in item.options
            )
            print(f"  - {item.name}: {shown}")

    blocked = [n for n in UNREPRESENTABLE_PRICING]
    print()
    print("Prices the client has given that this model cannot hold "
          f"({len(blocked)} products, all left unpriced):")
    for name in blocked:
        note = " ".join(UNREPRESENTABLE_PRICING[name].split())
        print(f"  - {name}:")
        for line in textwrap.wrap(note, width=72):
            print(f"      {line}")

    print()
    print("Unresolved / pending client confirmation:")
    missing = _missing_prices()
    print(f"  - {total - len(missing)} of {total} products now have a confirmed price.")
    print(f"    {len(missing)} still have base_price=None; nothing goes on sale until")
    print(f"    every one is filled in.")
    print("  - Tea Cakes are 'fixed': the client prices them as a 500 g loaf or a")
    print("    1 kg round, which are shapes rather than weights chosen at checkout.")
    print("  - Sugar Free Ragi Chocolate Cake is 'kg': the client quoted it per kg.")
    print("  - Button/Jumbo cookies are separate rows and still have no prices.")
    print("  - Product names are Title Cased; the client's source mixes cases.")
    print("  - 'eggless' is applied only where the client stated it. Raspberry")
    print("    Pistachio is eggless with egg-containing macaron decoration; the tag")
    print("    model cannot qualify a claim, so that sits in its description.")


# ── WRITE (only with --apply, only when fully priced) ─────────────────────


def _sync_options(row: Product, item: CatalogueProduct) -> int:
    """
    Give an existing row the sizes the catalogue says it sells in.

    Additive and idempotent: an option already on the row by label is left
    exactly as it is, so a price corrected by hand in the admin is not silently
    reverted by a re-run. Nothing is removed - an option the catalogue has
    dropped is reported rather than deleted, because a customer may have
    ordered it.
    """
    existing = {" ".join(o.label.split()).lower() for o in row.options}
    added = 0
    for i, (label, opt_price, opt_mult) in enumerate(item.options, start=1):
        if " ".join(label.split()).lower() in existing:
            continue
        print(f"  OPTION   {row.name} (id {row.id}): + {label} "
              f"{'Rs ' + format(opt_price, ',.0f') if opt_price is not None else f'x{opt_mult}'}")
        row.options.append(ProductOption(label=label, price=opt_price,
                                         multiplier=opt_mult, sort_order=i,
                                         is_active=True))
        added += 1

    wanted = {" ".join(l.split()).lower() for l, _, _ in item.options}
    for o in row.options:
        if o.id is not None and " ".join(o.label.split()).lower() not in wanted:
            print(f"  NOTE     {row.name} (id {row.id}): option {o.label!r} is on the row "
                  f"but not in the catalogue. Left alone - deactivate it by hand if "
                  f"it should no longer sell.")
    return added


def _is_draft_row(row: Product) -> bool:
    """
    A row this script put in place that is not yet on sale.

    Either it never had a real price (the sentinel / DRAFT_TAG), or it has been
    reconciled to its real price but deliberately left unpublished
    (UNPUBLISHED_TAG). Both still need --apply to put them on sale; neither is
    a product the owner has simply paused, which carries no tag at all.
    """
    tags = row.tags or []
    return DRAFT_TAG in tags or UNPUBLISHED_TAG in tags


def _match_existing(existing: dict, item: CatalogueProduct):
    """
    Find the row for a catalogue product: by current name, else by a name it
    used to be written under. Returns (row, matched_by) where matched_by is
    "name", a previous name, or None.

    Shared by the write and reconcile paths so the two cannot drift about what
    counts as the same product.
    """
    row = existing.get(item.name.strip().lower())
    if row is not None:
        return row, "name"
    for old_name in item.previous_names:
        row = existing.get(old_name.strip().lower())
        if row is not None:
            return row, old_name
    return None, None


def _write(db, resolved: dict[str, MenuSection], draft: bool) -> tuple[int, int]:
    """
    Create missing catalogue products; in real mode, price up existing drafts.

    Returns (created, promoted). Never deletes, never renames, and never writes
    to a product that is not part of this catalogue.
    """
    created = promoted = 0
    # Options are attached when a row is created, and reconciled when a draft
    # row is priced up. A draft pass deliberately does not touch a row that
    # already exists - see the module docstring.

    for section_name, products in CATALOGUE.items():
        section = resolved[section_name]
        existing = {
            p.name.strip().lower(): p
            for p in db.query(Product).filter(Product.section_id == section.id).all()
        }

        for position, item in enumerate(products, start=1):
            # Matched by current name, or by a name it used to be written
            # under, so a rename cannot quietly produce a second row for the
            # same cake. --apply-draft does NOT rename the row; --reconcile-draft
            # is the mode that does.
            match, matched_by = _match_existing(existing, item)
            renamed_from = matched_by if matched_by not in (None, "name") else None

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
                _sync_options(match, item)
                match.tags = [t for t in (match.tags or [])
                              if t not in (DRAFT_TAG, UNPUBLISHED_TAG)]
                match.is_available = True
                promoted += 1
                continue

            price = item.base_price if item.base_price is not None else PLACEHOLDER_PRICE
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
            product = Product(
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
            )
            for i, (label, opt_price, opt_mult) in enumerate(item.options, start=1):
                product.options.append(ProductOption(
                    label=label, price=opt_price, multiplier=opt_mult,
                    sort_order=i, is_active=True,
                ))
            db.add(product)
            created += 1

    return created, promoted


def _reconcile(db, resolved: dict[str, MenuSection]) -> dict:
    """
    Bring the existing draft rows in line with the catalogue, WITHOUT publishing.

    This is the local step between "the rows exist" and "the menu is live". It
    renames, prices and gives rows their options, and it never sets
    is_available - so nothing it does can put a product in front of a customer.
    Publishing stays the job of --apply, which still refuses while any price is
    missing.

    What it will not do:
      * create a row - a catalogue product with no matching row is an error,
        because it means the database and the catalogue have diverged and
        --apply-draft should have run first
      * touch products 1-5
      * overwrite an option price that already exists - a correction made by
        hand in the admin survives a re-run, and the difference is reported
        instead
      * delete an option - one may already be on an order

    Idempotent: a second run finds everything in place and changes nothing.
    """
    report = {
        "matched_by_name": [], "matched_by_previous_name": [], "renamed": [],
        "repriced": [], "options_added": [], "option_differences": [],
        "left_unpriced": [], "left_unavailable": [], "created": [],
        "placeholder_applied": [],
        "protected_touched": [], "missing": [],
    }

    for section_name, products in CATALOGUE.items():
        section = resolved[section_name]
        existing = {
            p.name.strip().lower(): p
            for p in db.query(Product).filter(Product.section_id == section.id).all()
        }

        for item in products:
            match, matched_by = _match_existing(existing, item)

            if match is None:
                report["missing"].append(f"{section_name} / {item.name}")
                continue

            if match.id in PROTECTED_PRODUCT_IDS:
                report["protected_touched"].append(f"id {match.id} ({match.name})")
                raise ValidationError(
                    f"{item.name!r} matches protected product id {match.id}. "
                    f"Refusing to touch the original seeded products."
                )

            where = f"{section_name} / {item.name}"
            if matched_by == "name":
                report["matched_by_name"].append(where)
            else:
                report["matched_by_previous_name"].append(f"{where} (was {matched_by!r})")

            # ── Name ──
            if match.name != item.name:
                report["renamed"].append(f"id {match.id}: {match.name!r} -> {item.name!r}")
                match.name = item.name

            # ── Price ──
            if item.base_price is not None:
                if match.base_price != item.base_price:
                    report["repriced"].append(
                        f"id {match.id} {item.name}: {match.base_price} -> {item.base_price}"
                    )
                    match.base_price = item.base_price
                # Priced now, so the "no price" marker would be a lie. Replaced
                # with the marker that says what is actually true: priced, but
                # not yet on sale.
                tags = [t for t in (match.tags or []) if t != DRAFT_TAG]
                if UNPUBLISHED_TAG not in tags:
                    tags.append(UNPUBLISHED_TAG)
                match.tags = tags
            else:
                # No price supplied by the client. The row sits at the
                # placeholder so the product can still be seen and edited in
                # the admin, tagged so nothing mistakes that for a price, and
                # unavailable so it cannot be sold. Availability is not touched
                # here - an admin who has deliberately taken something off sale
                # keeps that.
                if match.base_price != PLACEHOLDER_PRICE:
                    report["placeholder_applied"].append(
                        f"id {match.id} {item.name}: {match.base_price} -> {PLACEHOLDER_PRICE}"
                    )
                    match.base_price = PLACEHOLDER_PRICE
                if DRAFT_TAG not in (match.tags or []):
                    match.tags = list(match.tags or []) + [DRAFT_TAG]
                report["left_unpriced"].append(f"id {match.id} {item.name}")

            # ── Options ──
            for added in _plan_options(match, item):
                if added["action"] == "add":
                    report["options_added"].append(
                        f"id {match.id} {item.name}: + {added['label']}"
                    )
                    match.options.append(ProductOption(
                        label=added["label"], price=added["price"],
                        multiplier=added["multiplier"], sort_order=added["sort_order"],
                        is_active=True,
                    ))
                else:
                    report["option_differences"].append(added["note"])

            # ── Availability: never touched ──
            if not match.is_available:
                report["left_unavailable"].append(f"id {match.id} {item.name}")

    return report


def _plan_options(row: Product, item: CatalogueProduct) -> list[dict]:
    """
    What to do about this row's options: add the missing ones, and report - not
    overwrite - any whose stored value differs from the catalogue. An option
    already on the row may have been corrected by hand, or already sold.
    """
    by_label = {" ".join(o.label.split()).lower(): o for o in row.options}
    plan = []
    for i, (label, price, mult) in enumerate(item.options, start=1):
        key = " ".join(label.split()).lower()
        current = by_label.get(key)
        if current is None:
            plan.append({"action": "add", "label": label, "price": price,
                         "multiplier": mult, "sort_order": i})
            continue
        if current.price != price or current.multiplier != mult:
            plan.append({"action": "differs", "note": (
                f"id {row.id} {row.name}: option {label!r} is "
                f"{'Rs ' + str(current.price) if current.price is not None else 'x' + str(current.multiplier)} "
                f"in the database, catalogue says "
                f"{'Rs ' + str(price) if price is not None else 'x' + str(mult)}. "
                f"Left as it is - change it in the admin if the database is wrong."
            )})
    return plan


def _print_reconcile_report(report: dict, dry_run: bool) -> None:
    def block(title, key, empty="none"):
        rows = report[key]
        print(f"  {title}: {len(rows)}")
        for r in rows:
            print(f"      {r}")
        if not rows:
            print(f"      ({empty})")

    print()
    print("Reconciliation plan" if dry_run else "Reconciliation")
    print(f"  matched by current name : {len(report['matched_by_name'])}")
    print(f"  matched by previous name: {len(report['matched_by_previous_name'])}")
    for r in report["matched_by_previous_name"]:
        print(f"      {r}")
    print()
    block("renamed", "renamed")
    print()
    block("price changes", "repriced")
    print()
    block("options to attach", "options_added")
    print()
    block("placeholder price applied (no client price yet)", "placeholder_applied")
    if report["option_differences"]:
        print()
        block("options that DIFFER (left alone)", "option_differences")
    print()
    print(f"  left unpriced (placeholder, draft tag): {len(report['left_unpriced'])}")
    print(f"  left unavailable                      : {len(report['left_unavailable'])}")
    print(f"  products created                      : {len(report['created'])} (must be 0)")
    print(f"  protected products touched            : {len(report['protected_touched'])} (must be 0)")
    if report["missing"]:
        print(f"  MISSING rows (catalogue has no match) : {len(report['missing'])}")
        for r in report["missing"]:
            print(f"      {r}")


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
        "--reconcile-draft", action="store_true",
        help=(
            "bring existing draft rows in line with the catalogue WITHOUT "
            "publishing: rename, apply confirmed prices, attach options. Never "
            "sets is_available, never creates a row, never touches products "
            "1-5. Rows with no confirmed price keep their placeholder and stay "
            "unavailable. Idempotent."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "plan only. On its own it validates and reports; combined with a "
            "write mode it runs that mode in a transaction and rolls it back, "
            "so the exact plan can be read before anything is kept."
        ),
    )
    args = parser.parse_args()

    write_modes = [args.apply, args.apply_draft, args.reconcile_draft]
    if sum(bool(m) for m in write_modes) > 1:
        print("Pick one of --apply, --apply-draft or --reconcile-draft.", file=sys.stderr)
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

        if args.reconcile_draft:
            report = _reconcile(db, resolved)
            _print_reconcile_report(report, dry_run=args.dry_run)

            if report["missing"]:
                db.rollback()
                print()
                print("ABORTED: the catalogue has products with no row in the database.")
                print("Run --apply-draft first to create them.")
                return 1
            if report["protected_touched"]:
                db.rollback()
                print("\nABORTED: refused to touch a protected product.")
                return 2

            if args.dry_run:
                db.rollback()
                print()
                print("Dry run - rolled back. Nothing was written.")
                return 0

            db.commit()
            print()
            print("  committed - no product was made available; publishing is still --apply.")
            return 0

        if args.apply_draft:
            print()
            print("Applying DRAFT catalogue")
            print(f"  Every row is created with is_available=false. Unpriced rows carry")
            print(f"  the placeholder {PLACEHOLDER_PRICE} and the {DRAFT_TAG!r} tag - that")
            print(f"  is NOT a price, and the row cannot be ordered while it is unavailable.")
            created, _ = _write(db, resolved, draft=True)
            if args.dry_run:
                db.rollback()
                print(f"  dry run - rolled back; {created} row(s) would have been created")
                return 0
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
            print(f"--reconcile-draft would align {sum(len(v) for v in CATALOGUE.values())} "
                  f"existing row(s) without publishing any.")
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
        if args.dry_run:
            db.rollback()
            print(f"  dry run - rolled back; {created} created, {promoted} would be published")
            return 0
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
