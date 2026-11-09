# SPI triage service

The complete SPI incident-triage application: **durable backend + English
operator frontend**, one repository, one versioned image and one Argo CD owner.
This is not a frontend-only wrapper or a submodule pointing back to homelab.
Public source does **not** make incident data, queue APIs or evidence public.

## Application code

* `backend/`: durable SQLite queue and checkpoints; owner/fence/deadline handling;
  Match/Collect/Analyze/Publish; model/token admission and offline tokenizer;
  read-only evidence collection; immutable private S3 publication; GitHub intent
  reconciliation and scoped broker; Telegram receipt/link delivery; queue admin.
* `frontend/`: native HTML5, monospace CSS, plain JS and a standard-library BFF.
  Labels, errors, confirmations and dates are English. Original alert data is not
  rewritten or model-translated. Existing owner/platform-operator SSO only.
* `tests/backend/`, `tests/test_*.py`: complete extracted backend suite plus UI
  and authentication/language regressions; temporary local databases and mocks.
* `integrations/n8n/`: existing authenticated webhook/workflow contracts and
  custom nodes. n8n is an external orchestrator, not a second queue or database.
* `integrations/evidence/`: reader/download contract snapshots for offline tests;
  the existing RustFS/SSO infrastructure remains external and is not redeployed.
* `build/`: immutable upstream tokenizer/wheel pins and license provenance.
  Downloads happen at **image build time only**; no model weights or runtime
  downloads. Broker dependencies are wheel/hash locked too.

## One image, isolated runtime roles

The release image contains `/app` (backend), `/ui` (frontend), the pinned native
counter and broker dependencies. Existing isolated roles select an entrypoint:

| Role | Command | State / credentials |
| --- | --- | --- |
| Worker/API | `python -B /app/service.py` | Existing `/data/spi.sqlite` PVC; existing worker/evidence credentials |
| Web | `python -B /ui/queue_web.py` | No PVC, worker/model/GitHub/S3 secrets or service-account token; dedicated queue-control token only |
| Token broker | `python -B /app/spi/github_broker.py` | Existing scoped App identity, one-hour repository token and exact Secret-patch RBAC |

These are roles of **one service/release**, not independent frontend/backend
repositories or unrelated image versions. Keeping existing Kubernetes identities
and credential isolation avoids creating a second consumer or exposing privileged
backend credentials in the browser/web container.

## Preserved behavior

Existing jobs, immutable evidence, checkpoints, delivery receipts and finite
failure charges are retained. Cancellation is an audited expected-version CAS
for eligible pending jobs only; no active/Wait/terminal resurrection, force retry,
physical deletion or issue closure. Browser cookies go only to the fixed trusted
TLS auth endpoint, never the worker API. Caller identity headers cannot authorize.
Existing n8n workflow/node/credential IDs and same-execution Wait remain unchanged.
Do not replace the queue with Redis/Postgres or recreate its PVC during packaging.
Actual model quota remains30000TPM/3RPM/parallel1; no inference probes in tests.

## Checks and build

```sh
python3 scripts/test_all.py
node --check frontend/app.js
docker build --platform linux/amd64 --target test -t spi-triage:test .
docker run --rm --network none --read-only --cpus 1 --memory 384m \
  --memory-swap 384m --pids-limit 256 --tmpfs /tmp:rw,size=64m \
  -e SPI_REQUIRE_NATIVE_TESTS=true spi-triage:test
docker build --platform linux/amd64 --target release -t spi-triage:local .
```

Host-only discovery may skip the three optional native-runtime tests. The image
run requires **zero skips**, verifies the pinned native assets/library and has no
external network or production credentials. Production image has no Node/test
runner, no automatic credentials, and a non-root default user. Kubernetes keeps
existing role-specific uid/security contexts and resource limits.

## Single Socrat release

`.github/workflows/ci.yml`: backend + frontend tests and native constrained image
checks, then one protected `publish-and-deploy` job:

**full commit SHA → Harbor `homelab/spi-triage` → build output digest →
`alchemmist/deploy-to-socrat@v1` (`app: spi-triage`) → homelab image-only commit →
existing Argo `spi-triage`.** No frontend-specific publisher/Application.

Cluster chart, public configuration and secret *references* stay in homelab:
`charts/spi-triage`, `deploy/spi-triage/values.yaml`,
`deploy/argocd/apps/spi-triage.yaml`. No Argo token, direct sync, Helm upgrade or
Kubernetes image override is used by CI. CI credentials use existing scoped
Harbor robot secrets and the documented release deploy key, never human SSH keys.

### Stateful release gate

Production environment `spi-triage-production` requires owner approval. Before
approving a backend replacement, prevent new SPI claims using the existing native
workflow control, let the current execution/Wait and paid calls finish naturally,
and verify zero logical owners/processing/inflight/paid calls. Intake remains
durable and unrelated automations stay active. Approval alone is not evidence
of idle state. After exact-digest Argo reconciliation, restore the same workflow.
No forced termination, lease/budget reset or terminal retry is authorized.
This gate protects in-progress work; it does **not** reinstate a backup requirement.
The owner explicitly said backups are not a prerequisite for this extraction.

### Migration state and activation

The source extraction is complete, but image/deployment acceptance is separate.
`PUBLISH_ENABLED=false` holds publication after the graph release attempt:
hosted-runner Docker login still times out during the Harbor TLS/token handshake.
Private SSH/Kubernetes access now works and all four nodes were Ready. The graph
image was approved for publication **to standby only** after proving all20 Helm
resources identical with `bundleEnabled=false`; authentication failed before
publication, and the publication flag was restored to false. See
`docs/alert-graphs.md` for the receipt. No backend activation occurred.
Do not claim a live migration from source/CI success alone.

Homelab `bundleEnabled=false` is a transitional compatibility gate: its render
must match every existing worker/frontend/broker/PVC/RBAC resource. Existing
ConfigMap mounts stay active until a real full-service image is built and a safe
activation window is available. They are temporary frozen legacy snapshots, NOT
a second development source. Never activate without the pinned digest and pull
Secret available in both `automation` and `pr-agent`.

At activation set `bundleEnabled=true`, disable no queue API, reuse the same
worker/PVC/Service/CronJob/SSO identities, and remove legacy ConfigMap mounts so
`/app` and `/ui` come from the same release image. Remove legacy code mirrors only
after image-backed acceptance; retain historical private artifacts. No second
backend Deployment, new database or paid consumer is created.

The mistakenly bootstrapped `spi-queue-ui` Application was empty/disabled; its
source is retired, not adopted as a separate service. The former repository URL
redirects to this renamed full-service repository; release secrets/history stay
with the same GitHub repository identity.
