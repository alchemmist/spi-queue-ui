# GitHub read transport recovery

Semantic GitHub GET requests map typed UPSTREAM_TIMEOUT and
UPSTREAM_NETWORK_ERROR to GITHUB_READ_TIMEOUT and GITHUB_READ_NETWORK_ERROR.
Under the existing durable v1 workflow contract these return a deferred envelope
(HTTP 202), not a failed n8n node. Both use the same 60-second backoff, existing
owner/fence and original six-hour absolute deadline. Duplicate requests replay
receipts; restart retains progress. Failed reads never acquire paid admission.

This is NOT a generic HTTP retry or a new retry loop: POST/PATCH/PUT/DELETE,
auth failures, malformed responses and ambiguous publications retain their
existing finite failure/reconciliation rules. No TLS check is disabled. Only
safe error enums cross the API boundary, never raw transport exception details.
Legacy endpoints retain their non-202 contract.

Execution 16242 exposed the missing network-error classification: a semantic
GitHub read failed at Match Open SPI and escaped as HTTP 422. Previous waits in
the same execution handled timeouts correctly, but not connection failures.
The historical socket/DNS/TLS subtype was not retained and cannot be asserted.
No historical execution is replayed by this fix. Subsequent independent owner
expiry and WRITE_NEEDS_REVIEW holds must not be erased to manufacture recovery.

## Large catalog stage scheduling and incident links

Durable v1 Match yields MATCH_PROGRESS_DEFERRED after each checkpointed batch
(or pairwise comparison) if further paid work remains. Each subsequent request
gets the existing <=1800-second stage budget; the original six-hour owner
absolute deadline, 1980-second inflight fence, request/token and repair budgets
are unchanged. Completed proofs are replayed, never regenerated. Legacy stage
semantics are unchanged. A real paid transport timeout remains a failure, not
an automatic in-request retry or a pretend success.

Execution16473 reviewed151 open issues:11 batches succeeded, including one
843.626-second call. The next call started with only349 seconds of the shared
stage budget left and timed out. Read-only proxy spend metadata confirms this;
provider load was real, but the caller unnecessarily spent one stage budget on
many paid batches. No provider quota, parallelism or unrelated workload changed.

To avoid multiplying full comment reads on each wake, retained immutable
excerpts are reused only when their content digest and stored issue fingerprint
are valid AND the existing two-pass GitHub open_signature proves freshness.
Changed fingerprints force a fresh snapshot and existing selective proof
invalidation. No fingerprint bypass or frozen-catalog shortcut is introduced.

Owned v1 stage failures log only a validated numeric n8n_execution_id alongside
existing safe enums, never event bodies, job IDs or raw exceptions. The canonical
homelab Grafana rule uses a separate scalar metadata query to construct an n8n
execution URL annotation. It does not put execution IDs into alert labels or
change condition A/C, thresholds, window or routing. Old logs without metadata
cannot establish an execution link and are not rewritten or replayed.
