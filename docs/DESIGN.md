# DESIGN.md — Store Intelligence API

## System Overview

This system converts raw CCTV footage from a Purplle retail store (Brigade Road, Bangalore) into a live analytics API. The pipeline runs in four stages: video ingestion → person detection & tracking → event emission → API serving.

The north star metric is **offline store conversion rate**: unique visitors who completed a purchase divided by total unique visitors in the session window.

---

## Architecture

```
CCTV Clips (5 cameras)
        │
        ▼
┌─────────────────────┐
│   Detection Layer   │  YOLOv8s person detection (ultralytics)
│   pipeline/detect.py│  ByteTrack multi-object tracking
│   pipeline/tracker.py  IoU + appearance Re-ID fallback
│   pipeline/emit.py  │  Structured JSONL event emission
└────────┬────────────┘
         │  events.jsonl (per camera)
         ▼
┌─────────────────────┐
│   FastAPI App       │  POST /events/ingest (idempotent, batched)
│   app/ingestion.py  │  SQLite via SQLAlchemy ORM
│   app/database.py   │  EventRecord + SessionRecord + POSTransaction
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│  Intelligence Endpoints                                  │
│  GET /stores/{id}/metrics   — unique visitors, CVR, dwell│
│  GET /stores/{id}/funnel    — Entry→Zone→Billing→Purchase│
│  GET /stores/{id}/heatmap   — zone frequency, normalised │
│  GET /stores/{id}/anomalies — queue spike, dead zones    │
│  GET /health                — stale feed detection        │
└─────────────────────────────────────────────────────────┘
```

---

## Detection Layer Design

### Camera Setup (Brigade Bangalore — ST1008)

Five cameras cover the store. A critical observation made during initial analysis: **CAM 4 is a storeroom/back-office, not a customer-facing camera**. It is excluded from all metric computation via `exclude_from_metrics: true` in `store_layout.json`.

| Camera | Role | Key Design Decision |
|--------|------|---------------------|
| CAM 3 | Entry/Exit | Entry line at 55% frame height (glass door threshold) |
| CAM 1 | Skincare floor | 4 zone polygons at 1920×1080 |
| CAM 2 | Makeup floor | 5 zone polygons at 1920×1080 |
| CAM 5 | Billing counter | Billing + queue zones — full-width polygons covering 1920px |
| CAM 4 | Storeroom | Excluded entirely |

### Person Detection

YOLOv8s was chosen over YOLOv8n (faster but less accurate) and YOLOv8m (more accurate but 2× slower on CPU). For retail CCTV at 1080p with 2-8 people per frame, YOLOv8s provides the right accuracy/speed balance. Confidence threshold set at 0.35 — lower than default (0.25 is too noisy, 0.5 misses partially occluded people).

### Tracking

ByteTrack is used for multi-object tracking. It maintains track identity across frames using IoU-based matching with a Kalman filter for motion prediction. When ByteTrack is unavailable (version mismatch), the system falls back to a pure IoU tracker. Both paths produce the same event schema.

### Staff Exclusion

Staff wear all-black uniforms (observed across CAM 1, 2, 5). Staff detection uses HSV colour analysis on the torso region of each bounding box. Two HSV ranges cover: pure black (V < 45, strict) and dark low-saturation clothing. The pixel match threshold is 0.40 (40% of torso pixels must match) — high enough to require majority-black clothing while not triggering on dark accessories or shadows. All events carry `is_staff: bool` and staff events are excluded from customer metrics at query time, not at ingestion time — this preserves raw data for potential re-analysis.

### Entry/Exit Detection

The entry camera (CAM 3) uses a horizontal line at 55% of frame height as the crossing threshold. Direction is determined by Y-coordinate movement across this line:
- Last Y < line AND current Y >= line → INWARD → ENTRY event
- Last Y > line AND current Y <= line → OUTWARD → EXIT event

The `entry_line_ratio` is stored in `store_layout.json` per camera, making it configurable without code changes.

### Re-entry Handling

A **10-minute (600-second) Re-ID window** is applied (`REID_MAX_ABSENCE_SECONDS = 600` in `tracker.py`). If the same `visitor_id` (matched by HSV appearance histogram cosine similarity > 0.75) is detected after a prior exit within 600 seconds, a REENTRY event is emitted instead of a new ENTRY. This prevents re-entry inflation — a known problem with naive entry counting systems.

Re-entry detection works as follows:
1. When a track is lost for `max_lost_frames` (30) frames, it is moved to `exited_tracks`.
2. When a new detection arrives, it is compared against all `exited_tracks` by cosine similarity of their HSV histograms.
3. If a match is found within the 600-second window, the track is restored with `was_reentry=True`.
4. `detect.py` calls `tracker.is_reentry(visitor_id)` immediately after a crossing event — this reads and clears the `was_reentry` flag, returning True exactly once per re-entry.

