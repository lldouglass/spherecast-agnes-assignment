from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any
import re


SAFE_MATCH_THRESHOLD = 0.90
SAFE_MATCH_MARGIN = 0.10


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
    for row in tables["supplier_product"]:
        supplier_products_by_supplier[row["supplier_id"]].append(row)

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


def build_matched_purchase_order_payload(purchase_order: dict, supplier: dict) -> dict[str, Any]:
    return {
        "purchase_order_id": purchase_order["id"],
        "reference_num": purchase_order["reference_num"],
        "supplier": {
            "supplier_id": supplier["id"],
            "name": supplier["name"],
            "email": supplier["email"],
        },
    }


def build_schema_gaps(extraction: dict) -> list[dict[str, Any]]:
    return [
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
    ]


def _note_updates_by_sku(extraction: dict) -> dict[str, list[dict]]:
    note_map = defaultdict(list)
    for item in extraction.get("note_updates", []):
        norm = normalize_sku(item.get("sku_hint"))
        if norm:
            note_map[norm].append(item)
    return note_map


def _matched_product_payload(product: dict, supplier_product: dict | None = None) -> dict[str, Any]:
    payload = {
        "product_id": product["id"],
        "sku": product["sku"],
        "title": product["title"],
    }
    if supplier_product is not None:
        payload["supplier_sku"] = supplier_product.get("supplier_sku")
    return payload


def _build_line_candidates(line: dict, purchase_order: dict, indexes: dict) -> list[dict[str, Any]]:
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
    return candidates


def _is_safe_best_match(best: dict[str, Any] | None, runner_up: dict[str, Any] | None) -> bool:
    if not best:
        return False
    if best["score"] < SAFE_MATCH_THRESHOLD:
        return False
    if runner_up and best["score"] - runner_up["score"] < SAFE_MATCH_MARGIN:
        return False
    return True


def _build_ambiguous_match_review(line: dict, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "decision": "review",
        "reason": "Could not safely match extracted line to a single supplier-approved product.",
        "raw_line": line,
        "candidate_matches": candidates[:3],
        "flags": ["unmatched_or_ambiguous_product"],
    }


def _build_missing_po_line_review(existing_target: dict[str, Any], matched_product: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": "review",
        "reason": "Matched product is valid for the supplier but does not map to an existing open PO line.",
        "target": existing_target,
        "matched_product": matched_product,
        "flags": ["matched_product_not_on_open_po_line"],
    }


