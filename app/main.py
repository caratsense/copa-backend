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

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware

from app.config import get_settings
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

settings = get_settings()

# Rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables + uploads directory on startup."""
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # addon_rules.stock
        conn.execute(text("ALTER TABLE addon_rules ADD COLUMN IF NOT EXISTS stock INTEGER"))
        # orders: payment columns added for COD support
        conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR DEFAULT 'ONLINE'"))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE paymentstatus AS ENUM ('PENDING','PAID','COD_PENDING','FAILED','REFUNDED');
            EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """))
        conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status paymentstatus DEFAULT 'PENDING'"))
        conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_id VARCHAR"))
        conn.commit()
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    print(f"🚀 {settings.APP_NAME} v2 is starting...")
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
)

# ─── CORS ─────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── RATE LIMITING ────────────────────────────────────
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# ─── STATIC FILES (uploaded images) ──────────────────
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", tags=["Root"])
def root():
    return {
        "app": settings.APP_NAME,
        "version": "3.0.0",
        "docs": "/docs",
        "health": "/health",
    }
