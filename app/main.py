import uuid
import time
import logging
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.database import init_db, get_db, engine
from app.models import (
    IngestRequest, IngestResponse,
    StoreMetrics, FunnelResponse, HeatmapResponse,
    AnomaliesResponse, HealthResponse,
)
from app.ingestion import ingest_events, load_pos_transactions
from app.metrics import get_store_metrics
from app.funnel import get_store_funnel
from app.heatmap import get_store_heatmap
from app.anomalies import get_anomalies
from app.health import get_health


# ── Structured JSON logging ────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("trace_id", "store_id", "endpoint", "latency_ms",
                    "event_count", "status_code"):
            if hasattr(record, key):
                log[key] = getattr(record, key)
        return json.dumps(log)


handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("store_intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Auto-load POS data if available
    pos_path = os.getenv("POS_CSV_PATH", "dataset/pos_transactions.csv")
    if os.path.exists(pos_path):
        from app.database import SessionLocal
        with SessionLocal() as db:
            load_pos_transactions(pos_path, db)
    yield


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time retail store analytics — Apex Retail",
    lifespan=lifespan,
)


# ── Middleware: trace_id + structured request logging ─────────────────────────
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    start = time.perf_counter()

    response = await call_next(request)

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    store_id = request.path_params.get("store_id", "-")

    logger.info(
        "request",
        extra={
            "trace_id": trace_id,
            "store_id": store_id,
            "endpoint": request.url.path,
            "latency_ms": latency_ms,
            "status_code": response.status_code,
        }
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


# ── DB error handler — never expose stack traces ──────────────────────────────
@app.exception_handler(OperationalError)
async def db_error_handler(request: Request, exc: OperationalError):
    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": "Database temporarily unavailable. Please retry shortly.",
            "trace_id": getattr(request.state, "trace_id", None),
        }
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred.",
            "trace_id": getattr(request.state, "trace_id", None),
        }
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/events/ingest", response_model=IngestResponse)
def ingest(
    request_body: IngestRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Ingest up to 500 events per call.
    Idempotent by event_id — safe to call multiple times with same payload.
    Returns partial success on malformed events.
    """
    result = ingest_events(request_body, db)
    first = request_body.events[0] if request_body.events else {}
    store_id_log = first.get("store_id", "-") if isinstance(first, dict) else getattr(first, "store_id", "-")
    logger.info(
        "ingest_complete",
        extra={
            "trace_id": getattr(request.state, "trace_id", "-"),
            "store_id": store_id_log,
            "endpoint": "/events/ingest",
            "event_count": result.accepted,
            "status_code": 200,
            "latency_ms": 0,
        }
    )
    return result


@app.get("/stores/{store_id}/metrics", response_model=StoreMetrics)
def metrics(store_id: str, db: Session = Depends(get_db)):
    """
    Real-time store metrics for today.
    Excludes staff. Handles zero-traffic correctly.
    """
    return get_store_metrics(store_id, db)


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
def funnel(store_id: str, db: Session = Depends(get_db)):
    """
    Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    Session-level — re-entries do not double-count a visitor.
    """
    return get_store_funnel(store_id, db)


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
def heatmap(store_id: str, db: Session = Depends(get_db)):
    """
    Zone visit frequency + avg dwell, normalised 0–100.
    data_confidence=False if fewer than 20 sessions in window.
    """
    return get_store_heatmap(store_id, db)


@app.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
def anomalies(store_id: str, db: Session = Depends(get_db)):
    """
    Active anomalies: queue spike, conversion drop, dead zones.
    Severity: INFO / WARN / CRITICAL with suggested_action.
    """
    return get_anomalies(store_id, db)


@app.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)):
    """
    Service health check. STALE_FEED if any store has >10 min event lag.
    """
    return get_health(db)
