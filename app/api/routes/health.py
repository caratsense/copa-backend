from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
import redis

from app.db import get_db
from app.config import get_settings

router = APIRouter(tags=["Health"])
settings = get_settings()


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Check API, database, and Redis connectivity."""
    status = {"api": "ok", "database": "error", "redis": "error"}

    # DB check
    try:
        db.execute(text("SELECT 1"))
        status["database"] = "ok"
    except Exception as e:
        status["database"] = str(e)

    # Redis check
    try:
        r = redis.from_url(settings.REDIS_URL, decode_responses=True)
        r.ping()
        status["redis"] = "ok"
    except Exception as e:
        status["redis"] = str(e)

    healthy = status["database"] == "ok" and status["redis"] == "ok"
    return {"healthy": healthy, **status}
