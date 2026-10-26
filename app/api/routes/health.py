import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
import redis

from app.db import get_db
from app.config import get_settings

router = APIRouter(tags=["Health"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Check API, database, and Redis connectivity."""
    status = {"api": "ok", "database": "error", "redis": "error"}

    # Failures are logged, not returned: this endpoint is public, and a driver's
    # error text can carry the database host, port and user.
    # DB check
    try:
        db.execute(text("SELECT 1"))
        status["database"] = "ok"
    except Exception as e:
        logger.error("[HEALTH] Database check failed: %s", e)

    # Redis check
    try:
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.ping()
        status["redis"] = "ok"
    except Exception as e:
        logger.error("[HEALTH] Redis check failed: %s", e)

    healthy = status["database"] == "ok" and status["redis"] == "ok"
    return {"healthy": healthy, **status}
