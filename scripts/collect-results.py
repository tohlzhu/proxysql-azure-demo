#!/usr/bin/env python3
"""Collect original result documents without inventing a second result schema."""

import json
from pathlib import Path


def main() -> None:
    root = Path("artifacts")
    runs = {}
    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        if path.name in ("collection.json", "health.json", "azure-status.json"):
            continue
        document = json.loads(path.read_text())
        if isinstance(document, dict) and "runs" in document:
            if document.get("schema_version") != 1 or not isinstance(document["runs"], list):
                raise ValueError(f"Unsupported result collection in {path.name}")
            candidates = document["runs"]
        else:
            candidates = document if isinstance(document, list) else [document]
        for result in candidates:
            if not isinstance(result, dict) or "run_id" not in result:
                continue
            if result.get("schema_version") != 1:
                raise ValueError(f"Unsupported result schema in {path.name}")
            runs[result["run_id"]] = result
    if not runs:
        raise RuntimeError("No run results found; run make test-demo first")
    collection = {
        "schema_version": 1,
        "runs": list(runs.values()),
        "health": json.loads((root / "health.json").read_text()),
        "proxysql_stats": [
            json.loads(line)
            for line in (root / "proxysql-stats.jsonl").read_text().splitlines()
            if line.strip()
        ],
    }
    azure = root / "azure-status.json"
    if azure.exists():
        collection["azure"] = json.loads(azure.read_text())
    (root / "collection.json").write_text(json.dumps(collection, indent=2) + "\n")
    print(f"Collected {len(runs)} original run results in artifacts/collection.json")


if __name__ == "__main__":
    main()
