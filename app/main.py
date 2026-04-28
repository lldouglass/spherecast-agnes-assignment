from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline import run_pipeline


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Spherecast Agnes assignment demo pipeline.")
    parser.add_argument("--db", default="data/sample_db.xlsx")
    parser.add_argument("--extraction", default="data/extracted_fixture.json")
    parser.add_argument("--outdir", default="outputs")
    parser.add_argument("--trace-out", default=None, help="Optional explicit path for the pipeline trace artifact.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    db_path = (root / args.db).resolve()
    extraction_path = (root / args.extraction).resolve()
    outdir = (root / args.outdir).resolve()
    trace_out = (root / args.trace_out).resolve() if args.trace_out else outdir / "pipeline_trace.json"

    bundle = run_pipeline(db_path, extraction_path)
    result = bundle["result"]
    trace = bundle["trace"]

    summary = {
        "matched_purchase_order": result["matched_purchase_order"],
        "auto_apply_count": len(result["proposed_updates"]),
        "review_count": len(result["review_queue"]),
        "schema_gap_count": len(result["schema_gaps"]),
    }

    write_json(outdir / "summary.json", summary)
    write_json(outdir / "resolved_lines.json", result["resolved_lines"])
    write_json(outdir / "proposed_updates.json", result["proposed_updates"])
    write_json(outdir / "review_queue.json", result["review_queue"])
    write_json(outdir / "schema_gaps.json", result["schema_gaps"])
    write_json(trace_out, trace)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
