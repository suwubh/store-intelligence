from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime
from enum import Enum
import uuid


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


class HealthResponse(BaseModel):
    status: str
    service_uptime_seconds: float
    stores: list[StoreHealthStatus]
