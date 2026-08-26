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

# 3. Configure .env (see .env.example — every variable is documented there)
#    ENVIRONMENT=development          <- required, or the safety rules below apply
#    DATABASE_URL=postgresql://copa:copa_secret@localhost:5432/copa_db
#    REDIS_URL=redis://localhost:6379/0
#    JWT_SECRET=<anything, in development>
#    DEV_ALLOW_TEST_OTP=true          <- lets you log in with OTP 000000 locally

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

## Production Safety Rules

Three things fail CLOSED rather than falling back to a convenient default. Each
one used to be a silent bypass, so none of them can be re-enabled by accident —
every one needs `ENVIRONMENT` to be non-production **and** its own explicit flag.

| Missing configuration | What happens now | What used to happen |
|---|---|---|
| `JWT_SECRET` blank, a known placeholder, or under 16 chars | **The app refuses to start** in production | Signed sessions with a key published in this repository — anyone could mint an admin token |
| No SMS provider (`SMS_ENABLED=false` or no `TWOFACTOR_API_KEY`) | `send_otp` reports failure; `/auth/login`, `/auth/login-otp`, `/auth/resend-otp` and `/auth/forgot-password` return **503** | OTP `000000` was accepted for **every** account. With `/auth/login-otp` needing no password, a phone number alone was a full account takeover |
| `PAYU_KEY`/`PAYU_SALT` blank | `POST /payments/create-order` returns **503**; the callback refuses to settle | Every ONLINE order was marked `PAID` without collecting anything |

`ENVIRONMENT` defaults to `production`, and anything it does not recognise
(`development`, `dev`, `local`, `test`, `testing`, `ci`, `staging`) counts as
production — so a typo fails safe rather than unlocking the bypasses.

For local development set `ENVIRONMENT=development` plus `DEV_ALLOW_TEST_OTP=true`
(OTP `000000`) and `PAYU_ALLOW_DEMO_PAYMENTS=true` (simulated payments). Startup
refuses to boot if `DEV_ALLOW_TEST_OTP` is on while `ENVIRONMENT=production`.

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

## WhatsApp Integration

Inbound and outbound both run on the same order state machine the website uses.
There is no second order system: every WhatsApp command calls
`order_service.update_order_status`, so a baker replying on WhatsApp and a baker
clicking the button produce identical events, broadcasts, notifications and
delivery-tracking side effects.

### Inbound — `POST /webhook/whatsapp`

| Step | Behaviour |
|---|---|
| Authenticity | `X-Hub-Signature-256` HMAC over the raw body. **Fails closed** — every request is rejected while `WHATSAPP_APP_SECRET` is unset |
| Verification | `GET /webhook/whatsapp` with `hub.verify_token`, compared constant-time. Requires `WHATSAPP_WEBHOOK_VERIFY_TOKEN` |
| Idempotency | Meta message id in Redis (`wa:msg:{id}`, 24h, SET NX). Replays are dropped before any side effect |
| Batching | Every entry / change / message is processed, not just the first |
| Robustness | Always returns 200 after the signature check; one malformed event cannot abort its siblings or trigger a Meta retry loop |
| Sender lookup | Digits-only, exact match first, then a single unambiguous 10-digit suffix. Never a raw LIKE pattern |

**Staff commands are parsed deterministically** (`app/services/wa_commands.py`) —
never by a language model. The command must be the first word and the order id
is read after it, so prose like *"not done with 145 yet"* is not a transition.

```
admin  APPROVE <id> · REJECT <id> · PAID <id> · CANCEL <id> · ORDERS
baker  START <id>   · DONE <id>   · QUEUE
rider  PICKED <id>  · DELIVERED <id> · QUEUE
```

A verb without an order number (e.g. a bare `Completed`) is honoured only when
the sender has **exactly one** order at the matching stage. With none it says so;
with several it lists the numbers and refuses to guess.

### Outbound — templates

Template names are **not** written in route code. `app/services/wa_templates.py`
maps an internal key to a Meta template name, overridable per deployment via
`WHATSAPP_TEMPLATE_NAMES` without a code change.

`GET /dashboard/whatsapp-templates` (admin) returns the live list — names,
languages, recipients, triggers and placeholder counts — for whoever builds the
templates in WhatsApp Manager.

Every send is recorded in the `whatsapp_messages` outbox: intent first, then the
attempt, then the outcome. A Meta outage becomes a retryable backlog instead of
silence. Inspect with `GET /dashboard/whatsapp-outbox`, drain with
`POST /dashboard/whatsapp-outbox/retry`.

**A notification failure never affects order state.** Sends happen after the
transaction commits and cannot roll a transition back.

### Order events queue

`emit_event` writes an `OrderEvent` row (the durable audit trail) and also
pushes to the Redis `order_events` list. Nothing consumes that list in the
deployed configuration — Railway runs only uvicorn; `app/workers/event_worker.py`
is wired up in `docker-compose.yml` for local use. The list is therefore capped
and expired so it cannot grow without bound.

One consequence: the worker's morning queue processor does not run in
production, so orders placed off-hours are not auto-assigned at opening. The
admin dashboard surfaces them with an **Assign Now** button
(`POST /dashboard/process-queue`).

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
