# Spherecast - Agnes take-home

This submission treats the assignment as a **production-safe transaction ingestion problem**, not just a document extraction problem.

The core idea is simple:

> Do not let an LLM write directly into operational tables.
> Use an LLM-compatible extraction layer to produce structured candidates, then run deterministic matching, validation, and staged update decisions before anything touches production data.

That design choice is the difference between a flashy demo and a system that can safely support real purchase-order operations.

## What is included

- `docs/approach.md` - the design doc
- `app/` - the runnable prototype, live extraction adapter, and eval harness
- `data/` - the assignment inputs plus a deterministic extraction fixture
- `outputs/` - generated results from the sample run

## What the prototype demonstrates

The prototype focuses on the hardest part of the problem:
- matching noisy extracted fields to the right open PO and PO lines
- deciding what can be auto-applied vs what must go to review
- preserving provenance and safe automation boundaries

The repo now includes both paths:
- `data/extracted_fixture.json` for a deterministic offline run
- `app/extract_live.py` for a real extraction call from the raw email text plus `data/sample_po.jpg` into resolver-compatible JSON

That split is intentional. The riskiest production problem is not OCR in isolation. It is **corrupting production-critical data with false certainty**.

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

That script runs the offline sample, the eval harness, and a strict Anthropic-compatible mock smoke test for the live extractor.

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
- `outputs/eval_report.json`

Expected result for the sample:
- `outputs/summary.json` shows `auto_apply_count = 2`, `review_count = 3`, `schema_gap_count = 2`
- `outputs/eval_report.json` shows `passed = true` and `passed_count = 6`

Live extraction run from the raw sample email + PO image:

```bash
ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=claude-3-5-sonnet-latest \
python3 -B app/extract_live.py --out outputs/extracted_live.json

python3 -B app/main.py --extraction outputs/extracted_live.json
python3 -B app/eval_sample.py --extraction outputs/extracted_live.json
```

For local convenience on your own machine, you can also create an untracked repo-local `.env.local` with:

```bash
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-3-5-sonnet-latest
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
python3 -B app/main.py --extraction outputs/extracted_mock_api_strict.json
python3 -B app/eval_sample.py --extraction outputs/extracted_mock_api_strict.json
```

This path writes the captured request to `outputs/mock_anthropic_request_capture.json` so you can inspect the exact `/v1/messages` payload shape.

Optional runtime env vars:
- `ANTHROPIC_BASE_URL` - defaults to `https://api.anthropic.com`
- `ANTHROPIC_MODEL` - defaults to `claude-3-5-sonnet-latest`

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

## Why this is not "just OCR"

OCR answers "what text is on the page?" Agnes needs to answer a stricter operational question:
- which live PO does this document belong to
- which supplier-approved product each noisy line refers to
- which fields are safe to write automatically
- which extracted values must be preserved but not written because the current schema has nowhere safe to put them

The live extractor exists to prove that the resolver is wired to real raw inputs. The actual product value is in the **model + deterministic resolution + validation + review gating** stack, not in text recognition alone.

## Why this approach

I would not build Agnes as a freeform email agent that writes directly to the database.
I would build it as an **LLM-assisted transaction ingestion system** with:

- structured extraction
- deterministic entity resolution
- field-level confidence
- staged updates
- auditability
- review queues for ambiguity
- a feedback loop that improves supplier matching and extraction over time

That architecture is small enough to ship quickly, but strong enough to scale from purchase orders to transfer orders, work orders, shipments, and other supply-chain transactions.

## Model, evals, and observability

Model selection philosophy:
- start with one strong vision model that can jointly read the attachment and email body into structured JSON
- optimize for extraction recall and JSON reliability first, not marginal token cost
- only introduce tiered routing or smaller models after replay evals show which suppliers and document families are truly easy

Eval / replay philosophy:
- every model or prompt change should replay against saved raw emails, attachments, and reviewed outcomes
- this repo now includes `app/eval_sample.py` as the seed of that discipline: a machine-readable pass/fail report over the sample behaviors that matter
- the same pattern scales to a larger gold set built from historical reviewer-approved events

Observability / feedback loop:
- track parse failures, unmatched lines, review reasons, reviewer overrides, supplier drift, latency, and cost
- store raw source material, extracted JSON, resolver output, and final reviewer decision so failures are debuggable and replayable
- promote reviewer corrections into supplier alias memory and future eval cases

Automation boundary:
- auto-apply only when the PO match is clear, the line maps to a supplier-approved product, and the field change is non-ambiguous
- review-gate ambiguous dates, unmatched lines, suspiciously earlier dates, and schema fields that cannot be written safely

## Repo structure

```text
spherecast-agnes-assignment/
  README.md
  docs/
    approach.md
  data/
    assignment_brief.docx
    example_email.txt
    extracted_fixture.json
    sample_db.xlsx
    sample_po.jpg
  app/
    excel_loader.py
    eval_sample.py
    extract_live.py
    main.py
    mock_anthropic_server.py
    resolver.py
  outputs/
    *.json
```

## If I had 2 to 5 more days

I would add:
- a `proposed_updates` staging table and reviewer UI
- idempotent email event ingestion keyed by message-id + attachment hash
- replay evals over historical supplier communications
- supplier-specific alias memory and template memory
- observability for auto-apply rate, override rate, unmatched rate, and supplier drift
