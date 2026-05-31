from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, distinct, case

from app.database import EventRecord, SessionRecord
from app.models import HeatmapResponse, HeatmapZone

MIN_SESSIONS_FOR_CONFIDENCE = 20


def _day_window(db, store_id):
    from sqlalchemy import func as sqlfunc
    earliest = db.execute(
        select(sqlfunc.min(EventRecord.timestamp)).where(EventRecord.store_id == store_id)
    ).scalar()
    if earliest:
        day_start = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start, day_start + timedelta(days=1)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


def get_store_heatmap(store_id: str, db: Session) -> HeatmapResponse:
    day_start, day_end = _day_window(db, store_id)

    # FIX (Issue N8): Previously counted distinct sessions via SessionRecord.entry_time,
    # but floor/billing cameras never emit ENTRY events, so entry_time is NULL for most
    # sessions. NULL >= day_start evaluates False in SQL, causing total_sessions to be
    # far below the true visitor count, making data_confidence always False.
    # Fix: count distinct visitor_ids from the events table (same approach as metrics.py).
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

    # FIX (Issue N6): avg_dwell was diluted by ZONE_ENTER events (which always have
    # dwell_ms=0). Including them pulled averages toward 0 — a zone with 10 ZONE_ENTER
    # and 1 ZONE_DWELL (30s) showed avg ≈ 2.7s instead of 30s.
    # Fix: use conditional aggregation — avg_dwell only averages ZONE_DWELL rows,
    # while visit_count still counts both ZONE_ENTER and ZONE_DWELL (correct for frequency).
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