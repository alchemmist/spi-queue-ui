# Managed SPI workstreams

## Contract

There are at most20 managed open SPI issues. **Automation creates zero issues**:
every production GitHub client rejects POST /issues, including alternate slash /
query spellings, before transport. This is stronger than a count-then-create cap
(which cannot be atomic against independent human GitHub writes). Humans must
free a slot before opening/promoting another SPI and update the reviewed registry.
The application does not police or silently close unrelated human-created issues.

`backend/managed_catalog.json` binds20 distinct anchors and historical source
aliases. Each anchor must be open, must be a non-PR SPI in the existing repository,
and must carry its exact `socrat-spi-catalog:v1:<topic>` marker. Missing/closed/
wrong targets fail closed; they never authorize creating a replacement issue.

## Bounded fast path

Managed Match makes no model call, no full issue snapshot and no comment-history
read. Stable service/workload identities select a fixed investigation topic;
changing a pod hash, alert expression or observed root-cause uncertainty does not
create another tracking issue. Unknown/critical unmatched events enter review#17.
Availability signals use#50, but a report assessed high/critical promotes to#17
without a second paid analysis. A topic is NOT a declaration of common cause.
The model may retain issue_number=0 in its independent report; publication routes
that report to the configured topic with an explicit organizational-routing banner.

Collect, evidence citation validation, diagnostic logs/traces/metric samples,
immutable S3 verification and SSO graph/evidence links remain intact. Analysis
still has its original request/token/repair/owner budgets. Publication is an
idempotency-marked comment, never a new issue. Comment churn does not invalidate a
paid report or trigger a catalog-wide snapshot rewind. An ambiguous POST retains
its original write intent and reconciliation rules; it is never blindly repeated.
Normal work therefore does not become more expensive as topic comment history grows.
This removes catalog-size latency; provider contention can still delay analysis.

## History and migration

Migration consolidates152 source reports into20 open topics,132 standalone
closures. Closures use not_planned plus an explicit consolidated comment, NOT a
claim of remediation. Each target lists its sources; each source points back.
Original source bodies/comments/graph URLs/S3 objects remain unchanged. Anchor
bodies preserve their original reports verbatim under a new index. Different
symptoms and unknown causes remain separate subcases within a workstream.
Old completed/cancelled/dead jobs and evidence ledgers are not migrated or reset.
Recovery events can follow historical source aliases without rewriting old jobs.
Existing ambiguous publication intents always reconcile their original target first.

Do not change the GitHub catalog underneath an active legacy snapshot. Pause only
new SPI intake, allow the current native execution (including persisted Waits)
to finish, and verify WAL-aware job/owner/paid state plus native pending readback.
Queued admissions remain durable; an empty queue is not required. Only then
consolidate, publish an immutable full-service image and use the existing explicit
SPI workflow_dispatch -> deploy-to-socrat@v1 -> image-only GitOps -> existing Argo
owner. Ordinary push never deploys the stateful worker. Restore the original
workflow activity after healthy runtime/registry verification. No manual test
execution, historical replay, terminal retry, quota reset or alert muting is used.

## Open topics

- #6 Registry bootstrap and external image availability
- #9 Telemetry, collector reliability and monitoring blind spots
- #12 SPI processing, ownership, queue and publication reliability
- #16 Node CPU and I/O resource contention
- #17 Unclassified incidents and high-impact promotion queue
- #18 Cluster memory capacity and overcommitment
- #19 Alert delivery and notification transport
- #21 LMS availability and authentication failures
- #25 Harbor jobservice and registry runtime reliability
- #42 Prometheus availability and TSDB integrity
- #50 Availability checks and rollout readiness investigations
- #51 Calendar session and announcement reliability
- #56 n8n memory and execution runtime stability
- #72 Uptime Kuma application and database reliability
- #79 NetBird, STUN and VPN transport reliability
- #112 Storage provisioner and RustFS access reliability
- #135 Cluster node reachability and readiness
- #146 Image proxy upstream network and IPv6 failures
- #174 Cluster observer membership reconciliation
- #176 Kubernetes aggregated Metrics API availability
