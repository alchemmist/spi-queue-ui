# Alert graphs — implementation checkpoint (not activated)

## Ownership and safety

Application code lives in this repository, not in homelab backend ConfigMap
snapshots. The graph gateway is an isolated runtime role of the same immutable
release image: `python -B /app/graph_gateway.py`. It imports no service engine,
opens no queue, and requires no model, GitHub, S3 or worker credentials.
Homelab owns its opt-in Grafana-side Deployment/Service/NetworkPolicy.

This checkpoint includes the common gateway/client and opt-in SPI binary evidence
integration. The companion socrat-bot implementation adds native primary-photo
delivery. Activation is gated separately; source is not end-to-end acceptance.

## Contract

An alert annotation `spi_graphs` contains a JSON string:

```json
{"version":1,"graphs":[{"id":"primary","title":"Request latency","dashboard_uid":"service-health","panel_id":12,"variables":{"service":{"label":"service"}},"before_seconds":1800,"after_seconds":300}]}
```

Order is priority: Telegram uses only the first panel; SPI uses all declared
panels. This is an example, not an installed rule/panel mapping. At most 16
unique graph IDs. Literal bindings use `{"literal":"api"}`. Unknown fields,
duplicate JSON keys, malformed IDs, unbounded windows and accidental All selectors
are rejected. Missing annotation remains compatible with existing alerts.
Missing labels never become Grafana All. Annotation transport must also honor the
existing ingress annotation length limit.

`graph_contract.resolve` takes persisted `observed_at` rather than an implicit
clock. It produces a canonical request with absolute millisecond timestamps,
variable bindings, a SHA-256 render digest, 1200x600 dimensions and light theme.
Onset and resolved windows are clipped to available data. Telegram ongoing
notifications can use a recent window; SPI retains onset context. All timestamps
must include a timezone. No caller-provided origin, credentials or arbitrary URL.

## Gateway

`POST /v1/render` receives the resolved request and returns PNG bytes,
`X-Content-SHA256` and `X-Render-Digest`. Client and server validate PNG framing,
CRC, dimensions, bounded decompressed scanlines and checksums. Errors use bounded
machine codes; bodies, request paths and credentials are not logged.

Credentials are mounted read-only at `/graph-secrets/{grafana,telegram,spi}`.
Telegram and SPI use distinct credentials; their identity/priority is derived
from authentication, not a client-supplied priority field. Policy is mounted at
`/graph-policy/panels.json`, e.g. `{"service-health:12":["service"]}`. The gateway
accepts only explicitly allowed panels and exact variable-name sets. The example
is not a reviewed production allowlist.

The only Grafana upstream is
`kube-prometheus-stack-grafana.cattle-monitoring-system.svc.cluster.local:80`.
The client uses only
`socrat-alert-graphs.cattle-monitoring-system.svc.cluster.local:8080`.
Redirects are not followed. Existing bounded resolver/socket deadline primitives
are reused, with no S3 credential loading.

Two renderer slots, at most one SPI render, single-flight by digest, one-hour
16MiB LRU cache. Telegram upstream budget is four seconds within its planned
five-second caller budget; SPI is twenty seconds. Payloads and PNGs are bounded,
server request concurrency is capped, and socket requests have an absolute cap.
The cache is ephemeral and is **not** durable evidence or a checkpoint.

## Remaining integration work (must not be claimed complete)

1. Inventory all Grafana/Prometheus rules and Git-owned panels; author actual
   priority mappings, add missing diagnostic panels and CI coverage validation.
2. Configure/pin the native remote Grafana renderer and issue least-privilege
   Grafana read and gateway client credentials. Validate CNI behavior for the
   hostNetwork Telegram bot; do not widen to all node/pod CIDRs.
3. Preserve ingress timestamps durably and integrate the gateway client with SPI
   evidence collection, per-graph checkpoints, existing deadlines and immutable
   binary publication. The current external Reader contract snapshot already
   recognizes `socrat-evidence-v3`/`stored_bytes` and PNG presentation; verify its
   actual deployed owner/version before extending any Reader code. Do not treat
   a contract snapshot as the deployed service's development source.
4. Extend socrat-bot's native desired-state delivery for primary-photo messages
   without replacing its revision/lease/idempotency guarantees. Existing separate
   image replies are not equivalent to the requested one-message format. Maintain
   mute/ESC/resolved/SPI links and full-text fallback for caption overflow.
