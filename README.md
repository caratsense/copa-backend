# 🍰 Copa Bakery Backend

Production-ready bakery order management system built with FastAPI, PostgreSQL, and Redis.

---

## Quick Start (Docker — recommended)

```bash
# 1. Start everything (Postgres + Redis + App + Worker)
docker compose up --build

# 2. Seed the database with sample data
docker compose exec app python -m scripts.seed

# 3. Open the API docs
#    → http://localhost:8000/docs
```

That's it. The API is live at `http://localhost:8000`.

---

## Quick Start (Local — without Docker)

```bash
# Prerequisites: Python 3.12+, PostgreSQL running, Redis running

# 1. Create virtual environment
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure .env (update DATABASE_URL and REDIS_URL for local)
#    DATABASE_URL=postgresql://copa:copa_secret@localhost:5432/copa_db
#    REDIS_URL=redis://localhost:6379/0

# 4. Run migrations / create tables
python -c "from app.db import Base, engine; Base.metadata.create_all(bind=engine)"

# 5. Seed data
python -m scripts.seed

# 6. Start server
uvicorn app.main:app --reload

# 7. (Optional) Start the event worker in a separate terminal
python -m app.workers.event_worker
```

---

## API Endpoints Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | DB + Redis connectivity check |
| **Users** | | |
| `POST` | `/users` | Create a user |
| `GET` | `/users/{id}` | Get user by ID |
| `GET` | `/users` | List all users |
| **Products** | | |
| `POST` | `/products` | Create a product |
| `GET` | `/products` | List products (filter by `?category=`) |
| `GET` | `/products/{id}` | Get product |
| `PATCH` | `/products/{id}` | Update product |
| **Pricing** | | |
| `POST` | `/pricing/calculate` | Preview price with customizations |
| **Orders** | | |
| `POST` | `/orders` | Create order (auto-priced) |
| `GET` | `/orders` | List all orders |
| `GET` | `/orders/{id}` | Get order with items |
| `PATCH` | `/orders/{id}/status` | Update status (lifecycle enforced) |
| `POST` | `/orders/{id}/assign-baker` | Assign baker |
| `POST` | `/orders/{id}/assign-rider` | Assign rider |
| `GET` | `/orders/{id}/events` | Full event audit trail |
| **Admin** | | |
| `POST/GET/PATCH/DELETE` | `/admin/sizes` | Manage size rules |
| `POST/GET/PATCH/DELETE` | `/admin/flavors` | Manage flavor rules |
| `POST/GET/PATCH/DELETE` | `/admin/designs` | Manage design rules |
| `POST/GET/PATCH/DELETE` | `/admin/addons` | Manage addon rules |
| `POST/GET/PATCH/DELETE` | `/admin/rush` | Manage rush rules |
| `POST/GET/PATCH/DELETE` | `/admin/delivery-zones` | Manage delivery zones |
| **Delivery Tracking** | | |
| `GET` | `/delivery/admin/active` | Admin — every in-flight delivery + rider roster + live positions |
| `GET` | `/delivery/{id}/location` | Current rider position + ETA for one order |
| `POST` | `/delivery/{id}/start-tracking` | Manually (re)initialise tracking — normally automatic |
| `POST` | `/delivery/{id}/stop-tracking` | Manually stop tracking — normally automatic |
| **AI** | | |
| `POST` | `/ai/parse-order` | Parse natural language (stub) |

---

## Delivery Tracking

GPS never touches PostgreSQL. Positions live in Redis under `delivery:{order_id}`
(current fix), `:history` (last 10 points, for speed/ETA) and `:meta`
(rider, pickup/dropoff, status), all on a 24h TTL.

```
Rider phone ─GPS─► WS /ws/rider/{order_id} ─► Redis
                                               ├─► WS /ws/track/{order_id}   (customer)
                                               └─► WS /ws/delivery/admin     (admin fleet)
```

**Lifecycle is owned by the backend.** `PACKAGED → OUT_FOR_DELIVERY` starts
tracking (stamping the bakery origin and, where the customer's saved address has
coordinates, the destination that ETA is computed against).
`OUT_FOR_DELIVERY → DELIVERED` stops it, clears the Redis keys, notifies
watchers and closes the rider's GPS socket. No client call is required for
either.

### WebSockets

| Endpoint | Who may connect | Purpose |
|----------|-----------------|---------|
| `/ws/orders` | admin, baker, rider | Order status broadcasts (unscoped — all orders) |
| `/ws/rider/{order_id}` | the order's assigned rider, or an admin | Rider pushes `{"lat", "lng"}` |
| `/ws/track/{order_id}` | the order's customer, its rider, or an admin | Live position + ETA for one order |
| `/ws/delivery/admin` | admin only | One subscription for the whole fleet |

Authorisation is checked against the database on connect, not against the
token's claims — a demoted or deactivated user is refused immediately rather
than when their JWT expires. `/ws/track/{order_id}` and
`GET /delivery/{order_id}/location` share one rule
(`app.core.auth.can_observe_delivery`), so they cannot drift apart.

