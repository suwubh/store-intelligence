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


## Decision 4: API Architecture — Synchronous FastAPI Ingest

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| **Synchronous FastAPI ingest** | Simple, deterministic, easy to debug, no worker coordination | Not ideal for very high throughput |
| Async background workers | Better for large queues and long-running jobs | More moving parts, harder to reason about, more failure modes |
| Streaming ingest pipeline | Near real-time processing | Overkill for a batch-based hackathon submission |

### What I Chose and Why

I used a synchronous FastAPI API with direct ingest into the service layer. That keeps the pipeline simple: each request is processed end-to-end, then persisted immediately. For this project, batch sizes are small, ingestion is bounded, and the priority is reliability over maximum throughput.

I did not add background workers because they would introduce extra orchestration, state handling, and retry logic without improving the submitted use case. A single-process architecture is also easier to test locally and matches the `docker compose up` requirement.

**What would make me change this decision:** If ingest volume increased significantly or clips had to be processed continuously across multiple stores, I would move event processing into a background queue and keep FastAPI as the front door only.

---

## Summary Table

| Decision | Chosen | Rejected | Primary Reason |
|----------|--------|----------|----------------|
| Detection model | YOLOv8s | YOLOv8m, MediaPipe, VLM | CPU speed + accuracy balance |
| Zone classification | Polygon-based | VLM frame crops | Deterministic, zero latency, no cost |
| Schema design | Events + materialised sessions | Flat events only | Query performance at metric endpoints |
| Storage | SQLite | PostgreSQL | Zero-config docker compose up |
| API architecture | Synchronous FastAPI ingest | Async workers, streaming pipeline | Simplicity + deterministic request handling |
| Staff detection | Store-specific HSV profiles | Re-ID embeddings | Uniform colours are known per store |

---

## Updated Resource Addendum

After the challenge resources were refreshed, the implementation was changed from a single hardcoded store contract to a compatibility contract.

### Event schema choice

The detector still emits the original uppercase challenge schema because it is explicit and stable for raw video output: `ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON`, and `REENTRY`. The API now also accepts the updated sample-event schema with lower-case names such as `entry`, `zone_entered`, and `queue_completed`.

AI initially suggested replacing the database schema with event-family-specific tables. I rejected that because it would make the API harder to test and would spread the resource change across every endpoint. I chose a boundary adapter in `app/models.py`: normalize incoming payloads into the existing canonical event record, then keep metrics and funnel logic stable.

### POS parser choice

The updated POS file is line-item based with `order_id`, split `order_date` / `order_time`, and `total_amount`. The loader now supports both this shape and the older `transaction_id,timestamp,basket_value_inr` example. Rows are aggregated by `order_id` before storage so conversion works at purchase level rather than SKU-line level.

### Dataset discovery choice

The store ZIPs use natural filenames rather than fixed `CAM 1.mp4` through `CAM 5.mp4`. The runners now discover clip folders and infer camera roles from filenames when a layout mapping is unavailable. This is intentionally conservative: it lets the pipeline run, but precise zone analytics still require calibrated polygons in `store_layout.json`.

### Store identity and cross-camera choice

After reviewing the real dataset, I mapped Store 1 to `ST1008` and Store 2 to `ST1076` in code instead of renaming folders. This keeps the raw dataset intact while making event, POS, and sample-event IDs line up. I also changed the runner to process entry clips before floor and billing clips, then added ingestion-time camera-local visitor linking. AI suggested jumping directly to a learned appearance embedding model; I did not add that because it would introduce an unverified dependency late in the submission. The chosen approach is transparent, testable, and improves funnel correctness for this batch pipeline, while documenting that it is still a heuristic.

### Staff profile update

The original detector assumed black uniforms everywhere. The updated resource review showed different store profiles, so the detector now uses all-black for Store 1 and pink-top/black-bottom for Store 2. This is deliberately simple HSV logic because the challenge footage is anonymised and uniform colour is the strongest available staff cue.
