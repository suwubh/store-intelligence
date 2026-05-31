"""
Event emitter — writes structured events to .jsonl and optionally POSTs to API.
"""
import json
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


class EventEmitter:
    def __init__(
        self,
        store_id: str,
        camera_id: str,
        output_path: str,
        api_url: Optional[str] = None,
        batch_size: int = 50,
    ):
        self.store_id = store_id
        self.camera_id = camera_id
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_url = api_url
        self.batch_size = batch_size

        self._buffer: list[dict] = []
        self._session_seq: dict[str, int] = defaultdict(int)
        # FIX (Issue N5): Open in write mode ("w") not append mode ("a").
        # Append mode caused stale events from prior pipeline runs to be re-ingested,
        # corrupting metrics with duplicated data across executions.
        self._file = open(self.output_path, "w")

    def emit(
        self,
        visitor_id: str,
        event_type: str,
        timestamp: datetime,
        zone_id: Optional[str],
        dwell_ms: int,
        is_staff: bool,
        confidence: float,
        queue_depth: Optional[int] = None,
        sku_zone: Optional[str] = None,
    ):
        self._session_seq[visitor_id] += 1

        event = {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp": _fmt_ts(timestamp),
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": round(confidence, 4),
            "metadata": {
                "queue_depth": queue_depth,
                "sku_zone": sku_zone or zone_id,
                "session_seq": self._session_seq[visitor_id],
            },
        }

        self._file.write(json.dumps(event) + "\n")
        self._buffer.append(event)

        if len(self._buffer) >= self.batch_size:
            self._flush_to_api()

    def flush(self):
        self._file.flush()
        self._file.close()
        self._flush_to_api()
        logger.info(f"Emitter flushed. Total events: {sum(self._session_seq.values())}")

    def _flush_to_api(self):
        if not self.api_url or not self._buffer:
            self._buffer.clear()
            return

        import urllib.request
        payload = json.dumps({"events": self._buffer}).encode()
        req = urllib.request.Request(
            f"{self.api_url}/events/ingest",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                if status != 200:
                    logger.warning(f"API ingest returned {status}")
        except Exception as e:
            logger.warning(f"API ingest failed: {e} — events saved to file")
        finally:
            self._buffer.clear()


def _fmt_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")