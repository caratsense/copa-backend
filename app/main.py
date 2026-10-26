"""
Copa Bakery Backend — Main Application (v2)

New in v2:
- JWT authentication
- CORS configured for frontend
- Admin dashboard APIs
- WebSocket for live updates
- Product image uploads (static file serving)
- Coupon system
- Rate limiting on public endpoints
"""

import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
from app.core.broadcast import set_main_loop
from app.core.ratelimit import limiter
from app.core.request_limits import BodySizeLimitMiddleware, MediaHeadersMiddleware
from app.db import Base, engine

# Import all route modules
from app.api.routes.health import router as health_router
from app.api.routes.auth import router as auth_router
from app.api.routes.users import router as users_router
from app.api.routes.products import router as products_router
from app.api.routes.pricing import router as pricing_router
from app.api.routes.orders import router as orders_router
from app.api.routes.admin import router as admin_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.coupons import router as coupons_router
from app.api.routes.uploads import router as uploads_router
from app.api.routes.websocket import router as ws_router
from app.api.routes.delivery import router as delivery_router
from app.api.routes.baker import router as baker_router
from app.api.routes.staff import router as staff_router
from app.api.routes.ingredients import router as ingredients_router
from app.api.routes.reviews import router as reviews_router
from app.api.routes.settings import router as settings_router
from app.api.routes.webhook import router as webhook_router
from app.api.routes.menu_sections import router as menu_sections_router
from app.api.routes.ai import router as ai_router
from app.api.routes.addresses import router as addresses_router
from app.api.routes.extras import router as extras_router
from app.api.routes.payments import router as payments_router
from app.api.routes.build_a_cake import router as build_a_cake_router

settings = get_settings()

# The interactive docs and the schema behind them are a complete map of the
# API, including every admin route and its parameters. Useful in development;
# in production they only help someone looking for a way in.
_docs = settings.EXPOSE_API_DOCS or not settings.is_production


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables + uploads directory on startup."""
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # Each migration in its own try/except so one failure doesn't block the rest
        for stmt in [
            "ALTER TABLE addon_rules ADD COLUMN IF NOT EXISTS stock INTEGER",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR DEFAULT 'ONLINE'",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status VARCHAR DEFAULT 'PENDING'",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_id VARCHAR",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_charge DOUBLE PRECISION DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extras_total DOUBLE PRECISION DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS extras JSONB DEFAULT '[]'::jsonb",
            # WhatsApp consent. Defaults to FALSE so existing rows are treated as
            # "no recorded opt-in" — we must not message them until they opt in.
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_opt_in BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_opt_in_at TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_opt_in_source VARCHAR",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_opt_out_at TIMESTAMPTZ",
            # Which sponge a flavour is made on, for Build a Cake. It must be
            # added here and not only by scripts/setup_build_a_cake.py: the app
            # serves traffic the moment it boots, and every query that touches
            # flavor_rules selects this column — pricing a custom cake, the
            # WhatsApp ordering flow, the admin flavour list. Adding it after
            # the deploy instead of before would leave all three answering 500
            # until someone remembered to run the script. The column is left
            # empty here; the script is still what fills it in.
            "ALTER TABLE flavor_rules ADD COLUMN IF NOT EXISTS sponge VARCHAR",
            "CREATE INDEX IF NOT EXISTS ix_flavor_rules_sponge ON flavor_rules (sponge)",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[Migration] Skipped (already exists?): {e}")
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    # Capture the running loop so sync route handlers can push WebSocket messages.
    set_main_loop(asyncio.get_running_loop())

    if not settings.cors_origins_list:
        print("[WARN] CORS_ORIGINS is empty - browser requests from your frontend will be blocked.")
    if not settings.WHATSAPP_APP_SECRET and settings.WHATSAPP_ENABLED:
        print("[WARN] WHATSAPP_APP_SECRET is not set - incoming webhooks are NOT signature-verified.")

    print(f"{settings.APP_NAME} v2 is starting...")
    print(f"   CORS allow-list: {settings.cors_origins_list}")
    yield
    print("👋 Shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Production-ready bakery order management system.\n\n"
        "**Auth:** Register/login → get JWT token → use as Bearer token.\n\n"
        "**Roles:** customer, admin, baker, rider.\n\n"
        "**Admin features:** Dashboard stats, revenue reports, order management, "
        "product/pricing/coupon CRUD, image uploads, live WebSocket updates.\n\n"
        "**Customer features:** Order placement, tracking, history, coupon validation."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

# ─── CORS ─────────────────────────────────────────────
# Explicit allow-list from CORS_ORIGINS, plus an optional regex for preview
# deploys. allow_credentials is False because auth travels in the
# Authorization header, not cookies — and "*" + credentials is rejected by
# browsers anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ─── RATE LIMITING ────────────────────────────────────
# Without the exception handler, a tripped limit surfaces as a 500 instead of a 429.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ─── REQUEST HARDENING ────────────────────────────────
# Uploads are parsed (and spooled to disk) before any handler can look at their
# size, and one of the upload routes is open to the public. Cap the body first.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=(settings.MAX_UPLOAD_MB + 1) * 1024 * 1024)
app.add_middleware(MediaHeadersMiddleware)

# ─── STATIC FILES (uploaded images) ──────────────────
# StaticFiles types a file from the OS mimetypes table, which has no entry for
# webp on the slim Python image (guess_type returns None) — so an uploaded
# .webp went out as text/plain. The browser used to sniff past that and show it
# anyway; the nosniff header added above correctly forbids that, which means
# the type has to be right at the source. Register it (and avif, next in line)
# before the mount. The client uploads webp product photos, so without this the
# first one silently fails to render.
import mimetypes
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.UPLOAD_DIR), name="media")

# ─── REGISTER ROUTES ──────────────────────────────────
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(products_router)
app.include_router(pricing_router)
app.include_router(orders_router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(coupons_router)
app.include_router(uploads_router)
app.include_router(ws_router)
app.include_router(delivery_router)
app.include_router(baker_router)
app.include_router(staff_router)
app.include_router(ingredients_router)
app.include_router(reviews_router)
app.include_router(settings_router)
app.include_router(webhook_router)
app.include_router(menu_sections_router)
app.include_router(ai_router)
app.include_router(addresses_router)
app.include_router(extras_router)
app.include_router(payments_router)
app.include_router(build_a_cake_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", tags=["Root"])
def root():
    return {
        "app": settings.APP_NAME,
        "version": "3.0.0",
        "docs": "/docs" if _docs else None,
        "health": "/health",
    }
