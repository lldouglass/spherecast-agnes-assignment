from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

MOCK_PAYLOAD = {
    "document_type": "purchase_order",
    "header_fields": {
        "purchasing_ref_number": {"value": "12", "source": "attachment", "confidence": 0.99},
        "external_purchasing_ref_number": {"value": "43123", "source": "attachment", "confidence": 0.91},
        "terms": {"value": "DAP", "source": "attachment", "confidence": 0.96},
    },
    "line_item_candidates": [
        {
            "line_id": "scan-1",
            "raw_sku": "SKU-13",
            "raw_title": "PRODUCT ONE version 3",
            "quantity": 15000,
            "delivery_date": "2026-06-01",
            "total_price": 35000,
            "currency": None,
            "source": "attachment",
            "confidence": 0.95,
        },
        {
            "line_id": "scan-2",
            "raw_sku": "SKU-1",
            "raw_title": None,
            "quantity": 10000,
            "delivery_date": None,
            "total_price": 9000,
            "currency": None,
            "source": "attachment",
            "confidence": 0.92,
        },
        {
            "line_id": "scan-3",
            "raw_sku": "SKU-2",
            "raw_title": None,
            "quantity": 500,
            "delivery_date": "2026-07-01",
            "total_price": None,
            "currency": None,
            "source": "attachment",
            "confidence": 0.93,
        },
        {
            "line_id": "scan-4",
            "raw_sku": "SKU-3",
            "raw_title": None,
            "quantity": 500,
            "delivery_date": "2025-06-01",
            "total_price": None,
            "currency": None,
            "source": "attachment",
            "confidence": 0.89,
        },
        {
            "line_id": "scan-5",
            "raw_sku": "SKU-7",
            "raw_title": "[Secret] product version 4",
            "quantity": 1000,
            "delivery_date": None,
            "total_price": None,
            "currency": None,
            "source": "attachment",
            "confidence": 0.84,
        },
    ],
    "note_updates": [
        {
            "sku_hint": "SKU-1-3",
            "field": "delivery_date",
            "raw_value": "02/01/2027",
            "normalized_candidates": ["2027-02-01", "2027-01-02"],
            "source": "email_body",
            "confidence": 0.97,
            "comment": "Supplier explicitly says the ETA was pushed back further than the scanned attachment.",
        }
    ],
}


def _extract_text_and_image_parts(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_prompt = payload.get("system")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("payload.system must be a non-empty string")

    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("payload.messages must contain exactly one user message")

    user_message = messages[0]
    if not isinstance(user_message, dict) or user_message.get("role") != "user":
        raise ValueError("messages[0] must be a user message")

    content = user_message.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("user message content must be a non-empty multimodal list")

    text_parts = [item for item in content if isinstance(item, dict) and item.get("type") == "text"]
    image_parts = [item for item in content if isinstance(item, dict) and item.get("type") == "image"]
    if not text_parts:
        raise ValueError("user message must include a text part")
    if not image_parts:
        raise ValueError("user message must include an image part")
    return text_parts, image_parts



def validate_request_payload(payload: dict[str, Any], headers: Any) -> dict[str, Any]:
    api_key = headers.get("x-api-key", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("missing or invalid x-api-key header")

    anthropic_version = headers.get("anthropic-version", "")
    if anthropic_version != "2023-06-01":
        raise ValueError("anthropic-version must be 2023-06-01")

    content_type = headers.get("Content-Type", "")
    if "application/json" not in content_type.lower():
        raise ValueError("Content-Type must include application/json")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("payload.model must be a non-empty string")

    max_tokens = payload.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ValueError("payload.max_tokens must be a positive integer")

    text_parts, image_parts = _extract_text_and_image_parts(payload)

    image_source = image_parts[0].get("source")
    if not isinstance(image_source, dict):
        raise ValueError("image part must contain a source object")
    if image_source.get("type") != "base64":
        raise ValueError("image source type must be base64")

    media_type = image_source.get("media_type")
    if not isinstance(media_type, str) or "/" not in media_type:
        raise ValueError("image source media_type must be a MIME type")

    data = image_source.get("data")
    if not isinstance(data, str) or not data.strip():
        raise ValueError("image source data must be a non-empty base64 string")

    return {
        "model": model,
        "temperature": payload.get("temperature"),
        "max_tokens": max_tokens,
        "text_part_count": len(text_parts),
        "image_part_count": len(image_parts),
        "text_preview": str(text_parts[0].get("text", ""))[:240],
        "image_media_type": media_type,
        "image_data_prefix": data[:32],
    }


class Handler(BaseHTTPRequestHandler):
    capture_path: Path | None = None
    verbose: bool = False

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path != "/v1/messages":
            self._send_json(404, {"error": {"message": f"unexpected path: {self.path}"}})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": {"message": f"invalid JSON body: {exc}"}})
            return

        try:
            validation_summary = validate_request_payload(payload, self.headers)
        except ValueError as exc:
            self._send_json(400, {"error": {"message": str(exc)}})
            return

        if self.capture_path is not None:
            self.capture_path.parent.mkdir(parents=True, exist_ok=True)
            self.capture_path.write_text(
                json.dumps(
                    {
                        "path": self.path,
                        "headers": {
                            "x-api-key": self.headers.get("x-api-key"),
                            "anthropic-version": self.headers.get("anthropic-version"),
                            "Content-Type": self.headers.get("Content-Type"),
                        },
                        "validation_summary": validation_summary,
                        "request": payload,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        if self.verbose:
            print(json.dumps(validation_summary, indent=2), flush=True)

        self._send_json(
            200,
            {
                "id": "mock-msg-1",
                "type": "message",
                "role": "assistant",
                "model": payload["model"],
                "content": [{"type": "text", "text": json.dumps(MOCK_PAYLOAD)}],
                "stop_reason": "end_turn",
            },
        )

    def log_message(self, format: str, *args) -> None:
        return



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tiny Anthropic-compatible mock server for extractor smoke tests. Validates the /v1/messages request shape before returning a deterministic response."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--capture-path", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    Handler.capture_path = Path(args.capture_path).resolve() if args.capture_path else None
    Handler.verbose = args.verbose

    server = HTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
