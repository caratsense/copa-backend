"""
What it means for something to be out of stock.

Three tables answer this question — products, extras and add-on rules — and
before this they answered it three different ways. An add-on had a count. A
product had `is_available`, which does not mean "out of stock" at all: it takes
the product off the menu, so a brownie switched off at 4pm did not read as sold
out, it read as a brownie the shop had never heard of. An extra — balloons,
gift wrap, a candle — had nothing, and could be sold forever.

One mixin, so the rule is written once and all three obey it.
"""

from sqlalchemy import Boolean, Column, Integer


class StockTracked:
    """
    Two independent ways to be out, because a bakery has two.

    A brownie runs out: there were six, now there are none, and only a count
    can say so. A cake does not run out, because it has not been made yet —
    what happens is that the shop cannot do it today, and nobody is going to
    maintain a number for that. So `stock` is for the things that are counted
    and `sold_out` is a switch for the things that are not, and either one puts
    the same "Sold out" in front of the customer.

    Neither is `is_available`, which stays what it already was: not sold here
    at the moment, off the menu entirely. The difference matters to the person
    looking at the page — "Sold out" says the shop normally has this and will
    again, while a product that has silently disappeared says the page is
    broken, and the customer who had it bookmarked has no way to tell which
    happened.
    """

    # None = not counted, and most of the catalogue is not: a made-to-order
    # cake has no meaningful number, and a count somebody has to keep true for
    # no benefit will not stay true. 0 = none left.
    stock = Column(Integer, nullable=True)

    # Set by hand, cleared by hand. It deliberately survives a restock: if she
    # said "no red velvet today", more cream arriving does not change that, and
    # only she knows when it is over. A count coming back up is different — the
    # number is the fact — which is why these are two columns and not one.
    sold_out = Column(Boolean, nullable=False, server_default="false", default=False)

    @property
    def is_counted(self) -> bool:
        """Whether this row keeps a count at all."""
        return self.stock is not None

    @property
    def is_sold_out(self) -> bool:
        """
        What the customer is shown, from both routes at once.

        Derived rather than stored so the two cannot drift apart: a count that
        reaches zero IS sold out with nobody setting a flag, and a restock puts
        the item back the moment the number rises.
        """
        if self.sold_out:
            return True
        return self.stock is not None and self.stock <= 0
