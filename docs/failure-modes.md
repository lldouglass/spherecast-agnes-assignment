# Failure modes, current hedges, and future fixes

This repo treats the challenge as a staged transaction-ingestion system, so failure analysis should be stage-specific too.

## Stage 1, input / ingestion

### Failure mode: duplicate event replay
- What can go wrong:
  - the same email or attachment gets processed twice
  - downstream updates get proposed twice
- Current hedge in this prototype:
  - raw inputs stay outside the operational tables
  - the sample pipeline only produces staged JSON outputs, not direct writes
- Future production fix:
  - idempotency key from `message_id + attachment_hash`
  - persistent event ledger with dedupe before any downstream processing

### Failure mode: wrong or incomplete source context
- What can go wrong:
  - sender metadata is missing
  - the attachment is detached from the email note that changes its meaning
- Current hedge in this prototype:
  - the extraction schema keeps both attachment-derived and email-body-derived information
  - note-based updates are not silently merged into line updates
- Future production fix:
  - thread-aware ingestion, attachment hashing, and explicit event envelopes

## Stage 2, extraction

### Failure mode: document text is read incorrectly
- What can go wrong:
  - noisy scans, missing rows, or malformed JSON from the model
- Current hedge in this prototype:
  - extraction is a separate stage from resolution and write decisions
  - the resolver only trusts structured fields and can still reject or review them
  - `verify.sh` includes a strict mock API replay path so request shape regressions are visible
- Future production fix:
  - historical replay corpus by supplier/doc type
  - schema validation, automatic JSON repair policy, and model-specific fallback routing

### Failure mode: the model extracts too confidently
- What can go wrong:
  - a plausible but wrong SKU/title/date enters the system with high confidence
- Current hedge in this prototype:
  - extraction confidence never directly authorizes a database write
  - supplier-approved product matching and rule checks still gate automation
- Future production fix:
  - calibration against reviewed outcomes and confidence monitoring by supplier/template

## Stage 3, resolution

### Failure mode: the wrong PO is selected
- What can go wrong:
  - a reference number is malformed or ambiguous
- Current hedge in this prototype:
  - PO matching is deterministic and anchored on explicit PO references, including suffix normalization
  - failure to resolve the PO stops the run instead of guessing
- Future production fix:
  - additional anchors like supplier identity, open-PO date windows, and reviewer-approved aliases

### Failure mode: a line item matches the wrong product
- What can go wrong:
  - near-duplicate SKUs, OCR drift, or noisy titles produce a bad match
- Current hedge in this prototype:
  - matching is restricted to supplier-approved products only
  - ambiguous or low-margin matches go to review instead of auto-apply
  - the trace artifact shows candidate matches and the selected winner
- Future production fix:
  - supplier alias memory, reviewed confusion sets, and per-supplier thresholds

## Stage 4, validation

### Failure mode: a field looks valid syntactically but is unsafe operationally
- What can go wrong:
  - a delivery date moves earlier in a suspicious way
  - a slash-formatted date is locale-ambiguous
  - line totals get mapped into the wrong schema field
- Current hedge in this prototype:
  - suspicious earlier dates are review-gated
  - ambiguous slash dates are review-gated
  - non-schema fields are preserved as schema gaps instead of forced into writes
- Future production fix:
  - supplier locale policies, richer business rules, and explicit staging tables for non-core metadata

### Failure mode: a product is valid for the supplier but not on the open PO lines
- What can go wrong:
  - the system tries to mutate the wrong operational row or invent one implicitly
- Current hedge in this prototype:
  - the updated resolver now review-gates a matched product that does not map to an existing open PO line
- Future production fix:
  - explicit policies for line creation vs amendment, backed by reviewer approval and audit logs

## Stage 5, decision / apply-or-review

### Failure mode: an LLM-assisted stage writes directly into production-critical tables
- What can go wrong:
  - a single extraction mistake becomes live operational corruption
- Current hedge in this prototype:
  - no direct DB writes exist
  - outputs are separated into `proposed_updates`, `review_queue`, and `schema_gaps`
- Future production fix:
  - staged DB tables, reviewer actions, audit trails, and idempotent apply workers

### Failure mode: a module change quietly degrades quality
- What can go wrong:
  - changing the model, prompt, or resolver logic causes silent regressions
- Current hedge in this prototype:
  - `app/eval_sample.py` locks the important sample behaviors
  - `verify.sh` checks both the deterministic offline path and the strict mocked live-extraction path
  - `outputs/pipeline_trace.json` makes stage outputs inspectable
- Future production fix:
  - replay suites over reviewed historical events, supplier-specific metrics, and change-gated rollout policies

## Observability signals I would care about first
- parse failure rate
- unmatched line rate
- ambiguous match rate
- review rate by supplier
- reviewer override rate
- extraction latency and cost by model
- schema-gap frequency by field
- duplicate event rate once true ingestion is added
