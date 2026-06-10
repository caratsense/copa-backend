"""
Seed Script — Cake O' Clock by Shriya Mahendru
================================================
Real menu data from the bakery's flavor list PDF.

Pricing structure:
  Vanilla Base: ₹2,000/kg
  Vanilla Premium: ₹2,250/kg
  Chocolate Base: ₹2,200/kg
  Chocolate Premium: ₹2,400/kg

All cakes are 100% eggless (except Vanilla Blueberry Lemon Curd).
100% advance payment mandatory.
Minimum 1 day advance ordering.

Run: docker compose exec app python -m scripts.seed
"""

import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, engine, Base
from app.models.user import User, UserRole
from app.models.product import Product
from app.models.menu_section import MenuSection
from app.models.pricing import SizeRule, FlavorRule, DesignRule, AddonRule, RushRule
from app.models.delivery import DeliveryZone
from app.models.coupon import Coupon, DiscountType
from app.models.ingredient import Ingredient
from app.models.site_settings import SiteSettings
from app.models.extra import Extra
from app.core.auth import hash_password


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # ══════════════════════════════════════════════════
        # USERS
        # ══════════════════════════════════════════════════
        users = [
            User(name="Shriya Mahendru", phone="+919554444462",
                 email="admin@cakeoclock.in", role=UserRole.ADMIN,
                 password_hash=hash_password("admin123"), on_duty=True),
            User(name="Test Customer", phone="+919999999999",
                 email="test@cakeoclock.in", role=UserRole.CUSTOMER,
                 password_hash=hash_password("customer123")),
            User(name="Baker One", phone="+918888888881",
                 email="baker1@cakeoclock.in", role=UserRole.BAKER,
                 password_hash=hash_password("baker123"), on_duty=True),
            User(name="Baker Two", phone="+918888888882",
                 email="baker2@cakeoclock.in", role=UserRole.BAKER,
                 password_hash=hash_password("baker123"), on_duty=True),
            User(name="Rider One", phone="+917777777771",
                 email="rider1@cakeoclock.in", role=UserRole.RIDER,
                 password_hash=hash_password("rider123"), on_duty=True),
        ]

        # ══════════════════════════════════════════════════
        # MENU SECTIONS
        # ══════════════════════════════════════════════════
        sections = [
            MenuSection(name="Signature Collection", description="Our most loved cakes, handcrafted with the finest ingredients.", sort_order=1),
            MenuSection(name="Vanilla Cakes", description="Classic vanilla sponge in timeless flavor combinations.", sort_order=2),
            MenuSection(name="Chocolate Cakes", description="For the true chocolate lover — rich, indulgent, unforgettable.", sort_order=3),
            MenuSection(name="Premium Belgian", description="Imported Belgian couverture chocolate with exotic pairings.", sort_order=4),
            MenuSection(name="Specialty", description="Limited editions and signature creations.", sort_order=5),
        ]

        # ══════════════════════════════════════════════════
        # PRODUCTS — linked to sections
        # ══════════════════════════════════════════════════
        products = [
            Product(name="Vanilla Base Cake", category="vanilla-base",
                    base_price=2000, is_customizable=True, sort_order=1,
                    description="Our signature vanilla sponge with a variety of classic flavors. 100% eggless.",
                    tags=["eggless", "vanilla", "customizable"]),
            Product(name="Vanilla Premium Cake", category="vanilla-premium",
                    base_price=2250, is_customizable=True, sort_order=2,
                    description="Premium vanilla cakes with exotic flavor combinations and imported ingredients.",
                    tags=["eggless", "vanilla", "premium", "customizable"]),
            Product(name="Chocolate Base Cake", category="chocolate-base",
                    base_price=2200, is_customizable=True, sort_order=1,
                    description="Rich chocolate sponge cakes with classic flavor pairings. 100% eggless.",
                    tags=["eggless", "chocolate", "customizable"]),
            Product(name="Chocolate Premium Cake", category="chocolate-premium",
                    base_price=2400, is_customizable=True, sort_order=2,
                    description="Premium Belgian chocolate cakes — the finest cocoa, the richest flavors.",
                    tags=["eggless", "chocolate", "premium", "belgian", "customizable"]),
            Product(name="Baileys Coffee Mousse Cake", category="specialty",
                    base_price=2500, is_customizable=False, sort_order=1,
                    description="Our signature Baileys-inspired coffee mousse layered over rich chocolate.",
                    tags=["specialty", "coffee", "mousse", "bestseller"]),
        ]

        # ══════════════════════════════════════════════════
        # FLAVOR RULES — all 40+ flavors from the PDF
        # Grouped by category with appropriate extra costs
        # ══════════════════════════════════════════════════
        flavors = [
            # Vanilla Based (included in base price — ₹0 extra)
            FlavorRule(name="Plain Vanilla White Chocolate", extra_cost=0),
            FlavorRule(name="Vanilla Pineapple", extra_cost=0),
            FlavorRule(name="Vanilla Mix Fruit", extra_cost=0),
            FlavorRule(name="Vanilla Cookie Cream", extra_cost=0),
            FlavorRule(name="Vanilla Strawberry", extra_cost=0),
            FlavorRule(name="Vanilla Mango", extra_cost=0),

            # Vanilla Based Premium (₹250/kg extra over vanilla base)
            FlavorRule(name="Vanilla Orange Chocolate Crumble", extra_cost=250),
            FlavorRule(name="White Chocolate Raspberry Pistachio", extra_cost=250),
            FlavorRule(name="White Chocolate Rose Pistachio", extra_cost=250),
            FlavorRule(name="White Chocolate Apple Cinnamon", extra_cost=250),
            FlavorRule(name="Vanilla Mango Crumble", extra_cost=250),
            FlavorRule(name="Vanilla Strawberry Crumble", extra_cost=250),
            FlavorRule(name="Vanilla Pineapple Chocolate", extra_cost=250),
            FlavorRule(name="Vanilla Blueberry Crumble", extra_cost=250),
            FlavorRule(name="Vanilla Blueberry Lemon Curd", extra_cost=250),

            # Chocolate Based (included in choc base price — ₹0 extra)
            FlavorRule(name="Plain Chocolate", extra_cost=0),
            FlavorRule(name="Chocolate Coffee", extra_cost=0),
            FlavorRule(name="Chocolate Hazelnut", extra_cost=0),
            FlavorRule(name="Chocolate Irish Cream", extra_cost=0),
            FlavorRule(name="Chocolate Orange", extra_cost=0),
            FlavorRule(name="Chocolate Strawberry", extra_cost=0),
            FlavorRule(name="Chocolate Mango", extra_cost=0),

            # Chocolate Premium — Belgian (₹200/kg extra over choc base)
            FlavorRule(name="Plain Belgian Chocolate", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Irish Cream", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Cookie Cream", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Orange", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Hazelnut", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Pineapple", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Butterscotch", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Strawberry", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Mango", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Mango Crumble", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Coffee Salted Caramel", extra_cost=200),
            FlavorRule(name="Belgian Chocolate Orange Hazelnut Crumble", extra_cost=200),
        ]

        # ══════════════════════════════════════════════════
        # SIZE RULES — price per kg, so sizes are multipliers
        # ══════════════════════════════════════════════════
        sizes = [
            SizeRule(name="500g", multiplier=0.5),
            SizeRule(name="1kg", multiplier=1.0),
            SizeRule(name="1.5kg", multiplier=1.5),
            SizeRule(name="2kg", multiplier=2.0),
            SizeRule(name="3kg", multiplier=3.0),
            SizeRule(name="5kg", multiplier=5.0),
        ]

        # ══════════════════════════════════════════════════
        # DESIGN RULES
        # ══════════════════════════════════════════════════
        designs = [
            DesignRule(name="Basic Cream Finish", cost=0),
            DesignRule(name="Semi-Custom Design", cost=300),
            DesignRule(name="Full Custom Design", cost=500),
            DesignRule(name="Photo Print Cake", cost=400),
            DesignRule(name="Tiered Cake (2 tier)", cost=800),
            DesignRule(name="Tiered Cake (3 tier)", cost=1500),
        ]

        # ══════════════════════════════════════════════════
        # ADDON RULES
        # ══════════════════════════════════════════════════
        addons = [
            AddonRule(name="Cake Topper", cost=100),
            AddonRule(name="Candles Set", cost=50),
            AddonRule(name="Message Plate", cost=80),
            AddonRule(name="Sparklers", cost=60),
            AddonRule(name="Extra Frosting", cost=150),
            AddonRule(name="Fresh Flowers Decoration", cost=300),
            AddonRule(name="Fondant Figures", cost=500),
        ]

        # ══════════════════════════════════════════════════
        # RUSH RULES
        # ══════════════════════════════════════════════════
        rushes = [
            RushRule(name="Standard (24hr+)", cost=0),
            RushRule(name="Same Day", cost=300),
            RushRule(name="Express (4hr)", cost=500),
        ]

        # ══════════════════════════════════════════════════
        # DELIVERY ZONES — Lucknow areas
        # ══════════════════════════════════════════════════
        zones = [
            DeliveryZone(area_name="Pickup (Self)", charge=0, estimated_time=0),
            DeliveryZone(area_name="Lucknow Central", charge=50, estimated_time=30),
            DeliveryZone(area_name="Gomtinagar", charge=60, estimated_time=35),
            DeliveryZone(area_name="Aliganj", charge=70, estimated_time=40),
            DeliveryZone(area_name="Indira Nagar", charge=60, estimated_time=35),
            DeliveryZone(area_name="Hazratganj", charge=50, estimated_time=25),
            DeliveryZone(area_name="Aminabad", charge=55, estimated_time=30),
            DeliveryZone(area_name="Mahanagar", charge=65, estimated_time=35),
            DeliveryZone(area_name="Lucknow Outskirts", charge=150, estimated_time=75),
        ]

        # ══════════════════════════════════════════════════
        # COUPONS
        # ══════════════════════════════════════════════════
        coupons = [
            Coupon(code="WELCOME10", discount_type=DiscountType.PERCENTAGE,
                   discount_value=10, min_order_value=1500, max_discount=300,
                   max_uses=200, expires_at=datetime.now(timezone.utc) + timedelta(days=90)),
            Coupon(code="CAKE200", discount_type=DiscountType.FLAT,
                   discount_value=200, min_order_value=2000,
                   max_uses=100, expires_at=datetime.now(timezone.utc) + timedelta(days=60)),
            Coupon(code="BIRTHDAY15", discount_type=DiscountType.PERCENTAGE,
                   discount_value=15, min_order_value=2000, max_discount=500,
                   max_uses=50, expires_at=datetime.now(timezone.utc) + timedelta(days=120)),
        ]

        # ══════════════════════════════════════════════════
        # INGREDIENTS — for the showcase section
        # ══════════════════════════════════════════════════
        ingredients = [
            Ingredient(name="Belgian Chocolate", category="chocolate", is_premium=True, sort_order=1,
                       description="Rich couverture chocolate from Belgium.",
                       story="We source our Belgian chocolate from artisan chocolatiers for that deep, velvety flavor in every premium cake."),
            Ingredient(name="Madagascar Vanilla", category="extract", is_premium=True, sort_order=2,
                       description="Pure vanilla beans from Madagascar.",
                       story="Hand-selected vanilla pods from Madagascar give our cakes their signature warm, aromatic sweetness."),
            Ingredient(name="Fresh Cream", category="dairy", is_premium=False, sort_order=3,
                       description="Fresh heavy cream for silky smooth frosting.",
                       story="We use only the freshest cream — never powdered — for our buttercream and ganache."),
            Ingredient(name="Premium Pistachios", category="dry-fruit", is_premium=True, sort_order=4,
                       description="Hand-picked Iranian pistachios.",
                       story="Our pistachio flavors use premium Iranian pistachios, roasted in-house for the perfect nutty crunch."),
            Ingredient(name="Seasonal Strawberries", category="fruit", is_premium=True, sort_order=5,
                       description="Farm-fresh strawberries from Mahabaleshwar.",
                       story="Our seasonal strawberry cakes use fruit delivered fresh every morning — never frozen."),
            Ingredient(name="Organic Flour", category="flour", is_premium=False, sort_order=6,
                       description="Stone-ground organic wheat flour.",
                       story="The foundation of every Cake O' Clock creation — pure, unbleached flour for the perfect sponge."),
        ]

        # ══════════════════════════════════════════════════
        # SITE SETTINGS
        # ══════════════════════════════════════════════════
        site_settings = [
            SiteSettings(key="active_theme", value="classic",
                        metadata_json={"available": ["classic", "minimal", "rosegold", "blossom"]}),
            SiteSettings(key="store_open", value="true"),
            SiteSettings(key="store_hours_open", value="08:00"),
            SiteSettings(key="store_hours_close", value="22:00"),
            SiteSettings(key="announcement", value="",
                        metadata_json={"enabled": False}),
            SiteSettings(key="brand_name", value="Cake O' Clock"),
            SiteSettings(key="brand_tagline", value="Made with Love"),
            SiteSettings(key="brand_owner", value="Shriya Mahendru"),
            SiteSettings(key="brand_phone", value="+919554444462"),
            SiteSettings(key="advance_payment_required", value="true"),
            SiteSettings(key="min_advance_hours", value="24"),
            SiteSettings(key="eggless_default", value="true",
                        metadata_json={"exceptions": ["Vanilla Blueberry Lemon Curd"]}),
        ]

        # ══════════════════════════════════════════════════
        # EXTRAS — party add-ons
        # ══════════════════════════════════════════════════
        extras = [
            Extra(name="Balloons (5 pcs)", description="Colorful party balloons", price=150, sort_order=1),
            Extra(name="Birthday Candles", description="Number candles + sparkle candles", price=50, sort_order=2),
            Extra(name="Cake Topper", description="Custom Happy Birthday topper", price=200, sort_order=3),
            Extra(name="Knife & Plates Set", description="Cake knife + 10 paper plates", price=100, sort_order=4),
            Extra(name="Gift Wrapping", description="Premium gift box packaging", price=150, sort_order=5),
            Extra(name="Photo Frame Topper", description="Edible photo print on cake", price=350, sort_order=6),
            Extra(name="Party Popper (3 pcs)", description="Confetti poppers for celebration", price=120, sort_order=7),
        ]

        # ══════════════════════════════════════════════════
        # INSERT ALL
        # ══════════════════════════════════════════════════

        # Users first
        for item in users:
            db.merge(item)

        # Sections
        section_map = {}  # name -> db object
        for sec in sections:
            existing = db.query(MenuSection).filter(MenuSection.name == sec.name).first()
            if not existing:
                db.add(sec)
                db.flush()
                section_map[sec.name] = sec
            else:
                section_map[sec.name] = existing

        # Products — link to sections
        product_section_map = {
            "Vanilla Base Cake": "Vanilla Cakes",
            "Vanilla Premium Cake": "Vanilla Cakes",
            "Chocolate Base Cake": "Chocolate Cakes",
            "Chocolate Premium Cake": "Premium Belgian",
            "Baileys Coffee Mousse Cake": "Signature Collection",
        }
        for p in products:
            sec_name = product_section_map.get(p.name)
            if sec_name and sec_name in section_map:
                p.section_id = section_map[sec_name].id
            db.merge(p)

        # Pricing rules
        for item in sizes + designs + addons + rushes:
            db.merge(item)

        # Extras
        for extra in extras:
            existing = db.query(Extra).filter(Extra.name == extra.name).first()
            if not existing:
                db.add(extra)

        for flavor in flavors:
            existing = db.query(FlavorRule).filter(FlavorRule.name == flavor.name).first()
            if not existing:
                db.add(flavor)

        for zone in zones:
            existing = db.query(DeliveryZone).filter(DeliveryZone.area_name == zone.area_name).first()
            if not existing:
                db.add(zone)

        for coupon in coupons:
            existing = db.query(Coupon).filter(Coupon.code == coupon.code).first()
            if not existing:
                db.add(coupon)

        for ing in ingredients:
            existing = db.query(Ingredient).filter(Ingredient.name == ing.name).first()
            if not existing:
                db.add(ing)

        for setting in site_settings:
            existing = db.query(SiteSettings).filter(SiteSettings.key == setting.key).first()
            if not existing:
                db.add(setting)

        db.commit()

        print("✅ Cake O' Clock seed data loaded!")
        print(f"   {len(users)} users")
        print(f"   {len(products)} products (4 cake tiers + 1 specialty)")
        print(f"   {len(flavors)} flavors (vanilla base/premium + chocolate base/premium)")
        print(f"   {len(sizes)} sizes, {len(designs)} designs, {len(addons)} addons, {len(rushes)} rush types")
        print(f"   {len(zones)} delivery zones, {len(coupons)} coupons")
        print(f"   {len(ingredients)} ingredients, {len(site_settings)} site settings")
        print()
        print("📋 Login credentials:")
        print("   Admin (Shriya): +919554444462 / admin123")
        print("   Customer:       +919999999999 / customer123")
        print("   Baker One:      +918888888881 / baker123")
        print("   Baker Two:      +918888888882 / baker123")
        print("   Rider One:      +917777777771 / rider123")

    except Exception as e:
        db.rollback()
        print(f"❌ Seed failed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
