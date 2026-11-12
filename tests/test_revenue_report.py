"""
The revenue report, which had no tests and two bugs.

WHAT WAS WRONG
--------------
It grouped with `CAST(created_at AS DATE)`. That is a PostgreSQL answer to a
question SQLite answers differently: SQLite has no DATE type, so the cast falls
back to numeric affinity, reads the leading digits of "2026-08-12 10:00:00" and
returns the integer 2026. Every row collapsed into one bucket and the endpoint
500'd on the way out. Production runs Postgres, so it worked there — the only
place it failed was a developer's machine, where it is the one admin screen
that will not load.

And `period` was accepted, pattern-validated, echoed back in the response, and
then ignored. The GROUP BY was by day whatever was asked for, so "monthly"
returned a daily series labelled monthly. Nothing called it with anything but
the default, which is the only reason nobody had noticed.

WHAT THESE PIN DOWN
-------------------
That it answers at all on the database the tests run on, that each period
actually buckets the way its name says, and that the money rule — only what
has been paid for — is the same one the dashboard tile uses. The money rule is
the part worth guarding: it has been wrong twice before, once counting
abandoned checkouts as revenue and once counting COD orders the moment they
were placed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.user import UserRole

from tests.conftest import auth, make_order, make_user


def _sale(db, customer, *, days_ago: float, total: float,
          paid=True, status=OrderStatus.DELIVERED):
    """One order, placed `days_ago` days back, for `total`."""
    order = make_order(db, customer, status=status, with_proof=False)
    order.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    order.total_price = total
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PAID if paid else PaymentStatus.PENDING
    db.commit()
    return order


def _report(client, admin, **params):
    response = client.get("/dashboard/revenue", params=params, headers=auth(admin))
    assert response.status_code == 200, response.text
    return response.json()


# ── It answers ───────────────────────────────────────────────────────────


def test_the_report_comes_back_at_all(client, db, admin, customer):
    """
    The regression itself. This 500'd on SQLite — which is to say, on every
    developer machine and in this suite — for as long as the cast was there.
    """
    _sale(db, customer, days_ago=1, total=1200.0)
    report = _report(client, admin, days=30)

    assert report["total_orders"] == 1
    assert report["total_revenue"] == 1200.0
    assert len(report["data"]) == 1


def test_an_empty_shop_reports_zero_rather_than_failing(client, admin):
    """No orders is a number, not an error."""
    report = _report(client, admin, days=30)
    assert report == {"period": "daily", "data": [],
                      "total_revenue": 0.0, "total_orders": 0}


def test_the_date_is_a_date_and_not_the_year(client, db, admin, customer):
    """
    The shape of the old failure, named.

    SQLite returned 2026 for every row. A caller charting this gets one bar
    labelled "2026" and no way to tell that anything is wrong.
    """
    _sale(db, customer, days_ago=1, total=500.0)
    (row,) = _report(client, admin, days=30)["data"]
    # Parses as a real calendar date rather than an integer.
    parsed = datetime.strptime(row["date"], "%Y-%m-%d").date()
    assert parsed == (datetime.now(timezone.utc) - timedelta(days=1)).date()


# ── Each period buckets the way its name says ────────────────────────────


def test_daily_keeps_each_day_apart(client, db, admin, customer):
    _sale(db, customer, days_ago=1, total=100.0)
    _sale(db, customer, days_ago=1, total=200.0)
    _sale(db, customer, days_ago=3, total=400.0)

    report = _report(client, admin, days=30, period="daily")
    assert len(report["data"]) == 2, report["data"]
    assert report["total_revenue"] == 700.0
    assert {d["orders"] for d in report["data"]} == {1, 2}


def test_monthly_actually_groups_by_month(client, db, admin, customer):
    """
    It did not. The GROUP BY was by day whatever was asked for, so this came
    back as a daily series with "monthly" written on the top of it.
    """
    # Far enough apart to be different months whenever this runs.
    _sale(db, customer, days_ago=2, total=100.0)
    _sale(db, customer, days_ago=40, total=200.0)
    _sale(db, customer, days_ago=41, total=300.0)

    monthly = _report(client, admin, days=90, period="monthly")
    daily = _report(client, admin, days=90, period="daily")

    assert len(daily["data"]) == 3, "precondition: three separate days"
    assert len(monthly["data"]) < len(daily["data"]), \
        "monthly returned one row per day — period is being ignored"
    assert monthly["total_revenue"] == daily["total_revenue"] == 600.0
    # A month is labelled by its first, so the label is a date a client can parse.
    assert all(d["date"].endswith("-01") for d in monthly["data"]), monthly["data"]


def test_weekly_labels_each_bucket_with_its_monday(client, db, admin, customer):
    _sale(db, customer, days_ago=1, total=100.0)
    _sale(db, customer, days_ago=20, total=250.0)

    report = _report(client, admin, days=60, period="weekly")
    assert report["total_revenue"] == 350.0
    for row in report["data"]:
        monday = datetime.strptime(row["date"], "%Y-%m-%d").date()
        assert monday.weekday() == 0, f"{row['date']} is not a Monday"


def test_the_period_asked_for_is_the_period_reported(client, db, admin, customer):
    _sale(db, customer, days_ago=1, total=100.0)
    for period in ("daily", "weekly", "monthly"):
        assert _report(client, admin, days=30, period=period)["period"] == period


def test_an_unknown_period_is_refused(client, admin):
    """Rather than silently falling through to daily."""
    assert client.get("/dashboard/revenue", params={"period": "hourly"},
                      headers=auth(admin)).status_code == 422


# ── Only money that actually arrived ─────────────────────────────────────


def test_an_abandoned_checkout_is_not_revenue(client, db, admin, customer):
    """
    This has been wrong before: every abandoned PayU session used to show up
    as a sale, which is how the dashboard reported takings the shop never had.
    """
    _sale(db, customer, days_ago=1, total=900.0, paid=True)
    _sale(db, customer, days_ago=1, total=5000.0, paid=False)

    report = _report(client, admin, days=30)
    assert report["total_revenue"] == 900.0
    assert report["total_orders"] == 1


def test_a_cancelled_order_is_not_revenue(client, db, admin, customer):
    _sale(db, customer, days_ago=1, total=700.0)
    _sale(db, customer, days_ago=1, total=2000.0, status=OrderStatus.CANCELLED)

    assert _report(client, admin, days=30)["total_revenue"] == 700.0


def test_days_is_a_window_not_a_suggestion(client, db, admin, customer):
    """An order older than the window is outside the report."""
    _sale(db, customer, days_ago=2, total=100.0)
    _sale(db, customer, days_ago=45, total=900.0)

    assert _report(client, admin, days=7)["total_revenue"] == 100.0
    assert _report(client, admin, days=90)["total_revenue"] == 1000.0


def test_the_totals_are_the_sum_of_the_rows(client, db, admin, customer):
    """
    The headline figure and the chart under it come from the same arithmetic,
    so one cannot contradict the other.
    """
    for days_ago, total in [(1, 100.0), (1, 250.5), (4, 80.25), (9, 1000.0)]:
        _sale(db, customer, days_ago=days_ago, total=total)

    report = _report(client, admin, days=30)
    assert report["total_revenue"] == round(sum(d["revenue"] for d in report["data"]), 2)
    assert report["total_orders"] == sum(d["orders"] for d in report["data"])
    assert report["total_revenue"] == 1430.75


def test_revenue_is_admin_only(client, customer):
    """Takings are not a customer's business."""
    assert client.get("/dashboard/revenue").status_code in (401, 403)
    assert client.get("/dashboard/revenue", headers=auth(customer)).status_code == 403
