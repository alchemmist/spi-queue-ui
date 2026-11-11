# SPI native RPC Service identity (18606 DNS correction)

18606 failed before connecting: EAI_AGAIN resolving spi-triage.automation.svc.
Bounded read-only probes from the actual n8n Pod reproduced5–10s libc lookups and
intermittent direct CoreDNS timeouts. This is not model JSON truncation or a
worker restart. The broader DNS/network root cause is not claimed resolved.

The five native SPI POST nodes now use the EXISTING Kubernetes ClusterIP
10.43.194.185:8080, never a Pod/public IP. The Git-owned Helm Service pins its
already allocated VIP. integrations/n8n/worker-service.json binds namespace,
name, selector, port and existing Service UID; service_endpoint.py validates it
before native enrollment. Service identity drift fails closed for operator review.
No periodically running reconciler or hidden DNS override is introduced.

This removes DNS from n8n→SPI claim/stage RPC while retaining the existing
httpHeaderAuth credential, namespace policies, payloads, timeout, durable-v1
identity, ownership fences, native graph and finite budgets. No retryOnFail,
terminal retry, fallback POST or additional paid inference is introduced. Existing
plain HTTP inside the cluster is unchanged; no TLS/SSO check is disabled.

Enrollment changes ONLY the five URL parameters through the authenticated native
workflow API. It does not execute a workflow or replace n8n/SPI Pods. Persisted
execution workflowData must remain byte-for-byte equivalent. The inactive future
workflow definition may be updated while an old execution retains its immutable
snapshot; this is not worker rollout/drain proof. Native API version IDs can advance
for the configuration revision, while graph IDs, credentials, settings, pin/static
data and unrelated workflows remain unchanged. Re-activate only the originally
active workflow. No occupied worker is replaced. Any later IMAGE rollout still
requires fresh independent natural logical/native/processing/paid drain.

Render comparison must show ONLY Service/spi-triage.spec.clusterIP declaration,
with every Deployment/Pod template identical; actual Service UID/VIP and Pod UIDs
must remain unchanged. /healthz GET is an unpaid connectivity read, not synthetic
incident/full-publication acceptance. Historical18606 is not replayed or rewritten.