---

## Event Schema Design

Events follow a flat JSON schema with a nested `metadata` block:

```json
{
  "event_id": "uuid-v4",
  "store_id": "ST1008",
  "camera_id": "CAM_ENTRY_01",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-04-10T10:22:10Z",
  "zone_id": "MINIMALIST_AQUALOGICA",
  "dwell_ms": 32000,
  "is_staff": false,
  "confidence": 0.87,
  "metadata": {
    "queue_depth": null,
    "sku_zone": "MINIMALIST_AQUALOGICA",
    "session_seq": 4
  }
}
```

Key decisions: `event_id` is UUID v4 for global uniqueness and idempotent ingest. `visitor_id` is a short hash prefix for readability in logs. `is_staff` is stored on every event (not just at session level) to allow per-event filtering without joining sessions. Timestamps are ISO-8601 UTC derived from clip start time + frame offset.

---

## API and Storage Design

**Storage: SQLite** — chosen for zero-configuration deployment. The system runs `docker compose up` with no external dependencies. For 5 stores × 8 hours × ~50 events/minute, SQLite handles the write throughput comfortably. PostgreSQL would be the upgrade path for 40+ stores.

**Session materialisation** — sessions are built incrementally during ingest. Each event updates the session record (entry time, zones visited, billing flag, conversion). This avoids expensive re-computation at query time.

**Idempotency** — `POST /events/ingest` deduplicates by `event_id`. Sending the same batch twice produces 0 new accepted events on the second call. This is verified by tests.

**Conversion detection** — a visitor is counted as converted if they were in the billing zone within a 5-minute window before a POS transaction timestamp. The window is checked bidirectionally (±2 min) to handle camera-to-POS timing variance.

**POS timezone handling** — POS CSV timestamps are in IST (Indian Standard Time, UTC+5:30). Pipeline event timestamps are derived from clip start time in UTC and stored as UTC-naive datetimes. During POS ingestion, all IST timestamps are converted to UTC by subtracting 5h30m before storage, ensuring both timestamp columns share the same reference frame for conversion window matching.

---

## AI-Assisted Decisions

**1. Zone polygon sizing bug — caught by AI review**

Initial zone polygons were sized for 640×480 (a common default). An AI-assisted review of the store layout against the actual video resolution (1920×1080) identified that all polygons needed to be scaled up by 3×. Without this fix, zero zone events would have been generated since every person's centroid would fall outside all polygon boundaries.

**2. Entry line ratio per camera**

The AI suggested storing `entry_line_y_ratio` in `store_layout.json` rather than hardcoding it in the tracker. This led to a better design where each camera's entry threshold is configurable. For CAM 3 (Brigade Bangalore), visual inspection placed the glass door threshold at 55% frame height — the AI suggested starting at 40% (a common default) but override to 55% was made after reviewing the actual camera frame.

**3. Session counting without entry_time**

The AI flagged that floor and billing cameras don't emit ENTRY events, so `entry_time` would be NULL for most sessions. The original metrics query filtered `WHERE entry_time >= day_start`, which would return 0 visitors from those cameras. The fix — counting distinct `visitor_id` from the events table rather than the sessions table — was an AI-suggested approach that was adopted after confirming it matched the intended business logic.

**4. POS timezone alignment**

An AI audit identified that POS timestamps (IST wall clock) and event timestamps (UTC-naive) were being compared directly, guaranteeing zero conversion matches despite real purchase activity during the clip window. Fix: subtract 5h30m from POS timestamps at load time to align both columns in UTC-naive space.

**5. Billing zone polygon coverage**

An AI review identified that the original CAM_BILLING_01 zone polygons only covered ~52% of the 1920×1080 frame (right strip x=900–1920 from y=0–200 and other regions were unmapped). This caused billing funnel counts to severely underreport since visitors in unmapped regions generated zone_id=None. Fixed by extending both zones to cover the full 1920px width.

---

## Known Limitations

- **Cross-camera deduplication** is not implemented. The same physical person appearing in CAM 1 and CAM 2 gets two visitor IDs. For conversion rate calculation this is conservative (overcounts visitors, undercounts conversion).
- **Staff detection** relies on colour alone. A customer wearing all-black would be misclassified. A more robust approach would use Re-ID embeddings trained on the specific staff members.
- **CONVERSION_DROP anomaly** requires historical data (7-day baseline). With single-day evaluation data, this anomaly type cannot fire. A future improvement would bootstrap the baseline from POS transaction history.