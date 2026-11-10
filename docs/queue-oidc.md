# Standalone SPI operator frontend: native OIDC

## Status — implementation prepared, not activated

Canonical URL: `https://spi.socrat.alchemmist.xyz/` (the UI is served directly at root).
Legacy `/queue/` bookmarks redirect to `/`; the callback returns to `/`.
This is the existing frontend Deployment in the existing SPI Application and
shared image, not a second worker, database, Argo app or CI publisher. The old
RustFS URL is not the intended service identity. No DNS record/router change is
part of this work; the operator reports an existing wildcard.

The application code implements a confidential Authorization Code + PKCE S256
client with native Authentik MFA and explicit consent. Redirect URI is exactly
`https://spi.socrat.alchemmist.xyz/oauth/callback`, issuer exactly
`https://auth.socrat.alchemmist.xyz/application/o/spi-triage/`. Discovery cannot
choose another origin or token/JWKS endpoint. Pinned PyJWT/cryptography in the
existing image verify RS256, key ID, issuer/audience/azp, exp/iat, nonce and UUID
subject. There is no email linking, local password or trusted proxy-group header.

## Authorization and session boundary

Signed groups admit existing `homelab-owner` / `homelab-platform-operator`, or
reviewed app-specific `app-spi-triage-operator`. No existing person's membership,
password or MFA changes. The app-specific group is non-superuser, has no parent
or RBAC role, and starts empty. Observer is not implicitly admitted.

The only capability remains listing the queue and asking the existing guarded
backend to cancel eligible pending work. Active/Wait/publication fences, expected
revision checks, budgets and job history remain enforced by the worker. Frontend
OIDC credentials are not mounted into the worker/broker, and browser cookies or
OIDC credentials never go to the worker. Machine APIs/webhooks retain their own
credentials and never get a browser login redirect.

Only opaque random session IDs enter host-only `__Host-` Secure/HttpOnly/Lax
cookies; code verifier, tokens and client secret remain server-side. State is
bound to the initiating cookie, nonce is independently checked, callbacks are
single-use even after ambiguous token-exchange failure. POST requires canonical
Origin and non-simple CSRF header. Duplicate security cookies/state, wrong Host,
forged caller identity, invalid signatures/claims/role are denied.

Sessions are bounded in memory (128), last no longer than the verified ID token
or 300 seconds, and are invalidated on UI restart. No refresh token is requested
or stored; expiry requires a fresh native OIDC roundtrip, normally using the
existing IdP MFA session. Background API calls return JSON401 rather than
cross-origin HTML redirects. Sign out revokes only the local SPI session; it does
not claim global Authentik logout. Group removal is not immediate local logout:
the maximum retained session is five minutes. Callback/query/credential logging
is disabled. Do not claim seamless eight-hour refresh in this version.

## Deployment contract

Homelab owns the exact native OIDC catalog, Secret reference, per-app Ingress,
Certificate request and NetworkPolicy. Dedicated plan/apply tooling must not
replay all Authentik foundation or mutate unrelated providers, users or outposts.
Shared signing and groups mappings are referenced, not regenerated. Existing
provider/client without its private journal is a STOP, never a secret rotation.

The frontend requires `/oidc-client/credentials.json` from the frontend-only
`automation/spi-queue-oidc` Secret (root:fsGroup0440). Only `client_id` and
`client_secret` are accepted. Credentials are never committed. Startup fails
closed without this Secret; chart defaults do not accidentally enable OIDC for
the frozen legacy rollback. Old proxy-mode application sessions are not adopted.

Publish only through the existing CI job and `deploy-to-socrat@v1` with
`app: spi-triage` and build digest. No custom Git/image deployment path. Because
that shared-image release also replaces the singleton worker, retain the native
SPI-only natural-drain/production-approval gate. Do not approve merely because
frontend tests passed, force a lease/reset, replay historical jobs or kill paid
work. Keep the same database/PVC/counters/credentials and original workflow.

## Acceptance / remaining gates

The built linux/amd64 test image passed341 tests, zero skips, offline/read-only
under1CPU/384MiB. This includes the actual HTTP handler → native code callback →
RSA-verified session → protected HTML → logout, plus the negative JWT/PKCE/state/
metadata/CSRF and unchanged backend-transport tests. This is local protocol proof,
not a real Authentik human/MFA acceptance.

At this checkpoint no production provider, Secret, ingress or image was changed.
Live native Authentik GET-only plan succeeds with the existing automation identity.
Operator cluster access is presently blocked: workstation kubeconfig points to
unreachable192.168.0.23:6443 and SSH lacks the saved trusted node host key. No host
checking/TLS check, route/VPN or DNS setting was weakened to bypass that problem.
Restore the existing trusted operator access before runtime secret provisioning,
fresh IdP backup, safe worker drain and protected image release. After deployment,
verify ordinary DNS/TLS, exact IdP redirect, real personal MFA/callback, scoped
read-only queue listing, outsider rejection, logout and old-path retirement.
Do not cancel a production job or send notifications as an acceptance probe.


## Queue browsing and selection

The canonical UI remains `/`. Click Alert/job, State/stage, Timing/Created or
Progress/history to toggle ascending/descending server-side order across the
whole result, not just visible rows. Default is **Created ascending**, then job
ID for ties. State order is queued/retry/processing/dead/complete/cancelled/dry-run,
then numeric stage; progress uses saved match batches, then claims and failures.
Deadline/Available remain secondary timing lines. Full job IDs are always visible.
This is not a scheduler change: ready advanced stages, active Wait owners and
retry availability mean actual execution is not strictly global FIFO.

First 50 rows appear immediately; bounded sequential keyset pages append in the
background and near the scroll boundary. No Previous/Next UI. Creation cursors
avoid offset skips after earlier rows leave the queue. Changing mutable state/
progress while browsing can move rows; refresh obtains a new live view, not a
transactional snapshot. IDs are deduplicated. Auto-refresh pauses below the top
and during selection; failed loads stop until explicit Retry/Refresh.

Drag on **empty row space** to enter selection mode. Text dragging, links and
buttons keep native behavior. Selection uses subtle row backgrounds, no per-row
checkboxes. Once selected, click rows to toggle them; further drags add ranges
with edge auto-scroll. Select all includes future pages of the current loading
pass and waits until loading completes before permitting batch cancellation.
Arrow keys move row focus, Space/Enter toggle, Escape/Clear selection exits.

Cancel selected confirms a **frozen list of exact job IDs and updated versions**,
then calls the existing guarded single-item endpoint sequentially. New arrivals
are never swept in. Active/Wait/owned/publication/terminal jobs stay protected.
Changed rows are skipped; auth/network/uncertain responses stop the batch, report
confirmed/skipped/unconfirmed counts, and are never automatically replayed.
Each successful cancellation has the existing transactional audit/tombstone;
no history/evidence deletion, quota reset, retry or publication capability added.
Temporary-SQLite browser fixture: 127 rows loaded, real mouse drag/click selection,
126 pending cancellations with one active Wait retained. Not a production purge.

Selection actions use a fixed floating bottom toolbar, outside document flow.
Showing/clearing selection does not move the table or change document height.
Constant bottom clearance lets the final rows scroll above it; while dragging,
the overlay passes pointer hit-testing through to the underlying rows.
