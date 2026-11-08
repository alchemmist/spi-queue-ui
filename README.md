# SPI queue UI

Small private operator UI for an existing SPI triage backend: native HTML5,
monospace CSS, plain JavaScript and a Python standard-library BFF. Public source
code does **not** make queue access public. There is no database, model runtime,
worker or incident fixture in this repository.

## Safety boundary

* Browser cookies are verified against the fixed trusted HTTPS Authentik endpoint.
  Caller identity headers cannot grant access. Existing owner/platform-operator
  groups only; TLS verification stays enabled and auth errors fail closed.
* Only two fixed backend operations: list and cancellation. Backend management
  token is read from `/queue-control/token`, never sent to browser or logs.
* Cancellation preserves history. Backend CAS rejects stale rows, active/Wait,
  terminal jobs and jobs with publication intent/receipts. No force retry/reset,
  physical deletion, evidence deletion or workflow execution controls.
* Same-origin JSON POST, custom CSRF header, bounded request/response sizes,
  concurrency and deadlines; no external assets/frameworks or runtime downloads.
* Default cluster binding: `https://rustfs.socrat.alchemmist.xyz/queue/`, backend
  `spi-triage.automation.svc.cluster.local:8080`. These are configuration, not
  credentials. Deployment must preserve this trust boundary.

## Local checks

```sh
python3 -m unittest discover -s tests -v
node --check app/app.js
docker build -t spi-queue-ui:test .
```

Tests use local HTTP mocks only; never production cancellation or inference.
Image runs uid/gid10001. In Kubernetes use read-only root, no capabilities,
no service-account token, dedicated read-only token Secret and narrow networking.

## Socrat release contract and current state

CI runs tests and builds an image without deployment credentials on PR/main.
The release draft is `.github/release-workflow.yml.example`, **not enabled**.
The required sequence from homelab wiki is:

1. Separate `charts/spi-queue-ui`, `deploy/spi-queue-ui/values.yaml` and
   `deploy/argocd/apps/spi-queue-ui.yaml` in `alchemmist/homelab`.
2. Application in project `default`, main, explicit `parameters: []`, auto-sync
   with selfHeal and prune false. No AppProject/ApplicationSet or CI cluster token.
3. Scoped Harbor push credentials, namespace pull Secret and production gate.
4. Same job: tests/build -> Harbor full commit SHA tag -> digest from build output
   -> `alchemmist/deploy-to-socrat@v1` -> image-only homelab commit -> Argo.
5. Verify robot commit, exact image digest, Argo and actual authenticated browser.

Required CI secrets are `HARBOR_USERNAME`, `HARBOR_PASSWORD`,
`HOMELAB_DEPLOY_KEY`; no real credential is part of source or history.
Never use `latest`, CI kubectl/helm upgrades, Argo API sync or image overrides.
The SPI worker, encrypted n8n/PVC and existing workflows remain separately owned.

## Deployment ownership

UI-only code/image releases do not require a SQLite backup: the UI has no PVC.
The owner explicitly approved this independent release policy. This does not
allow destructive backend/data changes. The SPI backend queue API and dedicated
Secret remain enabled; only frontend resource ownership moves to this app.
SSO, TLS and cancellation safety remain mandatory. Do not concurrently manage
frontend resources from two Argo Applications.
