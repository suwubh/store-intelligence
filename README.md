# Store Intelligence API — Brigade Bangalore (ST1008)

Real-time retail analytics from CCTV footage. Converts raw video → structured events → queryable metrics API.

## Setup (5 commands)

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

### Windows (PowerShell)
```powershell
# Make sure your clips are at: dataset\clips\ST1008\CAM 1.mp4 ... CAM 5.mp4

# Run all cameras (processes CAM 1-3, CAM 5 — skips CAM 4 storeroom automatically)
python pipeline/run.bat

# OR run each camera manually
python -m pipeline.detect --video "dataset/clips/ST1008/CAM 1.mp4" --store ST1008 --camera CAM_FLOOR_01 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_FLOOR_01_events.jsonl --clip-start 2026-04-10T10:00:00Z

python -m pipeline.detect --video "dataset/clips/ST1008/CAM 2.mp4" --store ST1008 --camera CAM_FLOOR_02 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_FLOOR_02_events.jsonl --clip-start 2026-04-10T10:00:00Z

python -m pipeline.detect --video "dataset/clips/ST1008/CAM 3.mp4" --store ST1008 --camera CAM_ENTRY_01 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_ENTRY_01_events.jsonl --clip-start 2026-04-10T10:00:00Z

python -m pipeline.detect --video "dataset/clips/ST1008/CAM 5.mp4" --store ST1008 --camera CAM_BILLING_01 --layout dataset/store_layout.json --output dataset/events/ST1008_CAM_BILLING_01_events.jsonl --clip-start 2026-04-10T10:00:00Z
# CAM 4 is the storeroom — pipeline skips it automatically
```

### Ingest events into API
```powershell
# After pipeline runs, ingest all event files
python pipeline/run.bat --api-url http://localhost:8000

# Or ingest one file manually
python -c "
import json, urllib.request
events = [json.loads(l) for l in open('dataset/events/ST1008_CAM_ENTRY_01_events.jsonl') if l.strip()]
for i in range(0, len(events), 500):
    batch = events[i:i+500]
    req = urllib.request.Request('http://localhost:8000/events/ingest',
        json.dumps({'events': batch}).encode(), {'Content-Type': 'application/json'}, method='POST')
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
# While API is running and events are flowing:
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
│   └── pos_transactions.csv
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```
