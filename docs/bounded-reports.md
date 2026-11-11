# Bounded report generation

Execution17941 failed in Analyze with LLM_MODEL_OUTPUT_TRUNCATED after284s.
The production output allowance was2500 tokens, while the report schema permitted
unbounded prose and lists. Raising the allowance or regenerating indefinitely
would spend more without addressing that mismatch.

New generations use a separate bounded schema (`backend/report_bounds.py`):

- title96, summary160, match reason96, impact128, assessment128 characters;
- at most3 facts of120 characters, ending in an actual artifact citation;
- at most2 hypotheses/actions/missing-evidence entries per list,96 characters each;
- issue number is constrained to0 and the actual supplied candidates;
- compact plain-ASCII prose is requested; full diagnostics remain in the evidence.

Hard field caps alone can force a decoder to close a JSON string mid-sentence.
The model does not necessarily see response_format as prompt text. Therefore the
prompt now explicitly targets complete sentences of at most8 words (facts10 plus
citation, title6), well below the character ceilings. Generated prose must end in
sentence punctuation and not an ellipsis; fragments fail validation rather than
being published. This uses no new paid repair. Historical reports are not
revalidated under this generation-only completeness rule.

Output allowance2500, thinking-disabled template, request/admission/owner limits,
exact artifact-ID validation and the single existing durable citation repair
remain unchanged. Every generated report is revalidated after decoding. A length
finish still fails closed even if the content happens to parse: no partial JSON
salvage, clipped findings, invented recovery, new automatic paid regeneration,
terminal replay or silent publication.

The compact ASCII canonical representation at maximum field sizes is under2200
bytes, a conservative byte-level output-token bound without BPE merges. This is
not a guarantee for arbitrary JSON whitespace/Unicode escaping or a provider that
ignores structured output. Finite failure handling remains necessary.

## Real decoder compatibility

The deployed xgrammar0.2.7 ignores maxLength when a pattern is also present, and
its compiler asserts when multiple bounded pattern fields are combined. Therefore
ordinary prose uses native minLength/maxLength WITHOUT pattern. Only facts need
a citation regex; its maximum length is encoded inside the regex itself, not
merely in maxLength. MaxItems and candidate enums are independently constrained.

The exact proposed schema was compiled and matched offline using the deployed
decoder (CPU grammar compilation with a small character vocabulary, no model,
weights, inference request or notification). A valid report was accepted; an
oversized summary, oversized fact, excessive facts, unknown-only citation and
out-of-candidate integer were rejected. Exact citation post-validation still
rejects unknown IDs hidden alongside an allowed citation. Keep these backend
limitations in mind when changing the generation schema.

Historical REPORT_SCHEMA remains unchanged: previously saved complete reports
are not shortened or rejected by the new generation limits. Stored logs, metrics,
traces, graphs, artifact manifests, links, evidence ledgers and publication intents
are not migrated or discarded. Existing stage3 reports publish without another
paid analysis. The fixed20-topic catalog and zero automatic issue creation stay.

Stateful rollout still requires a fresh natural SPI-only logical-owner/native
pending/inflight/paid drain. Push tests/builds but never deploys SPI. Explicit
dispatch publishes an immutable image through deploy-to-socrat@v1 and the existing
GitOps/Argo owner; no occupied worker replacement or manual inference probe.