Rejections close with `4001` (unauthenticated) or `4003` (forbidden). **Browsers
do not see those codes**: uvicorn turns a close-before-accept into an HTTP 403
handshake failure, so `CloseEvent.code` is `1006` and a rejection is
indistinguishable from a network drop. Clients must not rely on the close code
to detect an authorisation failure — check the REST endpoint's status instead,
which is what the admin dashboard does before giving up on reconnecting.

`/ws/delivery/admin` sends `{"type": "snapshot", …}` on connect and then
`location_update`, `delivery_started`, `delivery_completed` and
`rider_reassigned` events. Send `"resync"` to request a fresh snapshot (used on
reconnect and tab refocus); send `"ping"` for a `"pong"`.

**Do not poll `/delivery/{id}/location` per order to build a fleet view.** Use
`GET /delivery/admin/active` once for the initial state and the admin socket for
updates.

### Tests

```bash
pip install pytest fakeredis
python -m pytest tests/ -q
```

The suite runs the real app against SQLite and an in-process fake Redis, so no
Postgres or Redis server is needed.

---

## How Pricing Works

```
price = base_price × size_multiplier
      + flavor_cost
      + design_cost
      + Σ(addon costs)
      + rush_cost
      + delivery_charge

line_total = price × quantity
```

All rules are database-driven. Change them via the Admin API — no code changes needed.

---

## How to Customize

### Add a new pricing dimension (e.g. "Tier" or "Occasion")

1. **Create the model** in `app/models/pricing.py`:
   ```python
   class OccasionRule(Base):
       __tablename__ = "occasion_rules"
       id = Column(Integer, primary_key=True, index=True)
       name = Column(String, unique=True, nullable=False)
       cost = Column(Float, nullable=False, default=0.0)
       is_active = Column(Boolean, default=True)
       created_at = Column(DateTime(timezone=True), server_default=func.now())
   ```

2. **Add schemas** in `app/schemas/__init__.py`:
   ```python
   class OccasionRuleCreate(BaseModel):
       name: str
       cost: float = 0.0

   class OccasionRuleRead(BaseModel):
       id: int; name: str; cost: float; is_active: bool
       class Config: from_attributes = True
   ```

3. **Register the model** in `app/db/__init__.py`:
   ```python
   from app.models.pricing import ..., OccasionRule  # noqa
   ```

4. **Add pricing logic** in `app/services/pricing_engine.py`:
   ```python
   occasion_cost = _lookup_or_zero(db, OccasionRule, "name", customization.occasion, "cost")
   # Add to the total
   ```

5. **Add admin CRUD** in `app/api/routes/admin.py`:
   ```python
   _build_crud("occasions", OccasionRule, OccasionRuleCreate, OccasionRuleRead)
   ```

6. **Add seed data** in `scripts/seed.py`

7. **Run migration**: `docker compose exec app alembic revision --autogenerate -m "add occasions"`

### Add a new product category

Just create it via the API — no code changes:
```bash
curl -X POST http://localhost:8000/products \
  -H "Content-Type: application/json" \
  -d '{"name": "Pastry Box", "category": "pastry", "base_price": 350, "is_customizable": true}'
```

### Plug in an LLM for AI parsing

1. Set `AI_PROVIDER=openai` in `.env`
2. Add your API key
3. Implement `_parse_with_openai()` in `app/services/ai_parser.py`
4. The `/ai/parse-order` endpoint works immediately

---

## Order Lifecycle

```
RECEIVED → CONFIRMED → ASSIGNED → IN_PRODUCTION → QC → PACKAGED → OUT_FOR_DELIVERY → DELIVERED
     ↓          ↓           ↓            ↓
  CANCELLED  CANCELLED  CANCELLED    CANCELLED
                                  QC → back to IN_PRODUCTION (rework)
```

Transitions are enforced — you can't skip steps or go backwards (except QC → rework).

---

## Project Structure

```
copa-backend/
├── app/
│   ├── main.py                    # FastAPI app + route registration
│   ├── config/settings.py         # Pydantic settings from .env
│   ├── db/                        # SQLAlchemy engine, session, base
│   ├── models/                    # ORM models (1 file per table)
│   ├── schemas/                   # Pydantic request/response schemas
│   ├── api/routes/                # API endpoint handlers
│   ├── services/                  # Business logic
│   │   ├── pricing_engine.py      # Price calculation
│   │   ├── order_service.py       # Order creation + lifecycle
│   │   ├── event_service.py       # DB + Redis event emission
│   │   ├── assignment_engine.py   # Baker/rider assignment
│   │   └── ai_parser.py           # NLP stub
│   └── workers/
│       └── event_worker.py        # Redis queue consumer
├── scripts/seed.py                # Database seed data
├── alembic/                       # Migration config
├── docker-compose.yml             # One-command setup
├── Dockerfile
├── requirements.txt
└── .env
```
