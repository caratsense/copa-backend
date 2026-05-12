from fastapi import APIRouter

from app.schemas import AIParseRequest, AIParseResponse
from app.services.ai_parser import parse_order_text

router = APIRouter(prefix="/ai", tags=["AI"])


@router.post("/parse-order", response_model=AIParseResponse)
def parse_order(data: AIParseRequest):
    """
    Parse natural language into structured order data.
    Currently a stub — returns raw text with 0 confidence.
    Set AI_PROVIDER in .env when you're ready to plug in an LLM.
    """
    return parse_order_text(data)
