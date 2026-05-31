import time
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_

from app.database import EventRecord
from app.models import HealthResponse, StoreHealthStatus

START_TIME = time.time()
STALE_FEED_MINUTES = 10


def get_health(db: Session) -> HealthResponse:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_cutoff = now - timedelta(minutes=STALE_FEED_MINUTES)
    hour_ago = now - timedelta(hours=1)

    # Get all store IDs that have ever sent events
    store_ids = db.execute(
        select(EventRecord.store_id).distinct()
    ).scalars().all()

    store_statuses = []
    for store_id in store_ids:
        last_ts = db.execute(
            select(func.max(EventRecord.timestamp)).where(
                EventRecord.store_id == store_id
            )
        ).scalar()

        count_last_hour = db.execute(
            select(func.count()).where(
                and_(
                    EventRecord.store_id == store_id,
                    EventRecord.timestamp >= hour_ago,
                )
            )
        ).scalar() or 0

        stale = (last_ts is None) or (last_ts < stale_cutoff)

        store_statuses.append(StoreHealthStatus(
            store_id=store_id,
            last_event_timestamp=last_ts,
            stale_feed=stale,
            event_count_last_hour=count_last_hour,
        ))

    overall = "degraded" if any(s.stale_feed for s in store_statuses) else "ok"

    return HealthResponse(
        status=overall,
        service_uptime_seconds=round(time.time() - START_TIME, 2),
        stores=store_statuses,
    )
