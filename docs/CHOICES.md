# CHOICES.md — Key Engineering Decisions

## Decision 1: Detection Model — YOLOv8s

### Options Considered

| Model | Pros | Cons |
|-------|------|------|
| YOLOv8n (nano) | Fastest on CPU, ~45fps | Lower accuracy, misses partial occlusions |
| **YOLOv8s (small)** | **Good accuracy/speed balance** | **Chosen** |
| YOLOv8m (medium) | Best accuracy | 2× slower, impractical on CPU for 4 clips |
| RT-DETR | Transformer-based, strong on crowded scenes | Much heavier, poor CPU performance |
| MediaPipe | Very fast | Person detection only, no confidence scores, poor at distance |

### What AI Suggested

When asked to evaluate detection models for retail CCTV on CPU, the AI recommended YOLOv8s as the starting point with the reasoning that retail environments have moderate crowd density (2-8 people) and occlusion levels that YOLOv8s handles well. It suggested YOLOv8m only if accuracy on occluded/partial detections was unacceptable after testing.

### What I Chose and Why

YOLOv8s. The clips are 1920×1080 at 15-30fps, and we process every 2nd frame (effective 7-15fps) on CPU. YOLOv8n was tested first and produced 15-20% more false positives (shelves being detected as people). YOLOv8s reduced these significantly with acceptable processing speed (~22 seconds per 140-second clip on CPU). The confidence threshold was set at 0.35 rather than the default 0.25 — this reduced shelf/display false positives without losing real detections.

**One thing I disagreed with the AI on:** It initially suggested using a VLM (GPT-4V style) for zone classification — feeding frame crops to a vision model to determine which zone a person is in. I rejected this because (a) it would add API latency to every frame, (b) it would cost money per frame, and (c) polygon-based zone assignment is deterministic, faster, and more auditable. The VLM approach would only make sense for ambiguous zone boundaries, which this store layout doesn't have.

---

## Decision 2: Event Schema Design

### The Core Problem

The schema needs to support multiple query patterns simultaneously: per-visitor session reconstruction, per-zone dwell analysis, conversion funnel computation, and anomaly detection. Over-normalising creates complex joins at query time; under-normalising inflates storage.

### Options Considered

**Option A — Flat events only**
Every metric computed by scanning all events. Simple to write, expensive to query at scale.

**Option B — Events + materialised sessions (chosen)**
Events are immutable. Sessions are built incrementally during ingest. Metrics query sessions for aggregates, events for zone-level detail.

**Option C — Pre-aggregated metrics only**
Store only computed metrics, not raw events. Fast queries, but no ability to recompute or debug. Rejected immediately — it would fail the "outputs do not vary with input" integrity check.

### What AI Suggested

The AI suggested Option B and specifically recommended the `session_seq` field in metadata — an ordinal counter per visitor per session. This allows reconstructing the exact sequence of zone visits without sorting by timestamp, which is useful when events from different cameras arrive out of order. I adopted this.

The AI also suggested storing `is_staff` on every event rather than only on the session. Initially this seemed redundant, but it's the right call: if staff classification improves later, individual events can be re-evaluated without re-ingesting everything.

### What I Chose and Why

Option B with the following specific decisions:

- **`event_id` as UUID v4** — global uniqueness, enables safe idempotent ingest across multiple pipeline runs
- **`visitor_id` as short hash** — readable in logs, stable within a session
- **`dwell_ms` on every event** — even ENTRY/EXIT events carry dwell_ms=0. This simplifies downstream consumers who don't need to check event_type before reading dwell_ms
- **Flat metadata block** — queue_depth, sku_zone, session_seq grouped together rather than at top level, keeping the main event fields clean and consistent across all event types

---

## Decision 3: Storage Engine — SQLite over PostgreSQL

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| **SQLite** | Zero config, single file, runs in container | Not suitable for concurrent writes from multiple processes |
| PostgreSQL | Production-grade, concurrent writes, full SQL | Requires separate container, connection pooling, more config |
| Redis | Fast in-memory, good for real-time counters | No persistence by default, no SQL queries |
| DuckDB | Excellent analytical queries | Less familiar ORM support, overkill for this scale |

### What AI Suggested

The AI gave a nuanced answer here: it recommended SQLite for the hackathon submission because the acceptance gate requires `docker compose up` with no manual steps, and SQLite satisfies that with zero configuration. However, it explicitly noted that SQLite would be the first thing to replace in a production deployment serving 40 stores.

It also warned about one specific SQLite limitation that turned out to matter: **SQLite has no native UPSERT that SQLAlchemy handles transparently**. When batches of events contain multiple updates for the same session (same visitor_id), naive INSERTs fail with UNIQUE constraint violations. The fix was to use an in-memory session cache per ingest batch combined with `db.flush()` after first insert — this ensures SQLAlchemy's identity map resolves the duplicate before SQLite sees it.

### What I Chose and Why

SQLite, for exactly the reason the AI stated. The `docker compose up` acceptance gate is binary — fail it and the submission is rejected before scoring. SQLite removes an entire class of potential failure (PostgreSQL container not starting, connection refused, auth errors).

The write pattern is also amenable to SQLite: events are ingested in batches (up to 500), not as individual concurrent writes. The pipeline processes one camera at a time, so there's never a case of two processes writing simultaneously.

**What would make me change this decision:** If the system needed to serve live events from all 40 stores simultaneously, SQLite would bottleneck on concurrent writes. The migration path is straightforward — swap the SQLAlchemy connection string to `postgresql://` and update `docker-compose.yml` to add a postgres service. The ORM layer means no SQL queries need to change.

---

## Summary Table

| Decision | Chosen | Rejected | Primary Reason |
|----------|--------|----------|----------------|
| Detection model | YOLOv8s | YOLOv8m, MediaPipe, VLM | CPU speed + accuracy balance |
| Zone classification | Polygon-based | VLM frame crops | Deterministic, zero latency, no cost |
| Schema design | Events + materialised sessions | Flat events only | Query performance at metric endpoints |
| Storage | SQLite | PostgreSQL | Zero-config docker compose up |
| Staff detection | HSV colour (black uniform) | Re-ID embeddings | Uniform colour is known and consistent |