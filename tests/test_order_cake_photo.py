"""
Every order says which cake it is a picture of.

WHAT WAS MISSING
----------------
An order line described itself in words: "1kg Vanilla Cookie Cream Cake". The
catalogue carries five vanilla cakes whose names differ by one word, and two
of them sitting on the same morning's list were told apart by reading
carefully. The photograph was in the database the whole time, on the product
row, and no staff screen asked for it.

WHY IT IS ON OrderItemRead
--------------------------
Because there are two read schemas and both take their items from here:
OrderRead for the shop and OrderTrackingRead for the customer. Putting it on
one of them is how pickups spent a release telling customers "Delivery" - the
field existed, the admin could see it, and the customer's own page could not.
"""

import pytest

from app.models.order import OrderStatus
from app.models.order_item import OrderItem
from app.models.product import Product
from tests.conftest import auth, make_order


def _product(db, name, image_url):
    product = Product(name=name, category="signature", base_price=2250.0,
                      is_available=True, image_url=image_url)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@pytest.fixture
def photographed_order(db, customer):
    """One order, one line, one cake with a photograph on file."""
    product = _product(db, "Vanilla Cookie Cream Cake", "/media/vanilla_cookie_cream.webp")
    order = make_order(db, customer, status=OrderStatus.CONFIRMED, fulfilment="PICKUP")
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1,
                     customization={"size": "1kg", "flavor": "vanilla"},
                     price=2380.0, price_breakdown={"base_price": 2250.0}))
    db.commit()
    db.refresh(order)
    return order


def test_the_shop_is_shown_the_cake_it_has_to_make(client, admin, photographed_order):
    response = client.get(f"/orders/{photographed_order.id}", headers=auth(admin))
    assert response.status_code == 200

    item = response.json()["items"][0]
    assert item["product_name"] == "Vanilla Cookie Cream Cake"
    assert item["product_image"] == "/media/vanilla_cookie_cream.webp"


def test_the_customer_is_shown_it_too(client, customer, photographed_order):
    """
    The other read schema.

    OrderTrackingRead is what the customer's own order page reads. It is a
    different class with a different field list, and the only reason it gets
    this for free is that both of them hold `items: list[OrderItemRead]`.
    """
    response = client.get(f"/orders/track/{photographed_order.id}", headers=auth(customer))
    assert response.status_code == 200
    assert response.json()["items"][0]["product_image"] == "/media/vanilla_cookie_cream.webp"


def test_the_queue_the_baker_works_from_carries_it(client, db, baker, customer):
    """
    The screen it matters most on.

    Whoever is making the cake is the person who cannot afford to confuse two
    similar names, and their queue is a third route with its own query.
    """
    product = _product(db, "Vanilla Pineapple Cake", "/media/vanilla_pineapple.webp")
    order = make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1,
                     customization={"size": "500gm"}, price=1400.0, price_breakdown={}))
    db.commit()

    response = client.get("/baker/my-queue", headers=auth(baker))
    assert response.status_code == 200, response.text

    mine = [o for o in response.json() if o["id"] == order.id]
    assert mine, response.json()
    assert mine[0]["items"][0]["product_image"] == "/media/vanilla_pineapple.webp"


def test_a_cake_with_no_photograph_uploaded_reads_as_nothing(client, db, admin, customer):
    """
    Not an error, and not an empty string.

    Most of the catalogue has no uploaded image_url at all - the client sent
    photographs as files and they ship with the frontend, matched by name. The
    screens fall back to those, so null here is the normal case and has to
    stay null rather than becoming "" or breaking the response.
    """
    product = _product(db, "A Cake Nobody Has Photographed", None)
    order = make_order(db, customer, status=OrderStatus.CONFIRMED)
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1,
                     customization={}, price=500.0, price_breakdown={}))
    db.commit()

    response = client.get(f"/orders/{order.id}", headers=auth(admin))
    assert response.status_code == 200
    assert response.json()["items"][0]["product_image"] is None


def test_renaming_or_reshooting_the_cake_moves_the_order_with_it(client, db, admin, photographed_order):
    """
    Read through the relationship, not copied onto the line.

    A re-uploaded photograph has to reach orders already placed - the kitchen
    is working from the current picture of the cake, not the one that was on
    file the day somebody ordered it.
    """
    product = db.query(Product).filter(Product.name == "Vanilla Cookie Cream Cake").first()
    product.image_url = "/media/vanilla_cookie_cream_reshot.webp"
    db.commit()

    response = client.get(f"/orders/{photographed_order.id}", headers=auth(admin))
    assert response.json()["items"][0]["product_image"] == "/media/vanilla_cookie_cream_reshot.webp"
