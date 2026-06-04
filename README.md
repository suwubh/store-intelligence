# Store Intelligence API

Store Intelligence API provides end-to-end retail analytics from anonymised CCTV footage. The system processes raw clips to extract structured behavioural events, ingests these events into a real-time FastAPI service, and exposes core store metrics, conversion funnels, heatmaps, anomaly alerts, and system health status.

Each store folder includes:

- CCTV clips (entry, floor/zone, billing)
- `store_layout.json` — A camera `source_file` map and zone definitions.
- `*layout*.png` — Floor plan reference image.

Store IDs mapped by the API:

| Clip Folder | API `store_id` |
|-------------|----------------|
| Store 1     | `ST1008`       |
| Store 2     | `ST1076`       |

## Quick Start

```powershell
# 1. Install dependencies
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Validate datasets
python pipeline/validate_dataset.py --dataset dataset

# 3. Start the API
docker compose up --build

# 4. Ingest pre-generated pipeline events (ST1008 — Store 1 clips)
#    Run from the project root after "docker compose up":
python -c "
import json, urllib.request, pathlib
for p in sorted(pathlib.Path('dataset/events').glob('ST1008_*.jsonl')):
    events = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    if not events: continue
    body = json.dumps({'events': events}).encode()
    req = urllib.request.Request('http://localhost:8000/events/ingest', body, {'Content-Type':'application/json'}, method='POST')
    print(p.name, urllib.request.urlopen(req).read().decode())
"

# 5. Ingest sample events (ST1076 demo schema)
python -c "import json, urllib.request; events=[json.loads(l) for l in open('dataset/events/sample_events.jsonl') if l.strip()]; req=urllib.request.Request('http://localhost:8000/events/ingest', json.dumps({'events': events}).encode(), {'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req).read().decode())"

# 6. Query metrics
curl http://localhost:8000/stores/ST1008/metrics
curl http://localhost:8000/stores/ST1076/funnel

# 7. View Live Dashboard (Part E)
# Open this URL in your browser:
# http://localhost:8000/
```

> **Note on conversion rate with pre-generated events:** The events in `dataset/events/` were generated with a hardcoded UTC anchor (`2026-04-10T20:00:00Z`) that does not overlap with the provided POS transaction window. As a result, `conversion_rate` will show as 0.0 when ingesting these pre-generated files. To see non-zero conversions, run the detection pipeline against the actual CCTV clips with `python run_pipeline.py --store-folder "Store 1" --api-url http://localhost:8000`. The OCR-based timestamp extraction will align events to the correct wall-clock time.

> **Note on health status:** The `/health` endpoint reports `"degraded"` when the latest event timestamp exceeds 10 minutes in age. Since the pre-generated events in `dataset/events/` date back to April 2026, the feed will register as stale after a fresh ingest. This design strictly accommodates real-time feeds. Running the pipeline against live clips (`run_pipeline.py`) restores a live status. The `last_event_timestamp` field indicates when data was recorded, while `event_count_last_hour` reflects recent API activity.

## Raw Clip Processing

The detection pipeline processes video clips and emits JSONL events. It links camera-local tracks to active entry sessions by processing entry cameras first, followed by floor and billing cameras.

```powershell
# Process a single store
python run_pipeline.py --store-folder "Store 1"

# Process all stores
python run_pipeline.py --all-stores

# Process and ingest directly into the running API
python run_pipeline.py --store-folder "Store 1" --api-url http://localhost:8000
```

Windows wrapper:

```powershell
pipeline\run.bat --store-folder "Store 1" --api-url http://localhost:8000
```

Events are saved to `dataset/events/<store_id>_<camera>_events.jsonl`.

To regenerate the layout JSON mapping (resolutions and filename-to-camera roles):

```powershell
python -c "from pathlib import Path; from pipeline.layout_builder import write_store_layout; write_store_layout(Path('dataset/clips/Store 1'))"
```

## API Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /events/ingest` | Validates, deduplicates, and stores up to 500 events per batch |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, dwell, queue depth, abandonment |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase |
| `GET /stores/{id}/heatmap` | Zone visit frequency, average dwell, 0-100 score |
| `GET /stores/{id}/anomalies` | Queue spike, conversion drop, dead zone checks |
| `GET /health` | Service status, per-store last event time, stale-feed warning |

## Event Compatibility

The API supports both the canonical uppercase schema emitted by the detection pipeline (e.g., `ENTRY`, `ZONE_ENTER`) and the lowercase sample-event schema (e.g., `entry`, `zone_entered`, `queue_completed`). Missing `event_id` attributes are resolved deterministically using UUIDv5. When ingesting the bundled sample file, `track_id` values 101–103 link securely to corresponding `id_token` values.

## POS Correlation

The dataset provided at `dataset/pos_transactions.csv` uses line-item rows aggregated by `order_id`. A visitor with a billing visit at time **B** qualifies as a converted visitor if a corresponding POS transaction exists at time **T**, where **B ≤ T ≤ B + 5 minutes**.

Store IDs are normalized during ingest and query. `Store 1` and `ST_STORE_1` map to `ST1008`; `Store 2`, `ST_STORE_2`, and `store_1076` map to `ST1076`. The system uses `POS_TIMEZONE_OFFSET_MINUTES` to reconcile any time base discrepancies between POS data and event timestamps.

## Calibration and Detection Profiles

The bundled `store_layout.json` files contain functional polygons for every camera, but floor zones are approximated in frame-space. For precise zone scoring, these polygons can be calibrated against the layout PNG and visible camera frames. Staff detection profiles are store-specific: Store 1 uses a black-uniform profile, while Store 2 relies on a pink-top and black-bottom profile.

## Tests

Execute the test suite to verify pipeline functionality and API integrity:

```powershell
pip install -r requirements.txt
pytest tests/ -v --cov=app --cov=pipeline --cov-report=term-missing
```

## Project Structure

```text
app/                  FastAPI, ingestion, metrics, funnel, heatmap, anomalies
pipeline/             Detection, layout_builder, validate_dataset, runners
dataset/clips/        Store 1, Store 2 (clips + per-store store_layout.json)
dataset/events/       Emitted JSONL + sample_events.jsonl
DESIGN.md             Architecture and design decisions
CHOICES.md            Engineering decisions
docs/DESIGN.md        Copy of architecture notes
docs/CHOICES.md       Copy of engineering decisions
```
