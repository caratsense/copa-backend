"""
Pydantic schemas for request/response validation.
Organized by domain — add new schemas at the bottom of each section.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ─── AUTH ─────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    phone: str
    password: str
    email: Optional[str] = None
    date_of_birth: Optional[str] = None  # "YYYY-MM-DD"
    role: str = "customer"

class LoginRequest(BaseModel):
    phone: str
    password: str
    device_fingerprint: Optional[str] = None   # for trusted device check

class LoginResponse(BaseModel):
    """Step 1 response — either full token (trusted device) or OTP required."""
    requires_otp: bool
    temp_token: Optional[str] = None           # temporary token for OTP step
    access_token: Optional[str] = None         # full JWT (if OTP skipped)
    token_type: str = "bearer"
    user: Optional[UserRead] = None
    message: str = ""

class OTPVerifyRequest(BaseModel):
    temp_token: str
    otp: str
    device_fingerprint: Optional[str] = None   # to save as trusted after verify

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead = None  # forward ref, resolved below


# ─── USERS ────────────────────────────────────────────

class UserCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    role: str = "customer"

class UserRead(BaseModel):
    id: int
    name: str
    phone: str
    email: Optional[str]
    role: str
    is_active: bool = True
    on_duty: bool = True
    created_at: datetime

    class Config:
        from_attributes = True

# Staff management (admin creates baker/rider)
class StaffCreate(BaseModel):
    name: str
    phone: str
    password: str
    email: Optional[str] = None
    role: str     # "baker" | "rider"

class StaffRead(BaseModel):
    id: int
    name: str
    phone: str
    email: Optional[str]
    role: str
    is_active: bool
    on_duty: bool
    active_order_count: int = 0
    class Config:
        from_attributes = True

class DutyToggle(BaseModel):
    on_duty: bool

class TransferRequest(BaseModel):
    to_baker_id: int

# Resolve forward ref
TokenResponse.model_rebuild()


# ─── PRODUCTS ─────────────────────────────────────────

class ProductCreate(BaseModel):
    name: str
    category: str
    description: Optional[str] = None
    base_price: float
    is_customizable: bool = True
    is_available: bool = True
    tags: list[str] = Field(default_factory=list)

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    base_price: Optional[float] = None
    is_customizable: Optional[bool] = None
    is_available: Optional[bool] = None
    tags: Optional[list[str]] = None

class ProductRead(BaseModel):
    id: int
    name: str
    category: str
    description: Optional[str]
    base_price: float
    is_customizable: bool
    is_available: bool
    image_url: Optional[str]
    tags: list[str] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


# ─── PRICING RULES ────────────────────────────────────

class SizeRuleCreate(BaseModel):
    name: str
    multiplier: float = 1.0

class SizeRuleRead(BaseModel):
    id: int
    name: str
    multiplier: float
    is_active: bool
    class Config:
        from_attributes = True

class FlavorRuleCreate(BaseModel):
    name: str
    extra_cost: float = 0.0

class FlavorRuleRead(BaseModel):
    id: int
    name: str
    extra_cost: float
    is_active: bool
    class Config:
        from_attributes = True

class DesignRuleCreate(BaseModel):
    name: str
    cost: float = 0.0

class DesignRuleRead(BaseModel):
    id: int
    name: str
    cost: float
    is_active: bool
    class Config:
        from_attributes = True

class AddonRuleCreate(BaseModel):
    name: str
    cost: float = 0.0

class AddonRuleRead(BaseModel):
    id: int
    name: str
    cost: float
    is_active: bool
    class Config:
        from_attributes = True

class RushRuleCreate(BaseModel):
    name: str
    cost: float = 0.0

class RushRuleRead(BaseModel):
    id: int
    name: str
    cost: float
    is_active: bool
    class Config:
        from_attributes = True


# ─── DELIVERY ZONES ──────────────────────────────────

class DeliveryZoneCreate(BaseModel):
    area_name: str
    charge: float = 0.0
    estimated_time: int = 60

class DeliveryZoneRead(BaseModel):
    id: int
    area_name: str
    charge: float
    estimated_time: int
    is_active: bool
    class Config:
        from_attributes = True


# ─── COUPONS ─────────────────────────────────────────

class CouponCreate(BaseModel):
    code: str
    discount_type: str = "flat"       # "flat" | "percentage"
    discount_value: float
    min_order_value: float = 0.0
    max_discount: Optional[float] = None
    max_uses: Optional[int] = None
    expires_at: Optional[datetime] = None

class CouponRead(BaseModel):
    id: int
    code: str
    discount_type: str
    discount_value: float
    min_order_value: float
    max_discount: Optional[float]
    max_uses: Optional[int]
    used_count: int
    is_active: bool
    expires_at: Optional[datetime]
    created_at: datetime
    class Config:
        from_attributes = True

class CouponApplyRequest(BaseModel):
    code: str
    order_total: float

class CouponApplyResponse(BaseModel):
    valid: bool
    discount: float = 0.0
    message: str = ""


# ─── PRICING CALCULATOR ──────────────────────────────

class ItemCustomization(BaseModel):
    size: str = "1kg"
    flavor: str = "vanilla"
    design: str = "basic"
    addons: list[str] = Field(default_factory=list)
    rush: str = "standard"

class PricingRequest(BaseModel):
    product_id: int
    quantity: int = 1
    customization: ItemCustomization = Field(default_factory=ItemCustomization)
    delivery_zone: Optional[str] = None

class PriceBreakdown(BaseModel):
    base_price: float
    size_multiplier: float
    size_adjusted: float
    flavor_cost: float
    design_cost: float
    addon_cost: float
    addon_details: dict[str, float] = Field(default_factory=dict)
    rush_cost: float
    delivery_charge: float
    item_total: float
    quantity: int
    line_total: float

class PricingResponse(BaseModel):
    breakdown: PriceBreakdown
    total: float


# ─── ORDERS ───────────────────────────────────────────

class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int = 1
    customization: ItemCustomization = Field(default_factory=ItemCustomization)

class OrderCreate(BaseModel):
    user_id: Optional[int] = None
    items: list[OrderItemCreate]
    delivery_address: Optional[str] = None
    delivery_time: Optional[datetime] = None
    delivery_zone: Optional[str] = None
    coupon_code: Optional[str] = None
    notes: Optional[str] = None

class OrderItemRead(BaseModel):
    id: int
    product_id: int
    quantity: int
    customization: dict
    price: float
    price_breakdown: dict
    class Config:
        from_attributes = True

class OrderRead(BaseModel):
    id: int
    user_id: int
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    status: str
    subtotal: float
    discount: float
    total_price: float
    coupon_code: Optional[str]
    delivery_address: Optional[str]
    delivery_time: Optional[datetime]
    delivery_zone_id: Optional[int]
    payment_status: str
    payment_method: Optional[str] = "ONLINE"
    assigned_baker_id: Optional[int]
    assigned_rider_id: Optional[int]
    baker_name: Optional[str] = None
    rider_name: Optional[str] = None
    notes: Optional[str]
    items: list[OrderItemRead] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_order(cls, order):
        """Build from ORM order with baker/rider names resolved."""
        data = {c.key: getattr(order, c.key) for c in order.__table__.columns}
        data["items"] = order.items
        data["baker_name"] = order.baker.name if order.baker else None
        data["rider_name"] = order.rider.name if order.rider else None
        data["status"] = order.status.value if hasattr(order.status, 'value') else order.status
        data["payment_status"] = order.payment_status.value if hasattr(order.payment_status, 'value') else order.payment_status
        return cls(**data)

class StatusUpdate(BaseModel):
    status: str

class PaymentUpdate(BaseModel):
    payment_status: str   # "PAID" | "REFUNDED"


# ─── ORDER TRACKING (CUSTOMER VIEW) ──────────────────

class OrderTrackingRead(BaseModel):
    """Lightweight order view for customers — no internal fields."""
    id: int
    status: str
    total_price: float
    delivery_address: Optional[str]
    delivery_time: Optional[datetime]
    payment_status: str
    created_at: datetime
    items: list[OrderItemRead] = []
    class Config:
        from_attributes = True


# ─── EVENTS ───────────────────────────────────────────

class EventRead(BaseModel):
    id: int
    order_id: int
    event_type: str
    payload: dict
    created_at: datetime
    class Config:
        from_attributes = True


# ─── DASHBOARD ────────────────────────────────────────

class DashboardStats(BaseModel):
    today_orders: int
    today_revenue: float
    pending_orders: int
    in_production_orders: int
    out_for_delivery_orders: int
    delivered_today: int
    cancelled_today: int
    total_users: int

class RevenueReport(BaseModel):
    period: str            # "daily" | "weekly" | "monthly"
    data: list[dict]       # [{"date": "2026-04-01", "revenue": 5000, "orders": 12}, ...]
    total_revenue: float
    total_orders: int


# ─── AI PARSE (STUB) ─────────────────────────────────

class AIParseRequest(BaseModel):
    text: str

class AIParseResponse(BaseModel):
    size: Optional[str] = None
    flavor: Optional[str] = None
    design: Optional[str] = None
    addons: list[str] = Field(default_factory=list)
    rush: Optional[str] = None
    date: Optional[str] = None
    raw_text: str
    confidence: float = 0.0
    provider: str = "stub"


# ─── INGREDIENTS ──────────────────────────────────────

class IngredientCreate(BaseModel):
    name: str
    description: Optional[str] = None
    story: Optional[str] = None
    category: Optional[str] = None
    is_premium: bool = False
    sort_order: int = 0

class IngredientUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    story: Optional[str] = None
    category: Optional[str] = None
    is_premium: Optional[bool] = None
    sort_order: Optional[int] = None

class IngredientRead(BaseModel):
    id: int
    name: str
    image_url: Optional[str]
    description: Optional[str]
    story: Optional[str]
    category: Optional[str]
    is_premium: bool
    is_active: bool
    sort_order: int
    class Config:
        from_attributes = True


# ─── REVIEWS ─────────────────────────────────────────

class ReviewCreate(BaseModel):
    order_id: Optional[int] = None
    product_id: Optional[int] = None
    rating: float = Field(ge=1.0, le=5.0)
    comment: Optional[str] = None

class ReviewRead(BaseModel):
    id: int
    order_id: Optional[int] = None
    product_id: Optional[int] = None
    user_id: int
    customer_name: str = ""
    rating: float
    comment: Optional[str]
    image_url: Optional[str]
    is_approved: bool
    is_featured: bool
    admin_reply: Optional[str]
    created_at: datetime
    class Config:
        from_attributes = True

class ReviewAdminAction(BaseModel):
    is_approved: Optional[bool] = None
    is_featured: Optional[bool] = None
    admin_reply: Optional[str] = None


# ─── SITE SETTINGS ───────────────────────────────────

class SiteSettingRead(BaseModel):
    key: str
    value: str
    metadata_json: dict = Field(default_factory=dict)
    class Config:
        from_attributes = True

class SiteSettingUpdate(BaseModel):
    value: str
    metadata_json: Optional[dict] = None

# ─── ADDRESSES ───────────────────────────────────────

class AddressCreate(BaseModel):
    label: str = "Home"
    full_address: str
    flat_building: Optional[str] = None
    landmark: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    is_default: bool = False

class AddressRead(BaseModel):
    id: int
    user_id: int
    label: str
    full_address: str
    flat_building: Optional[str]
    landmark: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    is_default: bool
    class Config:
        from_attributes = True

# ─── EXTRAS ──────────────────────────────────────────

class ExtraCreate(BaseModel):
    name: str
    description: Optional[str] = None
    price: float = 0.0

class ExtraRead(BaseModel):
    id: int
    name: str
    description: Optional[str]
    price: float
    image_url: Optional[str]
    is_active: bool
    sort_order: int
    class Config:
        from_attributes = True
