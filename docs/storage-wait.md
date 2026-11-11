# Immutable storage publication deferral (18891)

18891 failed Publish with STORAGE_TRANSIENT, while the durable job later completed
normally as18894, with10 immutable objects verified and one primary comment.
Historical executions are not replayed/rewritten. The old generic code cannot
establish whether that particular failure was DNS, timeout or upstream5xx.

The defect was producer/consumer parity: queue recovery considered storage
transient, but native durable stage protocol did not admit it as a Wait outcome.
Only Publish now yields STORAGE_TRANSIENT under the SAME execution, original
absolute6h deadline, finite per-object deadline and verified-prefix checkpoint.
No Match/Collect/Analyze/model replay or extra paid call. Other stages reject this
code. Native JS applies the same publish-only guard; tests execute the real JS.

This is safe specifically because the S3 adapter uses content-bound immutable
keys, conditional PUT and GET byte length/SHA verification. A lost PUT response
must be reconciled by GET, not unconditional overwrite. Completed objects are
not retransmitted and original report/binding remain intact across Wait.
403/auth/config/permanent/conflicting bytes/integrity/GitHub write ambiguity
remain fatal, not patient. Wait keeps the logical slot; it does not authorize a
new claim. Original absolute deadline is not extended.

Additional sanitized S3 reason phases expose DNS_RESOLUTION, TIMEOUT,
HTTP_TRANSPORT, HTTP_408/429/5XX, INCOMPLETE_READ, READBACK_NOT_READY or deadline;
no exception text, credentials, object keys, evidence or response body in logs.
This is recovery/diagnostic correction, NOT a claimed global DNS/network fix.
