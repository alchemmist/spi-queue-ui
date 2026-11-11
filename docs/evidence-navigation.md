# Human evidence and context contract

A short model brief does not replace evidence navigation. The S3 publication path
now emits the manifest's validated Grafana Explore links, including absolute
Prometheus/Loki/Tempo ranges, and explicit evidence gaps. Successful graph entries
link both the private RustFS PNG and the Grafana panel with identical from/to,
panel ID and variables. Resolved panel requests retain a panel link even if
capture fails. No arbitrary alert-supplied origin is promoted into navigation.
Navigation has finite publication budgets and reports omissions.

Graph capture previously failed before rendering: kubelet fsGroup widened the
service-owned graph spool to2770, while checkpoints require private permissions.
The worker now uses no-follow descriptors and tightens only service-owned,
service-group, non-world-accessible objects to0700/0600 as needed. It never chowns,
repairs world-accessible/foreign-group objects, deletes historical checkpoints,
relaxes symlink checks or changes existing content. Existing cached PNG bytes and
capture times are reused, not re-rendered or rebound. The same rules protect
checkpoint files after a PVC remount.

Loki collection now includes a bounded all-level sample before error/stack reads,
even when no error anchor exists. The original ten-request, response-size,
line-count, stage and model budgets remain. Baseline trace IDs can seed existing
bounded Tempo discovery. Scrape exporter pods require verified affected-workload
identity; an unverified exporter is not queried under the target namespace and
selectors are not silently widened.

Log artifacts retain the fetched bounded upstream JSON alongside the normalized
sample, exact query/window/limits and collection time; context snapshots do too.
Failed attempted reads produce failure receipts, not fake empty success. No-match,
unsafe scope, expired lookback, unavailable data and exhausted budgets remain
explicit unknowns, never proof of service health. Existing redaction applies to
responses before storage/model context; no auth headers are stored.

Text artifact budgets are shared fairly. When a sanitized document exceeds its
allocation, its .json file is a VALID JSON envelope identifying a bounded text
excerpt, original sanitized digest/size and truncation, not a chopped JSON string
masquerading as a complete response. This is not an exhaustive/unbounded log dump.
All full retained objects remain private behind existing Evidence/Authentik SSO.

Images are not sent to the model. Model excerpt/citation, output/repair/token
limits and the20-topic/no-automatic-issue policy stay unchanged. Old persisted
bundles/publication intents are not recollected or regenerated. Stateful rollout
still requires natural logical-owner/native pending/processing/paid drain; queued
admissions are retained, never treated as disposable drain blockers.
