"""
Putting the synced catalogue on sale.

`--sync` populates and prices the catalogue but publishes nothing, by design.
Something still has to make the menu visible, and doing it by hand means
dozens of availability toggles in the admin and no record of which were
flipped. `--publish` is that step.

What it acts on is the `draft-unpublished` marker - "priced, but never yet on
sale" - not `is_available`. That distinction is the whole design: a product an
admin has deliberately taken off sale is also unavailable, and publishing off
availability alone would switch it back on behind their back.
"""

from scripts import client_catalogue as cc

from app.models.menu_section import MenuSection
from app.models.product import Product

import sys

import pytest


# Derived, so a revised client menu does not break assertions that are not
# about the count. test_catalogue_sync pins the literal numbers.
ALL_ITEMS = [p for ps in cc.CATALOGUE.values() for p in ps]
PRICED = [p for p in ALL_ITEMS if p.base_price is not None]
UNPRICED = [p for p in ALL_ITEMS if p.base_price is None]


@pytest.fixture
def sections(db):
    """Production's shape before a sync: seeded ids 1-5, and every section."""
    for i in range(1, 6):
        db.add(Product(name=f"Seeded Tier {i}", category="seeded", base_price=2000.0 + i,
                       pricing_unit="kg", is_available=True, tags=[]))
    db.commit()
    for i, name in enumerate(cc.FINAL_ORDER_FOR_TESTS, start=1):
        db.add(MenuSection(name=name, sort_order=i, is_active=True))
    db.commit()


def _resolved(db):
    resolved, problems = cc._validate_sections(db)
    assert not problems, problems
    return resolved


def _sync(db):
    created, report = cc._sync_catalogue(db, _resolved(db))
    db.flush()
    return created, report


def _publish(db):
    report = cc._publish(db, _resolved(db))
    db.flush()
    return report


def _catalogue_rows(db):
    return db.query(Product).filter(Product.category != "seeded").all()


# ─── THE HAPPY PATH ──────────────────────────────────

def test_publishing_puts_the_31_priced_products_on_sale(db, sections):
    _sync(db)
    report = _publish(db)

    assert len(report["published"]) == len(PRICED)
    live = [p for p in _catalogue_rows(db) if p.is_available]
    assert len(live) == len(PRICED)


def test_the_17_unpriced_products_are_not_published(db, sections):
    """
    The refusal that matters. Rs 1 is a placeholder, not a price, and these
    rows must not reach a customer at it.
    """
    _sync(db)
    report = _publish(db)

    assert len(report["still_unpriced"]) == len(UNPRICED)
    unpriced = [p for p in _catalogue_rows(db) if cc.DRAFT_TAG in (p.tags or [])]
    assert len(unpriced) == len(UNPRICED)
    assert all(p.is_available is False for p in unpriced)
    assert all(p.base_price == cc.PLACEHOLDER_PRICE for p in unpriced)


def test_publishing_clears_the_unpublished_marker(db, sections):
    """The marker is a queue, so a published row has to leave it."""
    _sync(db)
    _publish(db)

    live = [p for p in _catalogue_rows(db) if p.is_available]
    assert live
    assert all(cc.UNPUBLISHED_TAG not in (p.tags or []) for p in live)


def test_published_products_keep_their_client_price(db, sections):
    """Publishing changes availability and nothing else about the price."""
    _sync(db)
    wanted = {p.name: p.base_price
              for ps in cc.CATALOGUE.values() for p in ps if p.base_price is not None}
    _publish(db)

    for row in _catalogue_rows(db):
        if row.is_available:
            assert row.base_price == wanted[row.name], row.name


def test_the_seeded_products_are_untouched(db, sections):
    _sync(db)
    before = {p.id: (p.name, p.base_price, p.is_available)
              for p in db.query(Product).filter(Product.category == "seeded").all()}
    _publish(db)
    after = {p.id: (p.name, p.base_price, p.is_available)
             for p in db.query(Product).filter(Product.category == "seeded").all()}
    assert before == after


# ─── IDEMPOTENCE ─────────────────────────────────────

def test_a_second_publish_does_nothing(db, sections):
    _sync(db)
    _publish(db)
    again = _publish(db)

    assert again["published"] == []
    assert len(again["already_live"]) == len(PRICED)


def test_publishing_is_stable_across_a_resync(db, sections):
    """
    The sequence a second deployment actually runs: sync, publish, sync again.
    The re-sync must not undo the publish or re-queue what is already live.
    """
    _sync(db)
    _publish(db)
    _sync(db)

    live = [p for p in _catalogue_rows(db) if p.is_available]
    assert len(live) == len(PRICED)
    assert all(cc.UNPUBLISHED_TAG not in (p.tags or []) for p in live)

    again = _publish(db)
    assert again["published"] == []


