from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, distinct, case

from app.database import EventRecord, SessionRecord, get_day_window
from app.models import HeatmapResponse, HeatmapZone

MIN_SESSIONS_FOR_CONFIDENCE = 20


def get_store_heatmap(store_id: str, db: Session) -> HeatmapResponse:
    day_start, day_end = get_day_window(db, store_id)

    total_sessions = db.execute(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
            )
        )
    ).scalar() or 0

    has_confidence = total_sessions >= MIN_SESSIONS_FOR_CONFIDENCE

    rows = db.execute(
        select(
            EventRecord.zone_id,
            func.count(EventRecord.event_id).label("visit_count"),
            func.avg(
                case(
                    (EventRecord.event_type == "ZONE_DWELL", EventRecord.dwell_ms),
                    else_=None,
                )
            ).label("avg_dwell"),
        ).where(
            and_(
                EventRecord.store_id == store_id,
                EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
                EventRecord.is_staff == False,
                EventRecord.timestamp >= day_start,
                EventRecord.timestamp < day_end,
                EventRecord.zone_id.isnot(None),
            )
        ).group_by(EventRecord.zone_id)
    ).all()

    if not rows:
        return HeatmapResponse(store_id=store_id, zones=[])

    max_visits = max(r.visit_count for r in rows) or 1

    zones = [
        HeatmapZone(
            zone_id=row.zone_id,
            visit_frequency=row.visit_count,
            avg_dwell_ms=round(row.avg_dwell or 0, 2),
            normalised_score=round((row.visit_count / max_visits) * 100, 2),
            data_confidence=has_confidence,
        )
        for row in rows
    ]
    zones.sort(key=lambda z: z.normalised_score, reverse=True)
    return HeatmapResponse(store_id=store_id, zones=zones)