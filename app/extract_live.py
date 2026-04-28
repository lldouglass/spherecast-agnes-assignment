from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any
from urllib import error, request


SYSTEM_PROMPT = """You extract structured purchase-order data from supplier emails and attached document images.
Return JSON only. Do not include markdown fences or commentary.
Do not invent values that are not visible in the image or explicit in the email body.
"""


USER_PROMPT_TEMPLATE = """Extract a purchase-order update from the email body and attached image.

Return one JSON object with this shape:
{{
  "document_type": "purchase_order",
  "header_fields": {{
    "purchasing_ref_number": {{"value": string_or_null, "source": "attachment", "confidence": number}},
    "external_purchasing_ref_number": {{"value": string_or_null, "source": "attachment", "confidence": number}},
    "terms": {{"value": string_or_null, "source": "attachment", "confidence": number}}
  }},
  "line_item_candidates": [
    {{
      "line_id": string,
      "raw_sku": string_or_null,
      "raw_title": string_or_null,
      "quantity": number_or_null,
      "delivery_date": "YYYY-MM-DD"_or_null,
      "total_price": number_or_null,
      "currency": string_or_null,
      "source": "attachment",
      "confidence": number
    }}
  ],
  "note_updates": [
    {{
      "sku_hint": string_or_null,
      "field": string,
      "raw_value": string_or_null,
      "normalized_candidates": [string],
      "source": "email_body",
      "confidence": number,
      "comment": string_or_null
    }}
  ]
}}

Rules:
- Extract only what is explicitly present in the email or image.
- Keep attachment dates in ISO format only when they are explicit and unambiguous.
- For slash-formatted email dates, preserve the literal string in raw_value. If the date is ambiguous, include every plausible ISO interpretation in normalized_candidates and let downstream review decide.
- Use null for missing title, quantity, date, total_price, or currency.
- Use "attachment" as the source for header fields and line items.
- Use "email_body" as the source for note_updates.
- line_id values should be stable simple identifiers like "scan-1", "scan-2", and so on.
- confidence must be between 0 and 1.

Email sender: {email_from}
Email subject: {email_subject}
Email body:
{email_body}
"""


class ApiError(RuntimeError):
    pass


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_repo_env(root: Path) -> None:
    for candidate in (root / ".env.local", root / ".env"):
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
                value = value[1:-1]
            os.environ[key] = value


def image_to_base64_source(path: Path) -> tuple[str, str]:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime_type, encoded


def clamp_confidence(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "-"}:
        return None
    return text


def coerce_number(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)

    cleaned = str(value).strip().replace(",", "")
    if not cleaned or cleaned.lower() in {"null", "none", "n/a", "-"}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def coerce_iso_date(value: Any) -> str | None:
    text = coerce_string(value)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None


