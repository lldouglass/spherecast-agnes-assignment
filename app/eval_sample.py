from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline import run_pipeline


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def find_resolved_line(result: dict[str, Any], raw_sku: str) -> dict[str, Any] | None:
    return next((line for line in result["resolved_lines"] if line["raw_line"].get("raw_sku") == raw_sku), None)


def find_proposed_update(result: dict[str, Any], sku: str) -> dict[str, Any] | None:
    return next((item for item in result["proposed_updates"] if item["matched_product"]["sku"] == sku), None)


def has_review_for_sku_field(result: dict[str, Any], sku: str, field: str) -> bool:
    for item in result["review_queue"]:
        matched_product = item.get("matched_product") or {}
        proposed_field_change = item.get("proposed_field_change") or {}
        if matched_product.get("sku") == sku and proposed_field_change.get("field") == field:
            return True
    return False


def has_review_for_raw_sku(result: dict[str, Any], raw_sku: str) -> bool:
    for item in result["review_queue"]:
        raw_line = item.get("raw_line") or {}
        if raw_line.get("raw_sku") == raw_sku:
            return True
    return False


def schema_gap_map(result: dict[str, Any]) -> dict[str, Any]:
    return {item["field"]: item["value"] for item in result["schema_gaps"]}


def build_report(result: dict[str, Any], extraction_path: str) -> dict[str, Any]:
    sku_13_line = find_resolved_line(result, "SKU-13")
    sku_2_update = find_proposed_update(result, "SKU-2")
    sku_3_update = find_proposed_update(result, "SKU-3")
    gaps = schema_gap_map(result)

    checks = [
        {
            "name": "matched_po_is_po_12",
            "passed": result["matched_purchase_order"]["reference_num"] == "PO-12",
            "details": {
                "actual_reference_num": result["matched_purchase_order"]["reference_num"],
            },
        },
        {
            "name": "sku_13_resolves_to_sku_1_3",
            "passed": bool(sku_13_line and sku_13_line["matched_product"] and sku_13_line["matched_product"]["sku"] == "SKU-1-3"),
            "details": {
                "actual_match": sku_13_line["matched_product"]["sku"] if sku_13_line and sku_13_line["matched_product"] else None,
            },
        },
        {
            "name": "sku_2_auto_applies_quantity_and_date",
            "passed": bool(
                sku_2_update
                and sku_2_update["decision"] == "auto_apply"
                and "quantity" in sku_2_update["changes"]
                and "delivery_date" in sku_2_update["changes"]
            ),
            "details": {
                "decision": sku_2_update["decision"] if sku_2_update else None,
                "change_fields": sorted((sku_2_update or {}).get("changes", {}).keys()),
            },
        },
        {
            "name": "sku_3_quantity_updates_while_date_is_review_only",
            "passed": bool(
                sku_3_update
                and sku_3_update["decision"] == "auto_apply"
                and "quantity" in sku_3_update["changes"]
                and "delivery_date" not in sku_3_update["changes"]
                and has_review_for_sku_field(result, "SKU-3", "delivery_date")
            ),
            "details": {
                "change_fields": sorted((sku_3_update or {}).get("changes", {}).keys()),
                "has_delivery_date_review": has_review_for_sku_field(result, "SKU-3", "delivery_date"),
            },
        },
        {
            "name": "sku_7_goes_to_review",
            "passed": has_review_for_raw_sku(result, "SKU-7"),
            "details": {
                "found_review_item": has_review_for_raw_sku(result, "SKU-7"),
            },
        },
        {
            "name": "schema_gaps_preserve_terms_and_external_ref",
            "passed": gaps.get("terms") == "DAP" and gaps.get("external_purchasing_ref_number") == "43123",
            "details": {
                "terms": gaps.get("terms"),
                "external_purchasing_ref_number": gaps.get("external_purchasing_ref_number"),
            },
        },
    ]

    passed_count = sum(1 for item in checks if item["passed"])
    return {
        "extraction_path": extraction_path,
        "passed": passed_count == len(checks),
        "passed_count": passed_count,
        "total_checks": len(checks),
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pass/fail checks for the Agnes assignment sample.")
    parser.add_argument("--db", default="data/sample_db.xlsx")
    parser.add_argument("--extraction", default="data/extracted_fixture.json")
    parser.add_argument("--out", default="outputs/eval_report.json")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    db_path = (root / args.db).resolve()
    extraction_path = (root / args.extraction).resolve()
    out_path = (root / args.out).resolve()

    bundle = run_pipeline(db_path, extraction_path)
    report = build_report(bundle["result"], str(extraction_path))
    write_json(out_path, report)
    print(json.dumps(report, indent=2))

    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
