from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import PricingRequest, PricingResponse
from app.services.pricing_engine import calculate_price

router = APIRouter(prefix="/pricing", tags=["Pricing"])


@router.post("/calculate", response_model=PricingResponse)
def calculate(data: PricingRequest, db: Session = Depends(get_db)):
    """
    Calculate price for an item with customization.
    Does NOT create an order — just returns the breakdown.
    Useful for previewing price on the frontend / WhatsApp bot.
    """
    return calculate_price(db, data)
