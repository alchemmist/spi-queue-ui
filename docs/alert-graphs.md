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

## Production activation receipt — 2026-11-09 14:01 UTC

This supersedes the earlier blocked/disabled release receipt below/above; it is
not a claim that historical queue backlog or unrelated incidents were repaired.

- Current live coverage: **193/193 Prometheus + 7/7 Grafana-managed rules** have
  ordered `spi_graphs`; source inventory200 rules/188 mappings/29 dashboards.
  Managed annotations were changed in place with condition/data/routing/labels,
  pause state and other annotations verified unchanged.
- Canonical Harbor CI works again. Existing robot grant `pull,push` is verified
  using Python HTTPS; an ephemeral private Docker config and an isolated BuildKit
  with `GODEBUG=tlsmlkem=0` avoid the observed hosted-runner Go TLS key-share
  compatibility failure. Certificate verification, DNS, routes, robot permissions
  and cluster pull identity are unchanged. The server TLSOption experiment was
  fully reverted, including its leftover custom resource. No new release token
  or user action is required.
- SPI publication run42622862630 succeeded. Full-service worker/frontend/broker
  and gateway use immutable SPI digest
  `sha256:eb830c98ccd6d19ef8535fcb5be003d783cb4aeb98d18fe64e66c1f51a37f64a`
  (source `b70462d`). Bot run42629684670 passed **207 PostgreSQL tests/17
  backend-specific skips** and deployed source `6e7f6d7`, digest
  `sha256:2bec0997b93f0c91bd0dfc561886a1e497058c4b39c5e37ae7842637c99a8635`.
- Renderer v5.12.10 uses actual verified Docker Hub digest
  `sha256:a7970b826f6e477461e02bf5463d92634e9ad27d525e4e50296978a2e592d2d7`;
  the chart's `image.sha` field requires raw64 characters, not a `sha256:` prefix.
  Renderer request logging is silenced and log level is error: never retain its
  ephemeral Grafana renderKey in normal URL access logs.
- Scoped native Grafana Viewer account `socrat-alert-graphs`, authenticated
  renderer, separate SPI/Telegram keys and private pull Secrets are deployed.
  Credentials are not in Git. Viewer token expires **2027-02-07T13:00:52Z**:
  renew it before that date using the native service-account token API, update
  only the existing gateway's `grafana` key and roll the isolated gateway through
  normal GitOps; no human SSO/CI identity or worker migration needs changing.
- Observed hostNetwork admission is the exact post-DNAT flannel source
  `10.42.0.0/32`, not the socket's pre-DNAT node IP. Other gateway ingress is
  namespace/pod-selected SPI only; no general CIDR admission was added.
- The original n8n workflow `J9akzlydrf9XZiID` was paused in place; current
  execution14787 finished naturally. Its validator gained only
  `GRAPH_CAPTURE_DEFERRED`. After zero logical owners/processing/active paid
  reservations, an idle off-cluster SQLite checkpoint was verified:
  236974080bytes, SHA256
  `c719d28370ab1e4c7ee8b6ea3f2840aa3992a23443b07fac226f75159438995e`.
  All766 prior job IDs/creation times and491 terminal state/stage/result/intent
  hashes survived exact-image activation. Same PVCs/state/credentials/leases,
  deadlines/budgets and historical Jobs were retained; no reset/replay occurred.
- Homelab activation `1321b97` set `bundleEnabled=true` and SPI graphs enabled.
  All three runtime roles no longer mount frozen code ConfigMaps. Only the three
  unused old code ConfigMaps were retired after proving no active pod mounts,
  saving preimages and applying UID/resourceVersion delete preconditions. No
  broad prune or Job/PVC/Secret/RBAC deletion was performed. Frozen Git snapshots
  remain rollback-only, never a second source for app development.
- The same workflow is active again; ordinary `dispatch_sent` and
  `publication_ok` resumed naturally. The native broker's next scheduled Job
  completed with the full-service image; no synthetic broker Job was launched.
- Real gateway PNG1200×600 passed strict validation and exact SHA256. Actual
  bot5-second capture + durable PostgreSQL snapshot completed in4.443s. Native
  Telegram receipts show **9 photo cards** with message IDs and persisted graph
  references from natural alerts: no real test notification was sent. Complete
  captions/inline buttons and photo edits share the native delivery path; old
  text cards remain text and conservative UTF-16 overflow preserves full text.
- A separately labelled synthetic storage-only check (no SPI job, model call,
  GitHub publication or notification) captured **both declared panels**, stored
  all4 immutable v3 objects using the actual SPI writer role, then read both
  PNGs through the deployed Reader's read-only IAM/manifest/hash-validation
  domain path. Model context was byte-equivalent before/after graph attachment.
  GitHub markdown uses protected Reader links; anonymous public PNG access401.
  This is **not** an owner-browser SSO login test: that positive browser check
  could not be repeated because the shared browser profile was already in use.
  Existing Authentik group/cookie authorization was not changed or bypassed.
- Harbor, Grafana, SPI and bot are **Synced/Healthy**; all4 nodes Ready. Gateway
  and both consumer flags are enabled in deployment overrides (chart defaults
  remain off). Private backup/credential/acceptance artifacts remain outside Git.

## Stateless gateway deadline and release target

Telegram rendering has a **15-second gateway budget**, within the renderer's
existing 20-second maximum. The old 4-second budget rejected genuine pod-panel
PNGs that completed in about 4.5 seconds, making bounded bot retries ineffective.
The bot must use a longer transport deadline (20 seconds) and a lease with enough
headroom for transport plus bounded database completion. SPI's 20-second render
budget, private credentials, reserved capacity and policy are unchanged.

The existing full-image publisher accepts an explicit `workflow_dispatch`
`deploy_target=alert-graphs`. This builds/tests/publishes the same complete SPI
image and invokes `deploy-to-socrat@v1` **in that publisher job**, with
`app=grafana, component=gateway`. It updates the canonical gateway image block
in `deploy/grafana/values.yaml` and its chart mirror; the existing Grafana Argo
Application owns the stateless gateway. No worker, SQLite/PVC, n8n owner, lease or
quota is replaced/reset by this target. It is not a second source tree or image.
The ordinary/default SPI target still requires an actual natural stateful drain.

For a gateway-only source change, use a deliberately reviewed `[skip ci]` main
commit to avoid an automatic stateful release, then dispatch this existing CI
workflow with the explicit stateless target. Its full native constrained tests
and protected publication environment still apply. Do not manually override live
images, invoke a stateful target as a shortcut, or bypass environment approval.
