#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "[0/7] Python version"
python3 --version

echo "[1/7] Syntax compile"
python3 -m py_compile app/*.py

echo "[2/7] Offline deterministic run"
python3 -B app/main.py

echo "[3/7] Offline eval"
python3 -B app/eval_sample.py

python3 -c 'import json; s=json.load(open("outputs/summary.json")); assert s["auto_apply_count"]==2, s; assert s["review_count"]==3, s; assert s["schema_gap_count"]==2, s; print("Summary counts OK:", s)'
python3 -c 'import json; r=json.load(open("outputs/eval_report.json")); assert r["passed"] and r["passed_count"]==6 and r["total_checks"]==6, r; print("Offline eval OK:", str(r["passed_count"]) + "/" + str(r["total_checks"]))'

echo "[4/7] Start strict mock Anthropic server"
python3 -B app/mock_anthropic_server.py --capture-path outputs/mock_anthropic_request_capture.json >/tmp/agnes-mock-server-verify.log 2>&1 &
SERVER_PID=$!
sleep 1

echo "[5/7] Strict fake API live extraction"
ANTHROPIC_API_KEY=dummy ANTHROPIC_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_MODEL=mock-model \
python3 -B app/extract_live.py --out outputs/extracted_mock_api_verify.json

echo "[6/7] Replay + eval"
python3 -B app/main.py --extraction outputs/extracted_mock_api_verify.json
python3 -B app/eval_sample.py --extraction outputs/extracted_mock_api_verify.json --out outputs/eval_report_verify.json

python3 -c 'import json; c=json.load(open("outputs/mock_anthropic_request_capture.json")); v=c["validation_summary"]; assert c["path"]=="/v1/messages", c["path"]; assert v["text_part_count"]==1, v; assert v["image_part_count"]==1, v; assert v["temperature"]==0, v; assert v["max_tokens"]==1800, v; print("Mock API request shape OK:", v)'
python3 -c 'import json; r=json.load(open("outputs/eval_report_verify.json")); assert r["passed"] and r["passed_count"]==6 and r["total_checks"]==6, r; print("Strict fake API eval OK:", str(r["passed_count"]) + "/" + str(r["total_checks"]))'

echo "[7/7] All checks passed"
echo "Artifacts:"
echo "  outputs/summary.json"
echo "  outputs/eval_report.json"
echo "  outputs/extracted_mock_api_verify.json"
echo "  outputs/eval_report_verify.json"
echo "  outputs/mock_anthropic_request_capture.json"