# ─── WHAT IT REFUSES TO TOUCH ────────────────────────

def test_a_product_paused_by_hand_is_not_put_back_on_sale(db, sections):
    """
    The reason publishing reads the marker rather than `is_available`. An admin
    who takes a cake off sale has made a decision; a later deployment must not
    quietly reverse it.
    """
    _sync(db)
    _publish(db)

    paused = next(p for p in _catalogue_rows(db) if p.is_available)
    paused.is_available = False
    db.commit()
    paused_id, paused_name = paused.id, paused.name

    report = _publish(db)

    assert f"id {paused_id} {paused_name}" in report["paused_by_hand"]
    assert db.get(Product, paused_id).is_available is False


def test_a_resync_does_not_requeue_a_product_paused_by_hand(db, sections):
    """
    The same decision, but tested through a sync. Re-pricing a row used to
    re-stamp the marker unconditionally, which would hand a paused product
    straight back to the next publish.
    """
    _sync(db)
    _publish(db)

    paused = next(p for p in _catalogue_rows(db) if p.is_available)
    paused.is_available = False
    db.commit()
    paused_id = paused.id

    _sync(db)

    row = db.get(Product, paused_id)
    assert cc.UNPUBLISHED_TAG not in (row.tags or [])
    assert row.is_available is False

    _publish(db)
    assert db.get(Product, paused_id).is_available is False


def test_products_outside_the_catalogue_are_left_alone(db, sections):
    _sync(db)
    stranger = Product(name="Someone Else's Cake", category="other", base_price=500.0,
                       pricing_unit="kg", is_available=False,
                       tags=[cc.UNPUBLISHED_TAG])
    db.add(stranger)
    db.commit()
    stranger_id = stranger.id

    _publish(db)

    row = db.get(Product, stranger_id)
    assert row.is_available is False
    assert cc.UNPUBLISHED_TAG in (row.tags or [])


# ─── ORDERING ────────────────────────────────────────

def test_publishing_before_a_sync_reports_every_row_missing(db, sections):
    """Publishing a catalogue that was never synced is an error, not a no-op."""
    report = _publish(db)

    assert len(report["missing"]) == len(ALL_ITEMS)
    assert report["published"] == []


# ─── THE COMMAND ITSELF ──────────────────────────────

@pytest.fixture
def cli(monkeypatch):
    """
    Drive main() the way the deploy does.

    _summary is stubbed out because it counts tagged rows with the PostgreSQL
    `@>` operator, which SQLite cannot parse. That is reporting only - it reads
    nothing the modes act on - and stubbing it is what lets the argparse
    wiring, the commit and the exit code be tested at all.

    Worth testing rather than trusting: the bug that made the sync helper drift
    from `--sync` lived in exactly this layer, not in the functions under it.
    """
    monkeypatch.setattr(cc, "_summary", lambda *a, **k: None)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["client_catalogue", *argv])
        return cc.main()

    return run


def test_the_publish_command_commits(db, sections, cli):
    assert cli("--sync") == 0
    assert cli("--publish") == 0

    db.expire_all()
    live = [p for p in _catalogue_rows(db) if p.is_available]
    assert len(live) == len(PRICED)


def test_the_publish_command_dry_run_writes_nothing(db, sections, cli):
    assert cli("--sync") == 0
    assert cli("--publish", "--dry-run") == 0

    db.expire_all()
    assert [p for p in _catalogue_rows(db) if p.is_available] == []


def test_publishing_without_a_sync_exits_nonzero(db, sections, cli):
    assert cli("--publish") == 1


def test_publish_and_sync_together_are_refused(db, sections, cli):
    assert cli("--sync", "--publish") == 2


# ─── AFTER A PRICE ARRIVES ───────────────────────────

def test_a_placeholder_row_publishes_once_it_has_a_real_price(db, sections):
    """
    The path out of the placeholder. Setting a real price clears the marker -
    the admin API does this - and the next publish picks the product up.
    """
    _sync(db)
    _publish(db)

    target = next(p for p in _catalogue_rows(db) if cc.DRAFT_TAG in (p.tags or []))
    target.base_price = 420.0
    target.tags = [t for t in target.tags if t != cc.DRAFT_TAG] + [cc.UNPUBLISHED_TAG]
    db.commit()
    target_id = target.id

    report = _publish(db)

    row = db.get(Product, target_id)
    assert row.is_available is True
    assert row.base_price == 420.0
    assert len(report["published"]) == 1
