# Spherecast - Agnes take-home

This submission treats the challenge as a **transaction ingestion problem**, not just a document extraction problem.

Main idea:

> Do not let an LLM write directly into operational tables.
> First extract structured candidates.
> Then use deterministic resolution, validation, and apply-or-review decisions before anything touches production data.

That is the main design choice behind the whole solution.

## What is included

- `docs/approach.md` - system design and reasoning
- `docs/failure-modes.md` - expected failure points, current hedges, and future mitigations
- `app/` - runnable prototype, stage orchestration, live extraction adapter, and eval harness
- `data/` - assignment inputs plus a deterministic extraction fixture
- `outputs/` - generated results from the sample run, including a stage trace artifact

## What the prototype demonstrates

The prototype focuses on the hardest part of the problem:
- matching noisy extracted fields to the right open PO and PO lines
- deciding what can be auto-applied vs what must go to review
- preserving provenance and safe automation boundaries
- making stage outputs inspectable so model or rule changes are easy to debug

The repo includes both paths:
- `data/extracted_fixture.json` for a deterministic offline run
- `app/extract_live.py` for a real extraction call from the raw email text plus `data/sample_po.jpg` into resolver-compatible JSON

That split is intentional. The hard part is not just reading the document. The hard part is avoiding bad writes into operational data.

## Requirements

- Python 3.9+
- No third-party Python dependencies
- No `pip install` required
- Run all commands from the repo root

## Quick start

Fastest end-to-end verifier:

```bash
bash verify.sh
```

That script runs the offline sample, the eval harness, the stage-trace checks, and a strict Anthropic-compatible mock smoke test for the live extractor.

Offline deterministic run:

```bash
python3 -B app/main.py
python3 -B app/eval_sample.py
```

This generates:
- `outputs/summary.json`
- `outputs/resolved_lines.json`
- `outputs/proposed_updates.json`
- `outputs/review_queue.json`
- `outputs/schema_gaps.json`
- `outputs/pipeline_trace.json`
- `outputs/eval_report.json`

Expected result for the sample:
- `outputs/summary.json` shows `auto_apply_count = 2`, `review_count = 3`, `schema_gap_count = 2`
- `outputs/eval_report.json` shows `passed = true` and `passed_count = 6`

Live extraction run from the raw sample email + PO image:

```bash
ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=claude-sonnet-4-6 \
python3 -B app/extract_live.py --out outputs/extracted_live.json

python3 -B app/main.py --extraction outputs/extracted_live.json
python3 -B app/eval_sample.py --extraction outputs/extracted_live.json
```

For local convenience on your own machine, you can also create an untracked repo-local `.env.local` with:

```bash
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-6
```

The repo should stay keyless in git. `.env.local` is only for local testing, not part of the submission contract.

Mock-API smoke test for the live extraction path, useful when a real API key is unavailable.
Run the mock server in a separate terminal and stop it with `Ctrl-C` when done:

```bash
python3 -B app/mock_anthropic_server.py --capture-path outputs/mock_anthropic_request_capture.json --verbose
```

In a second terminal:

```bash
ANTHROPIC_API_KEY=dummy ANTHROPIC_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_MODEL=mock-model \
python3 -B app/extract_live.py --out outputs/extracted_mock_api_strict.json
python3 -B app/main.py --extraction outputs/extracted_mock_api_strict.json --trace-out outputs/pipeline_trace_verify.json
python3 -B app/eval_sample.py --extraction outputs/extracted_mock_api_strict.json
```

This path writes the captured request to `outputs/mock_anthropic_request_capture.json` so you can inspect the exact `/v1/messages` payload shape.

Optional runtime env vars:
- `ANTHROPIC_BASE_URL` - defaults to `https://api.anthropic.com`
- `ANTHROPIC_MODEL` - defaults to `claude-sonnet-4-6`

## Stage map, in code

Leon called out wanting to see the different stages clearly. The code maps to that request directly:

