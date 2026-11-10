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
