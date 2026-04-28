from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from excel_loader import load_xlsx_tables
from resolver import resolve_document_with_trace


STAGE_ORDER = ["input", "extraction", "resolution", "validation", "decision"]


def load_reference_state(db_path: str | Path) -> dict[str, list[dict]]:
    return load_xlsx_tables(db_path)


def load_structured_extraction(extraction_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(extraction_path).read_text(encoding="utf-8"))


def build_input_stage(db_path: Path, tables: dict[str, list[dict]]) -> dict[str, Any]:
    return {
        "db_path": str(db_path),
        "table_counts": {name: len(rows) for name, rows in tables.items()},
        "notes": [
            "Reference state is loaded from the provided Excel workbook.",
            "The resolver never writes directly back into the workbook.",
        ],
    }


def build_extraction_stage(extraction_path: Path, extraction: dict[str, Any]) -> dict[str, Any]:
    source_email = extraction.get("source_email", {})
    header_fields = extraction.get("header_fields", {})
    return {
        "extraction_path": str(extraction_path),
        "document_type": extraction.get("document_type"),
        "source_email": {
            "from": source_email.get("from"),
            "subject": source_email.get("subject"),
            "body_preview": (source_email.get("body") or "")[:160],
        },
        "header_fields": {
            name: {
                "value": payload.get("value"),
                "source": payload.get("source"),
                "confidence": payload.get("confidence"),
            }
            for name, payload in header_fields.items()
        },
        "line_item_count": len(extraction.get("line_item_candidates", [])),
        "note_update_count": len(extraction.get("note_updates", [])),
        "notes": [
            "This stage consumes a structured extraction payload.",
            "In the live path, app/extract_live.py produces this JSON from the raw email body plus attachment image.",
        ],
    }


def build_decision_stage(result: dict[str, Any], validation_stage: dict[str, Any]) -> dict[str, Any]:
    auto_apply_targets = []
    no_action_lines = []
    for line in result["resolved_lines"]:
        if line["decision"] == "no_action":
            no_action_lines.append(
                {
                    "line_id": line["raw_line"].get("line_id"),
                    "raw_sku": line["raw_line"].get("raw_sku"),
                    "matched_product_sku": (line.get("matched_product") or {}).get("sku"),
                }
            )

    for item in result["proposed_updates"]:
        auto_apply_targets.append(
            {
                "target": item["target"],
                "matched_product_sku": item["matched_product"]["sku"],
                "change_fields": sorted(item["changes"].keys()),
                "match_confidence": item["match_confidence"],
            }
        )

    return {
        "summary": {
            "auto_apply_count": len(result["proposed_updates"]),
            "review_count": len(result["review_queue"]),
            "schema_gap_count": len(result["schema_gaps"]),
            "no_action_count": len(no_action_lines),
        },
        "auto_apply_targets": auto_apply_targets,
        "review_queue": result["review_queue"],
        "schema_gaps": result["schema_gaps"],
        "no_action_lines": no_action_lines,
        "notes": [
            "Validation findings are converted into either staged auto-apply updates, review items, or no-action outcomes.",
            f"Observed validation traces: {len(validation_stage.get('line_validations', []))} line validations and {len(validation_stage.get('note_update_validations', []))} note validations.",
        ],
    }


def run_pipeline(db_path: str | Path, extraction_path: str | Path) -> dict[str, Any]:
    db_path = Path(db_path)
    extraction_path = Path(extraction_path)

    tables = load_reference_state(db_path)
    extraction = load_structured_extraction(extraction_path)
    resolution_bundle = resolve_document_with_trace(extraction, tables)
    result = resolution_bundle["result"]
    resolution_trace = resolution_bundle["trace"]

    trace = {
        "artifact_version": 1,
        "stage_order": STAGE_ORDER,
        "stages": {
            "input": build_input_stage(db_path, tables),
            "extraction": build_extraction_stage(extraction_path, extraction),
            "resolution": resolution_trace["resolution"],
            "validation": resolution_trace["validation"],
            "decision": build_decision_stage(result, resolution_trace["validation"]),
        },
    }

    return {
        "tables": tables,
        "extraction": extraction,
        "result": result,
        "trace": trace,
    }
