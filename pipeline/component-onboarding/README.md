# Component Onboarding Sync Pipeline

Fetches component onboarding issues from Jira (`RHOAIENG`, label `component-onboarding`) and pushes them to the org-pulse API every 6 hours.

## How it works

1. Queries Jira for all `RHOAIENG` issues with the `component-onboarding` label (changelog included for validation date extraction).
2. For each issue:
   - Downloads the `componentonboardingdetails.yaml` attachment (graceful skip if absent) and reads YAML fields (both `snake_case` and `camelCase` keys are accepted; `inputs:` top-level key is unwrapped automatically).
   - Reads all issue links to collect linked RHAISTRAT/RHAIRFE feature keys (all link types, both directions).
   - Derives `validationDate` from the changelog — the earliest timestamp the `validation-successful` label was added. This is the start of the onboarding clock shown in the dashboard's Days column.
   - Derives `productContext` (RHOAI / ODH) from the YAML field, template clone link, summary prefix, or label hints — in that priority order.
   - Maps current Jira labels to `onboardingSteps` booleans.
3. DELETEs existing data from org-pulse, then bulk-upserts all records in chunks of 500.

## Prerequisites

- Python 3.11+
- A Jira Cloud API token (from https://id.atlassian.com/manage-profile/security/api-tokens)
- An org-pulse API token (`tt_...` prefix, created via **Settings → API Tokens** in the org-pulse UI)

## GitLab CI/CD Variables

Configure these in **Settings → CI/CD → Variables**. Mark `JIRA_TOKEN` and `ORG_PULSE_API_TOKEN` as **Masked**.

| Variable | Description |
|---|---|
| `ORG_PULSE_BACKEND_URL` | Base URL of the org-pulse backend, e.g. `https://org-pulse.example.com` |
| `ORG_PULSE_API_TOKEN` | Bearer token (`tt_...`) for the org-pulse API |
| `JIRA_EMAIL` | Red Hat email address used for Jira authentication |
| `JIRA_TOKEN` | Jira Cloud API token |

## Cron schedule

Configure the schedule in **CI/CD → Schedules** in GitLab:

| Cron | Description |
|---|---|
| `0 */6 * * *` | Every 6 hours |

The pipeline only runs on scheduled triggers and manual web triggers (not on every push).

## Manual run

```bash
cd pipeline/component-onboarding
pip install -r requirements.txt

JIRA_EMAIL=you@redhat.com \
JIRA_TOKEN=your-jira-api-token \
ORG_PULSE_BACKEND_URL=http://localhost:3001 \
ORG_PULSE_API_TOKEN=tt_your_token \
python fetch_and_push.py
```

The script prints a per-issue summary and a totals table, and exits non-zero if any records failed to upsert.

## Label → onboarding step mapping

Steps are in pipeline execution order. Multiple labels can map to the same step (e.g. both `-mr-raised` and `-mr-merged` variants); any match marks the step as done.

| Step | Jira Labels | Step field | Product |
|------|-------------|------------|---------|
| 1 | `yaml-attached`, `validation-successful` | `yamlValidated` | Both |
| 2 | `quay-mr-raised`, `quay-mr-merged`, `quay-repo-created` | `quayRepoCreated` | Both |
| 3 | `delivery-repo-mr-raised`, `delivery-repo-created` | `deliveryRepoProvisioned` | RHOAI |
| 4 | `konflux-mr-raised`, `konflux-mr-merged`, `krd-mr-merged` | `konfluxOnboarded` | Both |
| 5 | `rkc-pr-raised`, `tekton-pr-raised`, `tekton-pr-merged` | `pushPipelineConfigured` | Both |
| 5b | `rkc-pull-changes-done` | `pullPipelineConfigured` | RHOAI |
| 6 | `okc-pr-raised`, `okc-pr-merged`, `okc-changes-done` | `odhKonfluxOnboarded` | ODH / cross-product |
| 7 | `operator-pr-raised`, `operator-pr-merged` | `operatorIntegrated` | If operator |
| 8 | `bundle-changes-done`, `obc-changes-done` | `bundleConfigured` | Both |
| 9 | `product-listing-created` | `productListingUpdated` | RHOAI |
| 10 | `auto-merge-setup-done` | `autoMergeSetup` | RHOAI |
| 11 | `renovate-changes-done`, `renovate-sync-done`, `renovate-sync-triggered` | `renovateSetup` | RHOAI |

## Onboarding clock (Days column)

`validationDate` is extracted from the Jira changelog as the earliest timestamp the `validation-successful` label was added. The dashboard uses this as the start of the onboarding clock:

- **Days (completed):** `resolved − validationDate`
- **Days (in-progress):** `now − validationDate`

Falls back to `created` if the label was never added (e.g. very old issues).

## YAML attachment schema

The pipeline reads `componentonboardingdetails.yaml` attached to each Jira ticket. Both `snake_case` and `camelCase` field names are accepted. If the file has a top-level `inputs:` key, its contents are unwrapped automatically.

| Field | Aliases | Description |
|---|---|---|
| `product_context` | `productContext` | `RHOAI` or `ODH` |
| `component_name` | `componentName` | Slug of the component |
| `repo_url` | `repoUrl` | GitHub repository URL |
| `repo_branch` | `branch` | Branch to build from |
| `dockerfile_path` | `dockerfilePath` | Path to the Dockerfile |
| `context_path` | `contextPath` | Docker build context path |
| `is_operator` | `isOperator` | `true` if this is an operator |