5. Update original n8n workflows in place, release through ordinary CI/GitOps and
   perform safe stateful activation and exact end-to-end acceptance. No historical
   job revival, paid model probe or test Telegram send has occurred here.

## Offline verification

`python3 scripts/test_all.py`: 321 tests, three existing optional native-runtime
skips in host execution. New graph contract/renderer/client tests: 26 passing.
They include only synthetic PNGs, fake transports and a loopback HTTP server.
No test requires production credentials or accesses Grafana/Telegram/S3.
The production-image test gate still requires zero skips.

## Binary producer and compatibility

`SPI_GRAPHS_ENABLED=true` requires `SPI_EVIDENCE_BACKEND=s3` and the scoped token
file `/graph-client/spi`. Capture timestamps/specifications and exact PNG bytes
are checkpointed under private `/data/graphs/<event_key>/`; references, not bytes
or base64, enter jobs. The spool has a 512MiB soft ceiling and a 256MiB free-space
reserve; exhaustion is explicit missing evidence, not a queue reset or deletion.
The existing S3 publisher verifies every PNG like other immutable objects.

A binary-bearing bundle uses the Reader's existing `socrat-evidence-v3` contract
with `stored_bytes`; v2 jobs remain byte-identical. Human PNGs are detached from
the model artifact index and cannot be cited as model-reviewed evidence. Legacy
comment publication rejects binary bundles instead of silently omitting PNGs.
GitHub gets ordinary protected PNG links with exact UTC ranges, never public ACLs
or embedded images. Partial collection failures are explicit in the manifest.

After sixty seconds the collector persists captured files and defers with
`GRAPH_CAPTURE_DEFERRED`; the next same-execution collect reuses them. Add this
code to the **live workflow validator before enabling the worker flag**. Owner,
fence, stage and absolute deadline remain the existing protocol. No matching or
paid model call is replayed by a graph deferral.

Updated host regression:329tests,3existing optional native skips; the constrained native linux/amd64 Docker run also passed all329 with zero skips
(1CPU,384MiB,offline,read-only,64MiBtmpfs). Companion bot tests include isolated PostgreSQL and SQLite
(202passed,17explicit backend-specific skips); homelab verifies every196 source
rule, panel linkage, annotation coverage and unchanged conditions. Live/render,
actual authenticated download and natural Telegram acceptance are still separate
gates. Rule-scoped diagnostic panels can contain multiple series; their legends
and descriptions explicitly disclose that scope rather than claiming an exact
per-instance match.

##2026-11-09 release receipt: source shipped, activation blocked

- Application commit `45af122493392d303b5f2ad978004fd668fb2a3f` and bot commit
  `dfff15e37a005b3a770b2882f762079d4fa067a6` are on their respective main branches.
- Homelab `a1a3fa3` reconciled successfully. All189 live Prometheus alert rules
  have `spi_graphs`; all28 dashboard ConfigMaps exist. The7 Grafana-managed rule
  files are mapped in source but their live annotations have **not** been updated.
- The deployed independent `identity/rustfs-gateway` already supports v3,
  `stored_bytes` and PNG. Runtime source SHA256:
  `2896e7224d662cd742ce40717c241868eaa3f65e7076582d8db9b050d0fe4a64`.
  No contract snapshot was substituted for deployed Reader source.
- A loopback cross-repository check exercised the real bot capture client, real
  `/v1/primary` handler, fake Grafana transport and durable SQLite PNG snapshot.
  Exact PNG bytes and reuse passed without Telegram, model or S3 calls.
- [SPI CI](https://github.com/alchemmist/spi-triage/actions/runs/42610770650)
  passed tests/build, then failed Docker login to canonical Harbor with
  `net/http: TLS handshake timeout` at the token endpoint. No image was published.
- [Bot CI](https://github.com/alchemmist/socrat-bot/actions/runs/42610352796)
  passed tests; Docker login failed on the initial run and one bounded failed-job
  retry with the same TLS symptom. Bot code/migration have not been deployed.
- SPI publication is held false again. No DNS, routing, TLS-verification or
  authentication workaround was used. Gateway/consumer flags remain disabled.
  No historical work was replayed and no real test notification was sent.

Once canonical hosted-runner Harbor authentication works, publish pinned images
through the existing CI, prepare the dedicated machine identities/mounts and
remote renderer, verify actual hostNetwork/CNI sources and protected downloads,
update the live n8n validator before worker activation, apply the seven managed
annotation updates in place, and use the documented natural drain before any
backend replacement. Then observe real one-message Telegram delivery. These are
still release/acceptance gates, not completed production functionality.
