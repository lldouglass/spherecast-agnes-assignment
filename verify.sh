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

echo "[0/8] Python version"
python3 --version

echo "[1/8] Syntax compile"
python3 -m py_compile app/*.py

echo "[2/8] Offline deterministic run"
python3 -B app/main.py --trace-out outputs/pipeline_trace.json

echo "[3/8] Offline eval"
python3 -B app/eval_sample.py

python3 -c 'import json; s=json.load(open("outputs/summary.json")); assert s["auto_apply_count"]==2, s; assert s["review_count"]==3, s; assert s["schema_gap_count"]==2, s; print("Summary counts OK:", s)'
python3 -c 'import json; r=json.load(open("outputs/eval_report.json")); assert r["passed"] and r["passed_count"]==6 and r["total_checks"]==6, r; print("Offline eval OK:", str(r["passed_count"]) + "/" + str(r["total_checks"]))'
python3 -c 'import json; t=json.load(open("outputs/pipeline_trace.json")); assert t["stage_order"]==["input","extraction","resolution","validation","decision"], t["stage_order"]; assert t["stages"]["decision"]["summary"]["auto_apply_count"]==2, t["stages"]["decision"]; print("Pipeline trace OK:", t["stage_order"])'

echo "[4/8] Start strict mock Anthropic server"
python3 -B app/mock_anthropic_server.py --capture-path outputs/mock_anthropic_request_capture.json >/tmp/agnes-mock-server-verify.log 2>&1 &
SERVER_PID=$!
sleep 1

echo "[5/8] Strict fake API live extraction"
ANTHROPIC_API_KEY=dummy ANTHROPIC_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_MODEL=mock-model \
python3 -B app/extract_live.py --out outputs/extracted_mock_api_verify.json

echo "[6/8] Replay + eval"
python3 -B app/main.py --extraction outputs/extracted_mock_api_verify.json --trace-out outputs/pipeline_trace_verify.json
python3 -B app/eval_sample.py --extraction outputs/extracted_mock_api_verify.json --out outputs/eval_report_verify.json

python3 -c 'import json; c=json.load(open("outputs/mock_anthropic_request_capture.json")); v=c["validation_summary"]; assert c["path"]=="/v1/messages", c["path"]; assert v["text_part_count"]==1, v; assert v["image_part_count"]==1, v; assert v["temperature"]==0, v; assert v["max_tokens"]==1800, v; print("Mock API request shape OK:", v)'
python3 -c 'import json; t=json.load(open("outputs/pipeline_trace_verify.json")); assert t["stages"]["decision"]["summary"]["review_count"]==3, t["stages"]["decision"]; print("Mock replay trace OK:", t["stages"]["decision"]["summary"])'
python3 -c 'import json; r=json.load(open("outputs/eval_report_verify.json")); assert r["passed"] and r["passed_count"]==6 and r["total_checks"]==6, r; print("Strict fake API eval OK:", str(r["passed_count"]) + "/" + str(r["total_checks"]))'

echo "[7/8] All checks passed"
echo "Artifacts:"
echo "  outputs/summary.json"
echo "  outputs/eval_report.json"
echo "  outputs/pipeline_trace.json"
echo "  outputs/extracted_mock_api_verify.json"
echo "  outputs/pipeline_trace_verify.json"
echo "  outputs/eval_report_verify.json"
echo "  outputs/mock_anthropic_request_capture.json"
