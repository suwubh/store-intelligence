from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Optional
from datetime import datetime, timezone
from enum import Enum
import uuid
import json

from app.store_ids import normalize_store_id


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 1


class StoreEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @model_validator(mode="before")
    @classmethod
    def normalize_supported_event_shapes(cls, raw: Any):
        if not isinstance(raw, dict):
            return raw

        data = dict(raw)
        event_type_raw = str(data.get("event_type", "")).strip()
        event_type = _normalise_event_type(event_type_raw)
        if event_type:
            data["event_type"] = event_type

        data.setdefault("event_id", _stable_event_id(raw))
        data["store_id"] = normalize_store_id(data.get("store_id") or data.get("store_code"))
        data.setdefault("visitor_id", _normalise_visitor_id(data))
        data.setdefault("timestamp", _normalise_timestamp(data))
        data.setdefault("zone_id", data.get("zone_id"))
        data.setdefault("dwell_ms", _normalise_dwell_ms(data))
        data.setdefault("is_staff", bool(data.get("is_staff", False)))
        data.setdefault("confidence", float(data.get("confidence", 0.80)))

        metadata = dict(data.get("metadata") or {})
        metadata.setdefault("queue_depth", data.get("queue_depth") or data.get("queue_position_at_join"))
        metadata.setdefault("sku_zone", data.get("sku_zone") or data.get("zone_name") or data.get("zone_type"))
        metadata.setdefault("session_seq", int(data.get("session_seq") or 1))
        data["metadata"] = metadata

        return data

    @field_validator("zone_id")
    @classmethod
    def zone_required_for_zone_events(cls, v, info):
        zone_events = {
            EventType.ZONE_ENTER, EventType.ZONE_EXIT,
            EventType.ZONE_DWELL, EventType.BILLING_QUEUE_JOIN,
            EventType.BILLING_QUEUE_ABANDON
        }
        if info.data.get("event_type") in zone_events and v is None:
            raise ValueError(f"zone_id required for {info.data.get('event_type')}")
        return v


def _normalise_event_type(event_type: str) -> Optional[str]:
    if not event_type:
        return None
    direct = event_type.upper()
    if direct in EventType.__members__:
        return direct
    mapping = {
        "entry": "ENTRY",
        "exit": "EXIT",
        "zone_entered": "ZONE_ENTER",
        "zone_enter": "ZONE_ENTER",
        "zone_exited": "ZONE_EXIT",
        "zone_exit": "ZONE_EXIT",
        "zone_dwell": "ZONE_DWELL",
        "queue_completed": "BILLING_QUEUE_JOIN",
        "queue_joined": "BILLING_QUEUE_JOIN",
        "billing_queue_join": "BILLING_QUEUE_JOIN",
        "queue_abandoned": "BILLING_QUEUE_ABANDON",
        "billing_queue_abandon": "BILLING_QUEUE_ABANDON",
        "reentry": "REENTRY",
    }
    return mapping.get(event_type.lower(), direct)


def _normalise_visitor_id(data: dict[str, Any]) -> str:
    visitor = data.get("visitor_id") or data.get("id_token")
    if visitor:
        return str(visitor)
    track_id = data.get("track_id")
    if track_id is not None:
        return f"VIS_T{track_id}"
    return f"VIS_{uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(data, sort_keys=True, default=str)).hex[:10]}"


def _normalise_timestamp(data: dict[str, Any]) -> Any:
    return (
        data.get("timestamp")
        or data.get("event_timestamp")
        or data.get("event_time")
        or data.get("queue_join_ts")
        or data.get("queue_exit_ts")
        or datetime.now(timezone.utc).isoformat()
    )


def _normalise_dwell_ms(data: dict[str, Any]) -> int:
    if data.get("dwell_ms") is not None:
        return int(data.get("dwell_ms") or 0)
    wait_seconds = data.get("wait_seconds")
    if wait_seconds is not None:
        return int(float(wait_seconds) * 1000)
    return 0


def _stable_event_id(raw: dict[str, Any]) -> str:
    explicit = raw.get("queue_event_id") or raw.get("event_id")
    if explicit:
        return str(explicit)
    payload = json.dumps(raw, sort_keys=True, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, payload))


class IngestRequest(BaseModel):
    events: list[dict] = Field(max_length=500)  # validated per-event inside ingest for partial success


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicate: int
    errors: list[dict] = []


# ── API Response Models ────────────────────────────────────────────────────────

class ZoneDwellMetric(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    window_start: datetime
    window_end: datetime
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: list[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate: float


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    total_sessions: int


class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_ms: float
    normalised_score: float  # 0–100
    data_confidence: bool  # False if < 20 sessions


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[HeatmapZone]


class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"


class Anomaly(BaseModel):
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    zone_id: Optional[str] = None
    description: str
    suggested_action: str
    detected_at: datetime


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]


class StoreHealthStatus(BaseModel):
    store_id: str
    last_event_timestamp: Optional[datetime]
    stale_feed: bool
    event_count_last_hour: int
    last_ingest_at: Optional[datetime] = None  # Wall-clock time of most recent ingest (not event time)


class HealthResponse(BaseModel):
    status: str
    service_uptime_seconds: float
    stores: list[StoreHealthStatus]