def extract_json_blob(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model response did not contain a JSON object.")
    return json.loads(stripped[start : end + 1])


def normalize_header_field(value: Any, default_source: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "value": coerce_string(value.get("value")),
            "source": coerce_string(value.get("source")) or default_source,
            "confidence": clamp_confidence(value.get("confidence"), 0.75),
        }
    return {
        "value": coerce_string(value),
        "source": default_source,
        "confidence": 0.75 if value is not None else 0.0,
    }


def slash_date_candidates(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", raw_value)
    if not match:
        return []

    left, right, year = (int(part) for part in match.groups())
    if left > 12 and right > 12:
        return []
    if left == right:
        return [f"{year:04d}-{left:02d}-{right:02d}"]

    candidates = []
    if left <= 12 and right <= 31:
        candidates.append(f"{year:04d}-{left:02d}-{right:02d}")
    if right <= 12 and left <= 31:
        candidates.append(f"{year:04d}-{right:02d}-{left:02d}")

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique_candidates.append(candidate)
    return unique_candidates


def normalize_note_update(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    raw_value = coerce_string(item.get("raw_value"))
    normalized_candidates = item.get("normalized_candidates")
    if not isinstance(normalized_candidates, list):
        normalized_candidates = []

    cleaned_candidates = [candidate for candidate in (coerce_string(item) for item in normalized_candidates) if candidate]
    if not cleaned_candidates:
        cleaned_candidates = slash_date_candidates(raw_value)

    return {
        "sku_hint": coerce_string(item.get("sku_hint")),
        "field": coerce_string(item.get("field")) or "delivery_date",
        "raw_value": raw_value,
        "normalized_candidates": cleaned_candidates,
        "source": coerce_string(item.get("source")) or "email_body",
        "confidence": clamp_confidence(item.get("confidence"), 0.75),
        "comment": coerce_string(item.get("comment")),
    }


def normalize_line_item(value: Any, index: int) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "line_id": coerce_string(item.get("line_id")) or f"scan-{index}",
        "raw_sku": coerce_string(item.get("raw_sku")),
        "raw_title": coerce_string(item.get("raw_title")),
        "quantity": coerce_number(item.get("quantity")),
        "delivery_date": coerce_iso_date(item.get("delivery_date")),
        "total_price": coerce_number(item.get("total_price")),
        "currency": coerce_string(item.get("currency")),
        "source": coerce_string(item.get("source")) or "attachment",
        "confidence": clamp_confidence(item.get("confidence"), 0.75),
    }


def build_payload(raw_payload: dict[str, Any], email_from: str, email_subject: str, email_body: str) -> dict[str, Any]:
    line_items = raw_payload.get("line_item_candidates")
    if not isinstance(line_items, list):
        line_items = []

    note_updates = raw_payload.get("note_updates")
    if not isinstance(note_updates, list):
        note_updates = []

    header_fields = raw_payload.get("header_fields")
    if not isinstance(header_fields, dict):
        header_fields = {}

    payload = {
        "document_type": "purchase_order",
        "source_email": {
            "from": email_from,
            "subject": email_subject,
            "body": email_body,
        },
        "header_fields": {
            "purchasing_ref_number": normalize_header_field(header_fields.get("purchasing_ref_number"), "attachment"),
            "external_purchasing_ref_number": normalize_header_field(
                header_fields.get("external_purchasing_ref_number"), "attachment"
            ),
            "terms": normalize_header_field(header_fields.get("terms"), "attachment"),
        },
        "line_item_candidates": [normalize_line_item(item, index) for index, item in enumerate(line_items, start=1)],
        "note_updates": [normalize_note_update(item) for item in note_updates],
    }

    if not payload["header_fields"]["purchasing_ref_number"]["value"]:
        raise ValueError("Live extraction did not return a purchasing_ref_number.")

    if not payload["line_item_candidates"]:
        raise ValueError("Live extraction did not return any line_item_candidates.")

    return payload


def anthropic_messages_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


def read_messages_api(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    image_media_type: str,
    image_base64: str,
) -> dict[str, Any]:
    url = anthropic_messages_url(base_url)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1800,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image_media_type,
                            "data": image_base64,
                        },
                    },
                ],
            },
        ],
    }

    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as response:
            raw_response = json.loads(response.read().decode("utf-8"))
            content = raw_response.get("content")
            if not isinstance(content, list):
                raise ApiError("Model response content was missing or not a list.")
            message = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
            if not message.strip():
                raise ApiError("Model response did not contain any text blocks.")
            return extract_json_blob(message)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"Extraction API request failed with HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise ApiError(f"Extraction API request failed: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError(f"Could not parse extraction model response: {exc}") from exc


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_repo_env(root)

    parser = argparse.ArgumentParser(description="Run live extraction from the raw sample email body and PO image.")
    parser.add_argument("--email", default="data/example_email.txt")
    parser.add_argument("--attachment", default="data/sample_po.jpg")
    parser.add_argument("--email-from", default="big@supplier.com")
    parser.add_argument("--email-subject", default="Scanned purchase order")
    parser.add_argument("--out", default="outputs/extracted_live.json")
    parser.add_argument("--base-url", default=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    parser.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"))
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Example:\n"
            "ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=claude-3-5-sonnet-latest "
            "python3 -B app/extract_live.py --out outputs/extracted_live.json"
        )

    email_path = (root / args.email).resolve()
    attachment_path = (root / args.attachment).resolve()
    out_path = (root / args.out).resolve()

    email_body = email_path.read_text(encoding="utf-8").strip()
    prompt = USER_PROMPT_TEMPLATE.format(
        email_from=args.email_from,
        email_subject=args.email_subject,
        email_body=email_body,
    )
    image_media_type, image_base64 = image_to_base64_source(attachment_path)

    raw_payload = read_messages_api(api_key, args.base_url, args.model, prompt, image_media_type, image_base64)
    payload = build_payload(raw_payload, args.email_from, args.email_subject, email_body)
    write_json(out_path, payload)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