def _validate_and_decide_line(
    line: dict,
    purchase_order: dict,
    existing_line: dict | None,
    product: dict,
    supplier_product: dict,
    best_match: dict[str, Any],
    note_updates: dict[str, list[dict]],
) -> dict[str, Any]:
    matched_product = _matched_product_payload(product, supplier_product)
    flags: list[str] = []
    changes: dict[str, Any] = {}
    ignored_fields: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    norm_product_sku = normalize_sku(product["sku"])
    has_note_override = bool(note_updates.get(norm_product_sku))

    if existing_line is None:
        review_items.append(
            _build_missing_po_line_review(
                {"table": "purchase_order_line", "id": None},
                {k: v for k, v in matched_product.items() if k != "supplier_sku"},
            )
        )
        decision = "review"
        resolved_line = {
            "raw_line": line,
            "decision": decision,
            "matched_product": matched_product,
            "match_score": best_match["score"],
            "changes": changes,
            "ignored_fields": ignored_fields,
            "flags": ["matched_product_not_on_open_po_line"],
        }
        return {
            "resolved_line": resolved_line,
            "proposed_update": None,
            "review_items": review_items,
            "validation_trace": {
                "line_id": line.get("line_id"),
                "raw_sku": line.get("raw_sku"),
                "matched_product_sku": product["sku"],
                "existing_po_line_id": None,
                "pending_change_fields": [],
                "ignored_fields": ignored_fields,
                "review_reasons": [item["reason"] for item in review_items],
                "flags": ["matched_product_not_on_open_po_line"],
                "decision": decision,
            },
        }

    if line.get("quantity") is not None and line["quantity"] != existing_line["quantity"]:
        changes["quantity"] = {
            "old": existing_line["quantity"],
            "new": line["quantity"],
            "source": line["source"],
        }

    if line.get("delivery_date"):
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
                review_items.append(
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
    proposed_update = None
    if changes:
        proposed_update = {
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
            "match_confidence": best_match["score"],
            "match_reasons": best_match["reasons"],
            "changes": changes,
            "ignored_fields": ignored_fields,
            "related_review_flags": flags,
        }

    resolved_line = {
        "raw_line": line,
        "decision": decision,
        "matched_product": matched_product,
        "match_score": best_match["score"],
        "changes": changes,
        "ignored_fields": ignored_fields,
        "flags": flags,
    }

    return {
        "resolved_line": resolved_line,
        "proposed_update": proposed_update,
        "review_items": review_items,
        "validation_trace": {
            "line_id": line.get("line_id"),
            "raw_sku": line.get("raw_sku"),
            "matched_product_sku": product["sku"],
            "existing_po_line_id": existing_line["id"],
            "pending_change_fields": sorted(changes.keys()),
            "ignored_fields": ignored_fields,
            "review_reasons": [item["reason"] for item in review_items],
            "flags": flags,
            "decision": decision,
        },
    }


def _resolve_line_items(extraction: dict, indexes: dict, purchase_order: dict, note_updates: dict[str, list[dict]]) -> dict[str, Any]:
    resolved_lines: list[dict[str, Any]] = []
    proposed_updates: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []
    resolution_trace: list[dict[str, Any]] = []
    validation_trace: list[dict[str, Any]] = []

    for line in extraction["line_item_candidates"]:
        candidates = _build_line_candidates(line, purchase_order, indexes)
        best = candidates[0] if candidates else None
        runner_up = candidates[1] if len(candidates) > 1 else None

        if not _is_safe_best_match(best, runner_up):
            review_item = _build_ambiguous_match_review(line, candidates)
            review_queue.append(review_item)
            resolved_lines.append(
                {
                    "raw_line": line,
                    "decision": "review",
                    "matched_product": None,
                    "flags": review_item["flags"],
                }
            )
            resolution_trace.append(
                {
                    "line_id": line.get("line_id"),
                    "raw_sku": line.get("raw_sku"),
                    "candidate_matches": candidates[:3],
                    "selected_match": None,
                    "selection_status": "review",
                }
            )
            validation_trace.append(
                {
                    "line_id": line.get("line_id"),
                    "raw_sku": line.get("raw_sku"),
                    "matched_product_sku": None,
                    "existing_po_line_id": None,
                    "pending_change_fields": [],
                    "ignored_fields": [],
                    "review_reasons": [review_item["reason"]],
                    "flags": review_item["flags"],
                    "decision": "review",
                }
            )
            continue

        product = best["product"]
        supplier_product = best["supplier_product"]
        existing_line = indexes["po_line_by_key"].get((purchase_order["id"], product["id"]))
        decision_bundle = _validate_and_decide_line(
            line=line,
            purchase_order=purchase_order,
            existing_line=existing_line,
            product=product,
            supplier_product=supplier_product,
            best_match=best,
            note_updates=note_updates,
        )

        resolved_lines.append(decision_bundle["resolved_line"])
        if decision_bundle["proposed_update"]:
            proposed_updates.append(decision_bundle["proposed_update"])
        review_queue.extend(decision_bundle["review_items"])

        resolution_trace.append(
            {
                "line_id": line.get("line_id"),
                "raw_sku": line.get("raw_sku"),
                "candidate_matches": candidates[:3],
                "selected_match": {
                    "product_id": product["id"],
                    "sku": product["sku"],
                    "title": product["title"],
                    "score": best["score"],
                    "reasons": best["reasons"],
                },
                "selection_status": "matched",
            }
        )
        validation_trace.append(decision_bundle["validation_trace"])

    return {
        "resolved_lines": resolved_lines,
        "proposed_updates": proposed_updates,
        "review_queue": review_queue,
        "resolution_trace": resolution_trace,
        "validation_trace": validation_trace,
    }


def _build_note_update_reviews(
    extraction: dict,
    resolved_lines: list[dict[str, Any]],
    indexes: dict[str, Any],
    purchase_order: dict,
) -> dict[str, Any]:
    review_items: list[dict[str, Any]] = []
    note_trace: list[dict[str, Any]] = []

    for note in extraction.get("note_updates", []):
        norm = normalize_sku(note.get("sku_hint"))
        if not norm:
            note_trace.append(
                {
                    "sku_hint": note.get("sku_hint"),
                    "decision": "ignored",
                    "reason": "Note update did not contain a usable SKU hint.",
                }
            )
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
            review_item = {
                "decision": "review",
                "reason": "Email note references a SKU that was not matched anywhere in the extracted attachment.",
                "note": note,
                "flags": ["orphan_note_update"],
            }
            review_items.append(review_item)
            note_trace.append(
                {
                    "sku_hint": note.get("sku_hint"),
                    "decision": "review",
                    "reason": review_item["reason"],
                    "flags": review_item["flags"],
                }
            )
            continue

        existing_line = indexes["po_line_by_key"][(purchase_order["id"], matched_product["product_id"])]
        candidates = note.get("normalized_candidates", [])
        review_item = {
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
        review_items.append(review_item)
        note_trace.append(
            {
                "sku_hint": note.get("sku_hint"),
                "matched_product_sku": matched_product["sku"],
                "decision": "review",
                "reason": review_item["reason"],
                "normalized_candidates": candidates,
                "flags": review_item["flags"],
            }
        )

    return {"review_items": review_items, "note_trace": note_trace}


def resolve_document_with_trace(extraction: dict, tables: dict[str, list[dict]]) -> dict[str, Any]:
    indexes = build_indexes(tables)
    purchase_order = resolve_purchase_order(extraction, indexes)
    supplier = indexes["suppliers"][purchase_order["supplier_id"]]
    matched_purchase_order = build_matched_purchase_order_payload(purchase_order, supplier)
    note_updates = _note_updates_by_sku(extraction)

    line_bundle = _resolve_line_items(extraction, indexes, purchase_order, note_updates)
    note_bundle = _build_note_update_reviews(extraction, line_bundle["resolved_lines"], indexes, purchase_order)
    schema_gaps = build_schema_gaps(extraction)

    review_queue = line_bundle["review_queue"] + note_bundle["review_items"]
    result = {
        "matched_purchase_order": matched_purchase_order,
        "schema_gaps": schema_gaps,
        "resolved_lines": line_bundle["resolved_lines"],
        "proposed_updates": line_bundle["proposed_updates"],
        "review_queue": review_queue,
    }

    trace = {
        "resolution": {
            "matched_purchase_order": matched_purchase_order,
            "line_match_attempts": line_bundle["resolution_trace"],
        },
        "validation": {
            "line_validations": line_bundle["validation_trace"],
            "note_update_validations": note_bundle["note_trace"],
            "schema_gaps": schema_gaps,
        },
    }
    return {"result": result, "trace": trace}


def resolve_document(extraction: dict, tables: dict[str, list[dict]]) -> dict[str, Any]:
    return resolve_document_with_trace(extraction, tables)["result"]
