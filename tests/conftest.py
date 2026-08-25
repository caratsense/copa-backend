"""
Test harness for the delivery-tracking suite.

Runs the real FastAPI app against a throwaway SQLite database and an in-process
fake Redis, so the tests exercise the actual routes, authorisation dependencies
and WebSocket handlers without needing Postgres or a Redis server.

Environment is configured before any app module is imported, because
`get_settings()` is lru_cached and the first caller freezes the configuration.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="copa-tests-")

os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TMP, 'test.db')}"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["JWT_SECRET"] = "test-secret-not-a-real-key"
os.environ["WHATSAPP_ENABLED"] = "false"
os.environ["SMS_ENABLED"] = "false"

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


# The models use Postgres JSONB. SQLite has no such type, so teach the compiler
# to emit plain JSON there — the columns are only read/written as JSON anyway.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return "JSON"


from app.core.auth import create_access_token, hash_password  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.address import Address  # noqa: E402
from app.models.order import Order, OrderStatus  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.services import delivery_tracking  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """A clean schema per test — no cross-test leakage of orders or users."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """
    Swap the tracking service's Redis client for an in-process fake.

    Patches the module-level client directly so every code path — REST, service
    and WebSocket — shares the same instance and sees the same keys.
    """
    server = fakeredis.FakeServer()
    client = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    monkeypatch.setattr(delivery_tracking, "_client", client)
    yield client


@pytest.fixture
def broken_redis(monkeypatch):
    """Simulate Redis being unreachable: every command raises."""
    class Dead:
        def __getattr__(self, name):
            def _raise(*a, **kw):
                raise ConnectionError("Redis is down")
            return _raise

    monkeypatch.setattr(delivery_tracking, "_client", Dead())
    yield


@pytest.fixture(autouse=True)
def reset_socket_managers():
    """
    Clear the module-level connection managers between tests.

    They are process-wide singletons holding order-keyed state (including the
    completed-order tombstones). Each test starts from a fresh schema and so
    reuses low order ids, which would otherwise inherit the previous test's
    verdict about "order 1".
    """
    from app.api.routes.websocket import (
        delivery_manager,
        fleet_manager,
        manager,
        rider_sockets,
    )

    def _clear():
        fleet_manager.admins.clear()
        fleet_manager.active_orders.clear()
        fleet_manager.completed_orders.clear()
        delivery_manager.watchers.clear()
        rider_sockets.sockets.clear()
        manager.active.clear()
        manager.admin_connections.clear()

    _clear()
    yield
    _clear()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """
    TestClient without lifespan.

    The app's lifespan runs Postgres-specific ALTER TABLE migrations and captures
    the event loop for the sync→async broadcast bridge; neither is wanted here,
    and the tests assert on broadcast behaviour directly instead.
    """
    with TestClient(app) as c:
        yield c


# ─── FACTORIES ────────────────────────────────────────

def make_user(db, name, role, phone=None, on_duty=True, is_active=True):
    user = User(
        name=name,
        phone=phone or f"9{abs(hash(name)) % 1000000000:09d}",
        password_hash=hash_password("test-password"),
        role=role,
        on_duty=on_duty,
        is_active=is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY, rider=None, address="12 Hazratganj, Lucknow"):
    order = Order(
        user_id=customer.id,
        status=status,
        subtotal=1000.0,
        total_price=1000.0,
        delivery_address=address,
        assigned_rider_id=rider.id if rider else None,
        payment_method="COD",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def make_address(db, user, full_address, lat, lng, flat_building=None, landmark=None):
    addr = Address(
        user_id=user.id,
        label="Home",
        full_address=full_address,
        flat_building=flat_building,
        landmark=landmark,
        latitude=lat,
        longitude=lng,
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)
    return addr


def token_for(user):
    return create_access_token(user.id, user.role.value)


def auth(user):
    return {"Authorization": f"Bearer {token_for(user)}"}


@pytest.fixture
def customer(db):
    return make_user(db, "Priya Customer", UserRole.CUSTOMER)


@pytest.fixture
def other_customer(db):
    return make_user(db, "Nosy Customer", UserRole.CUSTOMER)


@pytest.fixture
def admin(db):
    return make_user(db, "Shriya Admin", UserRole.ADMIN)


@pytest.fixture
def rider(db):
    return make_user(db, "Rahul Rider", UserRole.RIDER)


@pytest.fixture
def other_rider(db):
    return make_user(db, "Aman Rider", UserRole.RIDER)


@pytest.fixture
def baker(db):
    return make_user(db, "Bina Baker", UserRole.BAKER)
