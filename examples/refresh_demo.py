"""Refresh timestamps ONLY in the fabricated demo, never in production feeds."""
import datetime
import json
from pathlib import Path

root = Path(__file__).resolve().parent
for name in ["candidates.json", "market.json"]:
    path = root / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    for offer in payload["offers"]:
        if not offer["product_key"].startswith("demo:"):
            raise ValueError("Refusing to refresh timestamps on non-demo evidence")
        offer["observed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
