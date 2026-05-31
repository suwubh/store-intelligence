# Store Intelligence API — Brigade Bangalore (ST1008)

Real-time retail analytics from CCTV footage. Converts raw video → structured events → queryable metrics API.

## Quick Start (pre-computed events — no GPU needed)

Pre-processed event files from the Brigade Bangalore clips are committed at `dataset/events/`. Use this path to get live metrics in under 60 seconds without re-running the detection pipeline.

```powershell
# 1. Start the API
docker compose up --build -d

# 2. Wait ~15 seconds for the container to be ready, then verify
curl http://localhost:8000/health

# 3. Ingest the pre-computed events (takes ~5 seconds)
python -c "
import json, glob, urllib.request
for ef in sorted(glob.glob('dataset/events/*.jsonl')):
    events = [json.loads(l) for l in open(ef) if l.strip()]
    if not events:
        continue
    acc = 0
    for i in range(0, len(events), 500):
        batch = events[i:i+500]
        req = urllib.request.Request(
            'http://localhost:8000/events/ingest',
            json.dumps({'events': batch}).encode(),
            {'Content-Type': 'application/json'}, method='POST')
        res = json.loads(urllib.request.urlopen(req).read())
        acc += res.get('accepted', 0)
    print(f'{ef.split(\"/\")[-1]}: {acc} events ingested')
print('Done — metrics ready.')
"

# 4. Check metrics
curl http://localhost:8000/stores/ST1008/metrics
curl http://localhost:8000/stores/ST1008/funnel
curl http://localhost:8000/stores/ST1008/anomalies
```

## Full Setup (re-run detection pipeline from raw clips)

Only needed if you want to re-process the CCTV footage yourself.

```bash
# 1. Clone and enter project
cd store-intelligence

# 2. Create virtual environment and install deps
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Mac/Linux

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Start the API
docker compose up --build -d

# 5. Verify API is running
curl http://localhost:8000/health
```

## Running the Detection Pipeline

Place your clips at `dataset/clips/ST1008/CAM 1.mp4` through `CAM 5.mp4`, then:

```powershell
# Processes CAM 1-3, CAM 5 — skips CAM 4 storeroom automatically
python run_pipeline.py

# With live ingest into the running API
python run_pipeline.py --api-url http://localhost:8000
```

### Manual per-camera commands (Windows PowerShell)

```powershell
python -m pipeline.detect --video "dataset/clips/ST1008/CAM 1.mp4" --store ST1008 --camera CAM_FLOOR_01 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_FLOOR_01_events.jsonl --clip-start 2026-04-10T10:00:00Z

python -m pipeline.detect --video "dataset/clips/ST1008/CAM 2.mp4" --store ST1008 --camera CAM_FLOOR_02 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_FLOOR_02_events.jsonl --clip-start 2026-04-10T10:00:00Z

python -m pipeline.detect --video "dataset/clips/ST1008/CAM 3.mp4" --store ST1008 --camera CAM_ENTRY_01 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_ENTRY_01_events.jsonl --clip-start 2026-04-10T10:00:00Z

python -m pipeline.detect --video "dataset/clips/ST1008/CAM 5.mp4" --store ST1008 --camera CAM_BILLING_01 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_BILLING_01_events.jsonl --clip-start 2026-04-10T10:00:00Z
# CAM 4 is the storeroom — skipped automatically
```

### Ingest after manual pipeline run

```powershell
python -c "
import json, glob, urllib.request
for ef in sorted(glob.glob('dataset/events/*.jsonl')):
    events = [json.loads(l) for l in open(ef) if l.strip()]
    for i in range(0, len(events), 500):
        req = urllib.request.Request('http://localhost:8000/events/ingest',
            json.dumps({'events': events[i:i+500]}).encode(),
            {'Content-Type': 'application/json'}, method='POST')
        print(json.loads(urllib.request.urlopen(req).read()))
"
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `POST /events/ingest` | Ingest detection events (up to 500/batch, idempotent) |
| `GET /stores/ST1008/metrics` | Unique visitors, conversion rate, queue depth, dwell per zone |
| `GET /stores/ST1008/funnel` | Entry → Zone Visit → Billing → Purchase with drop-off % |
| `GET /stores/ST1008/heatmap` | Zone visit frequency + avg dwell, normalised 0-100 |
| `GET /stores/ST1008/anomalies` | Active anomalies: queue spike, conversion drop, dead zones |
| `GET /health` | Service status, STALE_FEED detection |

## Live Dashboard (Terminal)

```powershell
# While API is running and events are ingested:
python dashboard/live.py --store ST1008 --api http://localhost:8000
```

## Running Tests

```powershell
pytest tests/ -v --cov=app --cov-report=term-missing
```

## Camera Mapping (Brigade Bangalore)

| Camera File | Camera ID | Coverage | Notes |
|---|---|---|---|
| CAM 1.mp4 | CAM_FLOOR_01 | Skincare section | FarmStay, TFS, GoodVibes, Minimalist, Aqualogica |
| CAM 2.mp4 | CAM_FLOOR_02 | Makeup section | Maybelline, Lakme, FacesCanada, Alps, L'Oreal |
| CAM 3.mp4 | CAM_ENTRY_01 | Entry/Exit threshold | Glass door, Purplle signage |
| CAM 4.mp4 | CAM_STOREROOM | Back office | **EXCLUDED** — staff-only stockroom |
| CAM 5.mp4 | CAM_BILLING_01 | Billing counter | POS laptop, accessories display |

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # YOLOv8 + tracking + event emission
│   ├── tracker.py         # ByteTrack / IoU tracker + Re-ID
│   ├── emit.py            # Event schema + JSONL/API output
│   ├── staff_detector.py  # HSV color-based staff classification
│   ├── zone_mapper.py     # Pixel → zone via store_layout.json polygons
│   ├── run.sh             # Linux/Mac: process all clips
│   └── run.bat            # Windows: process all clips
├── app/
│   ├── main.py            # FastAPI app + middleware + endpoints
│   ├── models.py          # Pydantic schemas
│   ├── database.py        # SQLAlchemy + SQLite
│   ├── ingestion.py       # Event ingest + session materialisation
│   ├── metrics.py         # /metrics endpoint logic
│   ├── funnel.py          # /funnel endpoint logic
│   ├── heatmap.py         # /heatmap endpoint logic
│   ├── anomalies.py       # /anomalies endpoint logic
│   └── health.py          # /health endpoint logic
├── dashboard/
│   └── live.py            # Rich terminal live dashboard
├── tests/
│   └── test_api.py        # pytest suite (>70% coverage)
├── docs/
│   ├── DESIGN.md          # Architecture + AI-assisted decisions
│   └── CHOICES.md         # 3 key engineering decisions
├── dataset/
│   ├── store_layout.json  # Zone polygons + camera mappings
│   ├── pos_transactions.csv
│   └── events/            # Pre-computed pipeline output (committed)
│       ├── ST1008_CAM_ENTRY_01_events.jsonl
│       ├── ST1008_CAM_FLOOR_01_events.jsonl
│       ├── ST1008_CAM_FLOOR_02_events.jsonl
│       └── ST1008_CAM_BILLING_01_events.jsonl
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```