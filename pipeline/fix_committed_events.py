import json
from pathlib import Path

def fix_events():
    events_dir = Path("dataset/events")
    if not events_dir.exists():
        print(f"Directory {events_dir} not found.")
        return

    jsonl_files = list(events_dir.glob("*.jsonl"))
    print(f"Found {len(jsonl_files)} event files to process.")

    for filepath in jsonl_files:
        print(f"Processing {filepath.name}...")
        updated_lines = []
        modified_count = 0
        total_count = 0

        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_count += 1
                try:
                    event = json.loads(line)
                    visitor_id = event.get("visitor_id")
                    if visitor_id and str(visitor_id).isdigit():
                        event["visitor_id"] = f"VIS_{visitor_id}"
                        modified_count += 1
                    updated_lines.append(json.dumps(event))
                except Exception as e:
                    print(f"  Error parsing line: {e}")
                    updated_lines.append(line)

        # Write back
        with open(filepath, "w", encoding="utf-8") as f:
            for line in updated_lines:
                f.write(line + "\n")
        
        print(f"  Done. Modified {modified_count}/{total_count} events.")

if __name__ == "__main__":
    fix_events()