1. **Input / ingestion boundary**
   - `app/pipeline.py`
   - Loads the reference workbook and the structured extraction payload.

2. **Extraction**
   - `app/extract_live.py`
   - Converts raw email text + attachment image into structured JSON.
   - The offline fixture in `data/extracted_fixture.json` is the deterministic equivalent of this stage.

3. **Resolution**
   - `app/resolver.py`
   - Resolves the PO and line-item candidates against supplier-approved products and open PO lines.

4. **Validation**
   - `app/resolver.py`
   - Applies safety rules such as ambiguous-match review, suspicious-earlier-date review, note override handling, and schema-gap preservation.

5. **Decision: auto-apply vs review vs preserve**
   - `app/pipeline.py` + `app/resolver.py`
   - Packages the final outputs into `proposed_updates`, `review_queue`, `schema_gaps`, and `no_action` lines.

## Reviewer-facing artifacts

If I were reviewing this repo cold, I would look at these files first:

- `outputs/pipeline_trace.json`
  - end-to-end trace of stage outputs, candidate matches, validation findings, and final decisions
- `docs/failure-modes.md`
  - likely failure points, current hedges, and future hardening path
- `app/eval_sample.py`
  - machine-readable regression checks for the sample behaviors that matter operationally
- `verify.sh`
  - one-command proof that the deterministic and mocked live paths still behave correctly

## Expected sample outcome

For the provided sample, the pipeline:
- matches the document to `PO-12`
- resolves scanned `SKU-13` to canonical `SKU-1-3`
- auto-applies the safe `SKU-2` quantity/date update
- auto-applies the safe `SKU-3` quantity update
- sends the suspicious `SKU-3` date to review
- sends the email-only ETA update for `SKU-1-3` to review because `02/01/2027` is locale-ambiguous, while preserving both plausible ISO interpretations
- sends `SKU-7` to review because it cannot be safely matched to a supplier-approved product
- preserves non-mappable fields like `terms` and `external_purchasing_ref_number` as schema gaps instead of silently dropping or misrouting them

## Why this is not just OCR

OCR answers "what text is on the page?" Agnes needs to answer a stricter operational question:
- which live PO does this document belong to
- which supplier-approved product each noisy line refers to
- which fields are safe to write automatically
- which extracted values must be preserved but not written because the current schema has nowhere safe to put them

The live extractor exists to show that the resolver is wired to real raw inputs. The real value is in the **extraction + deterministic resolution + validation + review gating** stack, not in text recognition alone.

## Logging, observability, and regression safety

Leon also called out observability and safe stage swaps.

This repo handles that in three concrete ways:

1. **Trace artifact**
   - `outputs/pipeline_trace.json` shows stage outputs and decisions.
   - It answers, "what happened at each stage, and why?"

2. **Regression harness**
   - `app/eval_sample.py` checks the important operational outcomes, not just whether the code ran.
   - `verify.sh` also validates the mocked live extraction request shape, so changing models, prompts, or adapters is less likely to fail silently.

3. **Separation of concerns**
   - extraction can change without giving the model direct write access
   - resolver rules can change while staying replayable against saved extraction payloads
   - validation policy can tighten or loosen without rewriting the extraction stage

## Repo structure

```text
spherecast-agnes-assignment/
  README.md
  docs/
    approach.md
    failure-modes.md
  data/
    assignment_brief.docx
    example_email.txt
    extracted_fixture.json
    sample_db.xlsx
    sample_po.jpg
  app/
    eval_sample.py
    excel_loader.py
    extract_live.py
    main.py
    mock_anthropic_server.py
    pipeline.py
    resolver.py
  outputs/
    *.json
```

## If I had 2 to 5 more days

I would add:
- a true `proposed_updates` staging table and reviewer UI
- idempotent email event ingestion keyed by message-id + attachment hash
- replay evals over historical supplier communications
- supplier-specific alias memory and template memory
- better observability for auto-apply rate, override rate, unmatched rate, and supplier drift
