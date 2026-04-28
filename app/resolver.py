from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any
import re


def normalize_sku(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Z0-9]", "", value.upper())
    return cleaned or None


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def title_similarity(a: str | None, b: str | None) -> float:
    left = normalize_title(a)
    right = normalize_title(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def build_indexes(tables: dict[str, list[dict]]) -> dict[str, Any]:
    products = {row["id"]: row for row in tables["product"]}
    suppliers = {row["id"]: row for row in tables["supplier"]}
    purchase_orders = {row["id"]: row for row in tables["purchase_order"]}
    po_lines = tables["purchase_order_line"]

    po_by_reference = {row["reference_num"]: row for row in tables["purchase_order"]}
    po_by_reference_suffix = {}
    for row in tables["purchase_order"]:
        digits = re.sub(r"\D", "", str(row["reference_num"]))
        if digits:
            po_by_reference_suffix[digits] = row

    supplier_products_by_supplier = defaultdict(list)
    supplier_product_map = {}
    for row in tables["supplier_product"]:
        supplier_products_by_supplier[row["supplier_id"]].append(row)
        supplier_product_map[(row["supplier_id"], row["product_id"])] = row

    po_line_by_key = {}
    for row in po_lines:
        po_line_by_key[(row["purchase_order_id"], row["product_id"])] = row

    return {
        "products": products,
        "suppliers": suppliers,
        "purchase_orders": purchase_orders,
        "po_by_reference": po_by_reference,
        "po_by_reference_suffix": po_by_reference_suffix,
        "supplier_products_by_supplier": supplier_products_by_supplier,
        "supplier_product_map": supplier_product_map,
        "po_line_by_key": po_line_by_key,
    }


def resolve_purchase_order(extraction: dict, indexes: dict) -> dict:
    po_ref = extraction["header_fields"]["purchasing_ref_number"]["value"]
    direct = indexes["po_by_reference"].get(po_ref)
    if direct:
        return direct

    normalized = re.sub(r"\D", "", po_ref)
    if normalized in indexes["po_by_reference_suffix"]:
        return indexes["po_by_reference_suffix"][normalized]

    prefixed = f"PO-{po_ref}"
    if prefixed in indexes["po_by_reference"]:
        return indexes["po_by_reference"][prefixed]

    raise ValueError(f"Could not resolve purchase order for reference {po_ref!r}")


def score_product_match(line: dict, product: dict, supplier_product: dict | None) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    raw_sku = normalize_sku(line.get("raw_sku"))
    canonical_sku = normalize_sku(product.get("sku"))
    supplier_sku = normalize_sku(supplier_product.get("supplier_sku") if supplier_product else None)

    if raw_sku and supplier_sku and raw_sku == supplier_sku:
        score += 1.0
        reasons.append("exact supplier SKU match")

    if raw_sku and canonical_sku and raw_sku == canonical_sku:
        score += 0.95
        reasons.append("exact canonical SKU match")

    title_score = title_similarity(line.get("raw_title"), product.get("title"))
    if title_score > 0:
        score += title_score * 0.45
        reasons.append(f"title similarity {title_score:.2f}")

    if supplier_product:
        score += 0.05
        reasons.append("product is approved for this supplier")

    return score, reasons


def _note_updates_by_sku(extraction: dict) -> dict[str, list[dict]]:
    note_map = defaultdict(list)
    for item in extraction.get("note_updates", []):
        norm = normalize_sku(item.get("sku_hint"))
        if norm:
            note_map[norm].append(item)
    return note_map


def resolve_document(extraction: dict, tables: dict[str, list[dict]]) -> dict:
    indexes = build_indexes(tables)
    purchase_order = resolve_purchase_order(extraction, indexes)
    supplier = indexes["suppliers"][purchase_order["supplier_id"]]
    note_updates = _note_updates_by_sku(extraction)

    resolved_lines = []
    proposed_updates = []
    review_queue = []

    for line in extraction["line_item_candidates"]:
        supplier_products = indexes["supplier_products_by_supplier"][purchase_order["supplier_id"]]
        candidates = []

        for supplier_product in supplier_products:
            product = indexes["products"][supplier_product["product_id"]]
            score, reasons = score_product_match(line, product, supplier_product)
            if score > 0:
                candidates.append(
                    {
                        "product": product,
                        "supplier_product": supplier_product,
                        "score": round(score, 4),
                        "reasons": reasons,
                    }
                )

        candidates.sort(key=lambda item: item["score"], reverse=True)
        best = candidates[0] if candidates else None
        runner_up = candidates[1] if len(candidates) > 1 else None

        if not best or best["score"] < 0.90 or (runner_up and best["score"] - runner_up["score"] < 0.10):
            review_item = {
                "decision": "review",
                "reason": "Could not safely match extracted line to a single supplier-approved product.",
                "raw_line": line,
                "candidate_matches": candidates[:3],
                "flags": ["unmatched_or_ambiguous_product"],
            }
            review_queue.append(review_item)
            resolved_lines.append(
                {
                    "raw_line": line,
                    "decision": "review",
                    "matched_product": None,
                    "flags": review_item["flags"],
                }
            )
            continue

        product = best["product"]
        supplier_product = best["supplier_product"]
        existing_line = indexes["po_line_by_key"].get((purchase_order["id"], product["id"]))
        flags = []
        changes = {}
        ignored_fields = []
        norm_product_sku = normalize_sku(product["sku"])
        has_note_override = bool(note_updates.get(norm_product_sku))

        if line.get("quantity") is not None and existing_line and line["quantity"] != existing_line["quantity"]:
            changes["quantity"] = {
                "old": existing_line["quantity"],
                "new": line["quantity"],
                "source": line["source"],
            }

        if line.get("delivery_date") and existing_line:
            if has_note_override:
                ignored_fields.append(
                    {
                        "field": "delivery_date",
                        "value": line["delivery_date"],
                        "reason": "Superseded by newer supplier email note.",
                    }
                )
            elif line["delivery_date"] != existing_line["delivery_date"]:
                if line["delivery_date"] < purchase_order["delivery_date"]:
                    flags.append("suspicious_earlier_delivery_date")
                    review_queue.append(
                        {
                            "decision": "review",
                            "reason": "Delivery date moved materially earlier than the current PO date and may be bad data.",
                            "target": {
                                "table": "purchase_order_line",
                                "id": existing_line["id"],
                            },
                            "matched_product": {
                                "product_id": product["id"],
                                "sku": product["sku"],
                                "title": product["title"],
                            },
                            "proposed_field_change": {
                                "field": "delivery_date",
                                "current": existing_line["delivery_date"],
                                "candidate": line["delivery_date"],
                                "source": line["source"],
                            },
                            "flags": ["suspicious_earlier_delivery_date"],
                        }
                    )
                else:
                    changes["delivery_date"] = {
                        "old": existing_line["delivery_date"],
                        "new": line["delivery_date"],
                        "source": line["source"],
                    }

        if line.get("total_price") is not None:
            ignored_fields.append(
                {
                    "field": "total_price",
                    "value": line["total_price"],
                    "reason": "The current schema stores supplier-level price_per_unit, not PO-line total price.",
                }
            )

        decision = "auto_apply" if changes else "no_action"
        if changes:
            proposed_updates.append(
                {
                    "decision": decision,
                    "target": {
                        "table": "purchase_order_line",
                        "id": existing_line["id"],
                    },
                    "matched_product": {
                        "product_id": product["id"],
                        "sku": product["sku"],
                        "title": product["title"],
                    },
                    "match_confidence": best["score"],
                    "match_reasons": best["reasons"],
                    "changes": changes,
                    "ignored_fields": ignored_fields,
                    "related_review_flags": flags,
                }
            )

        resolved_lines.append(
            {
                "raw_line": line,
                "decision": decision,
                "matched_product": {
                    "product_id": product["id"],
                    "sku": product["sku"],
                    "title": product["title"],
                    "supplier_sku": supplier_product.get("supplier_sku"),
                },
                "match_score": best["score"],
                "changes": changes,
                "ignored_fields": ignored_fields,
                "flags": flags,
            }
        )

    for note in extraction.get("note_updates", []):
        norm = normalize_sku(note.get("sku_hint"))
        if not norm:
            continue

        matched_product = next(
            (
                line["matched_product"]
                for line in resolved_lines
                if line.get("matched_product") and normalize_sku(line["matched_product"]["sku"]) == norm
            ),
            None,
        )
        if not matched_product:
            review_queue.append(
                {
                    "decision": "review",
                    "reason": "Email note references a SKU that was not matched anywhere in the extracted attachment.",
                    "note": note,
                    "flags": ["orphan_note_update"],
                }
            )
            continue

        existing_line = indexes["po_line_by_key"][(purchase_order["id"], matched_product["product_id"])]
        candidates = note.get("normalized_candidates", [])
        review_queue.append(
            {
                "decision": "review",
                "reason": "Supplier email provides a newer ETA, but the slash-formatted date is locale-ambiguous.",
                "target": {
                    "table": "purchase_order_line",
                    "id": existing_line["id"],
                },
                "matched_product": matched_product,
                "proposed_field_change": {
                    "field": note["field"],
                    "current": existing_line[note["field"]],
                    "raw_value": note["raw_value"],
                    "normalized_candidates": candidates,
                    "source": note["source"],
                },
                "flags": ["email_overrides_attachment", "ambiguous_slash_date"],
            }
        )

    return {
        "matched_purchase_order": {
            "purchase_order_id": purchase_order["id"],
            "reference_num": purchase_order["reference_num"],
            "supplier": {
                "supplier_id": supplier["id"],
                "name": supplier["name"],
                "email": supplier["email"],
            },
        },
        "schema_gaps": [
            {
                "field": "external_purchasing_ref_number",
                "value": extraction["header_fields"]["external_purchasing_ref_number"]["value"],
                "reason": "Not represented in the sample relational schema.",
            },
            {
                "field": "terms",
                "value": extraction["header_fields"]["terms"]["value"],
                "reason": "Not represented in the sample relational schema.",
            },
        ],
        "resolved_lines": resolved_lines,
        "proposed_updates": proposed_updates,
        "review_queue": review_queue,
    }
