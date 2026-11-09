# Workflow failure policy: offline generation and Quality Graph

## Developer/agent loop

Store new workflow JSON templates under the recursive roots declared in
`n8n-workflow-policy.json` (homelab: `docs/n8n`; SPI: `integrations/n8n`). A file
list does not need updating for each new template. JSON object and array exports
are supported; non-workflow JSON is not mistaken for a template.

```sh
uv sync --locked
uv run python scripts/n8n/check_failure_alerts.py
# If the check reports a missing binding:
uv run python scripts/n8n/check_failure_alerts.py --fix
uv run python scripts/n8n/check_failure_alerts.py
uv run qg validate
```

The checker is standard-library-only, so `python3 scripts/n8n/check_failure_alerts.py`
also works without installing Quality Graph. The explicit `--fix` changes local
JSON only, preserving graph/credentials/other settings semantically. It adds the
existing handler reference; it does NOT generate a new live alert/workflow.
Commit the repaired JSON and use the existing explicit native workflow sync to
apply it. There is no scheduled reconciler, live API call, credentials, automatic
commit, activation or production test in this checker or its CI jobs.

A different explicit handler, handler recursion, invalid/duplicate-key JSON,
invalid configuration/path or malformed workflow makes the check fail. `--fix`
refuses all writes when such a conflict exists. It is idempotent. The canonical
handler is exempt ONLY at its explicitly configured template path, with expected
name and Error Trigger type; a copied name/trigger elsewhere is not an exemption.
The instance's handler ID is a public reference in the policy, not an auth token.
Changing an existing target requires an explicit reviewed edit, never `--force`.

Workflows created only in the live UI are intentionally outside this mechanism.
Export them into the declared roots before their normal code review/sync. If you
add a new source root, declare it in the policy; a misconfigured empty discovery
cannot silently pass. CI does not prove that the referenced handler exists live.

## Quality Graph owns the new quality jobs

Source of truth: **`qg.yaml`**, not hand-edited `.github/workflows` outputs.
Follow <https://quality-graph.xyz/quickstart/> and
<https://quality-graph.xyz/migration/>. The CLI/provider are both locked to0.1.8;
runtime Action is pinned to the quickstart's reviewed40-character release SHA.
Setup and upload Actions are pinned as well. `uv.lock` pins dependencies.

Nodes check graph freshness, workflow policy and offline fixer regressions. SPI
also declares the complete existing service/native-image check for parity.
Every node runs on PR and main-push with read-only execution permissions and no
production secrets. CI runs check mode only: a missing target fails with the file
and the `--fix` command. The developer/agent explicitly invokes the fixer.

```sh
# After changing qg.yaml:
uv run qg generate
uv run qg validate
# Commit declaration AND outputs, never edit generated YAML directly:
git add qg.yaml .github/workflows .quality-graph .prettierignore
```

This is a bootstrap: neither repository had qg.yaml at implementation time.
Per the migration guide, old checks are retained until a representative PR proves
parity. Privileged deployment/registry jobs remain outside the secretless graph.
Do not delete old checks or change branch-protection requirements merely because
local validation passes. The trusted publisher reads the default-branch graph:
the bootstrap PR may lack aggregate/dashboard acceptance before merge. After
reviewed bootstrap merge, use a non-production probe PR to verify the trusted
publisher; only then switch required checks/remove duplicate legacy checks.
Generating/validating does not administer GitHub branch protection or deployments.
