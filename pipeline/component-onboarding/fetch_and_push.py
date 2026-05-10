"""Fetch component onboarding issues from Jira and push to org-pulse API."""

import json
import os
import re
import sys
from datetime import datetime, timezone

import yaml
import requests

from jira_client import JiraClient

JIRA_BASE_URL = "https://redhat.atlassian.net"

# Only RHOAIENG; filter by label only (no ScriptRunner issueFunction dependency).
JQL = 'project = RHOAIENG AND labels = "component-onboarding" ORDER BY created DESC'

# changelog expand is needed to extract validationDate.
JIRA_FIELDS = [
    "summary", "status", "labels", "issuelinks",
    "created", "resolutiondate", "attachment",
]
JIRA_EXPAND = ["changelog"]

ATTACHMENT_FILENAME = "componentonboardingdetails.yaml"
CHUNK_SIZE = 500

# Maps real Jira label conventions → onboarding step keys (pipeline order).
# Multiple labels can map to the same step; any matching label marks it done.
LABEL_TO_STEP: dict[str, str] = {
    # Step 1 — YAML Validated (both)
    "yaml-attached":                   "yamlValidated",
    "validation-successful":           "yamlValidated",
    # Step 2 — Quay Repo (both)
    "quay-mr-raised":                  "quayRepoCreated",
    "quay-mr-merged":                  "quayRepoCreated",
    "quay-repo-created":               "quayRepoCreated",
    # Step 3 — Delivery Repo (RHOAI only)
    "delivery-repo-mr-raised":         "deliveryRepoProvisioned",
    "delivery-repo-created":           "deliveryRepoProvisioned",
    # Step 4 — Konflux Release Data / KRD (both)
    "konflux-mr-raised":               "konfluxOnboarded",
    "konflux-mr-merged":               "konfluxOnboarded",
    "krd-mr-merged":                   "konfluxOnboarded",
    # Step 5 — Push Pipelines (rkc-* RHOAI, tekton-* ODH)
    "rkc-pr-raised":                   "pushPipelineConfigured",
    "tekton-pr-raised":                "pushPipelineConfigured",
    "tekton-pr-merged":                "pushPipelineConfigured",
    # Step 5b — Pull Pipelines (RHOAI only)
    "rkc-pull-changes-done":           "pullPipelineConfigured",
    # Step 6 — ODH Konflux Central (ODH + cross-product RHOAI components)
    "okc-pr-raised":                   "odhKonfluxOnboarded",
    "okc-pr-merged":                   "odhKonfluxOnboarded",
    "okc-changes-done":                "odhKonfluxOnboarded",
    # Step 7 — Operator Integration (if operator)
    "operator-pr-raised":              "operatorIntegrated",
    "operator-pr-merged":              "operatorIntegrated",
    # Step 8 — Bundle (both)
    "bundle-changes-done":             "bundleConfigured",
    "obc-changes-done":                "bundleConfigured",
    # Step 9 — Product Listing (RHOAI only)
    "product-listing-created":         "productListingUpdated",
    # Step 10 — Auto Merge (RHOAI only)
    "auto-merge-setup-done":           "autoMergeSetup",
    # Step 11 — Renovate (RHOAI only)
    "renovate-changes-done":           "renovateSetup",
    "renovate-sync-done":              "renovateSetup",
    "renovate-sync-triggered":         "renovateSetup",
}

STEP_KEYS: list[str] = list(dict.fromkeys(LABEL_TO_STEP.values()))

RESOLVED_STATUSES = {"Resolved", "Closed", "Done", "Cancelled"}

# RHOAI/ODH template ticket keys (used to detect product context from cloner links).
RHOAI_TEMPLATE = "RHOAIENG-17225"
ODH_TEMPLATE   = "RHOAIENG-35683"


def env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: Required environment variable {name!r} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def derive_product_context(issue: dict, yaml_data: dict) -> str:
    """Determine RHOAI or ODH in priority order:
    1. Explicit field in the attached YAML
    2. Template ticket the issue was cloned from (RHOAIENG-17225 → RHOAI, RHOAIENG-35683 → ODH)
    3. Summary prefix ("RHOAI ..." vs "ODH ...")
    4. Label prefix hints
    5. Default to RHOAI
    """
    if yaml_data.get("product_context") in ("RHOAI", "ODH"):
        return yaml_data["product_context"]
    if yaml_data.get("productContext") in ("RHOAI", "ODH"):
        return yaml_data["productContext"]

    links = issue.get("fields", {}).get("issuelinks", [])
    for link in links:
        for direction in ("inwardIssue", "outwardIssue"):
            key = (link.get(direction) or {}).get("key", "")
            if key == RHOAI_TEMPLATE:
                return "RHOAI"
            if key == ODH_TEMPLATE:
                return "ODH"

    summary = issue.get("fields", {}).get("summary", "")
    if re.match(r"^RHOAI\s", summary, re.IGNORECASE):
        return "RHOAI"
    if re.match(r"^ODH\s", summary, re.IGNORECASE):
        return "ODH"

    labels = [lb.lower() for lb in (issue.get("fields", {}).get("labels") or [])]
    if any(lb.startswith("rhoai") for lb in labels):
        return "RHOAI"
    if any(lb.startswith("odh") for lb in labels):
        return "ODH"

    return "RHOAI"


def derive_component_name(issue: dict, yaml_data: dict) -> str:
    """Prefer the YAML field; fall back to extracting from the summary."""
    for field in ("component_name", "componentName"):
        val = yaml_data.get(field, "")
        if val:
            return str(val)

    summary = issue.get("fields", {}).get("summary", "")

    # Bracket format at the end: "... [Component Name]"
    m = re.search(r"\[([^\]]+)\]\s*$", summary)
    if m:
        return m.group(1).strip()

    # Suffix slug after "Onboarding": odh-something, kube-rbac-proxy, etc.
    m = re.search(r"[Oo]nboarding\s+((?:odh-|rkc-|kube-)[a-z0-9-]+|[a-z][a-z0-9-]+)$", summary)
    if m:
        return m.group(1).strip()

    return ""


def build_component(
    issue: dict,
    yaml_data: dict,
    linked_features: list[str],
    feature_titles: dict[str, str],
    validation_date: str | None,
    synced_at: str,
) -> dict:
    fields      = issue.get("fields", {})
    status_name = fields.get("status", {}).get("name", "Unknown")
    labels      = [lb for lb in (fields.get("labels") or []) if isinstance(lb, str)]

    onboarding_steps = {step: False for step in STEP_KEYS}
    for label in labels:
        step = LABEL_TO_STEP.get(label)
        if step:
            onboarding_steps[step] = True

    completion_status = (
        "completed" if status_name in RESOLVED_STATUSES or "component-onboarding-completed" in labels
        else "in-progress"
    )

    component: dict = {
        "key":              issue["key"],
        "summary":          fields.get("summary", ""),
        "status":           status_name,
        "completionStatus": completion_status,
        "productContext":   derive_product_context(issue, yaml_data),
        "componentName":    derive_component_name(issue, yaml_data),
        "syncedAt":         synced_at,
        "labels":           labels,
        "onboardingSteps":  onboarding_steps,
        "linkedFeatures":   linked_features,
        "featureTitles":    feature_titles,
        "created":          fields.get("created"),
        "resolved":         fields.get("resolutiondate"),
        "validationDate":   validation_date,
    }

    # Optional YAML fields
    for src, dst in (
        ("repo_url",        "repoUrl"),
        ("repoUrl",         "repoUrl"),
        ("repo_branch",     "branch"),
        ("branch",          "branch"),
        ("dockerfile_path", "dockerfilePath"),
        ("dockerfilePath",  "dockerfilePath"),
        ("context_path",    "contextPath"),
        ("contextPath",     "contextPath"),
    ):
        val = yaml_data.get(src)
        if val is not None and dst not in component:
            component[dst] = str(val)

    # is_operator / isOperator
    for key in ("is_operator", "isOperator"):
        if key in yaml_data:
            component["isOperator"] = bool(yaml_data[key])
            break

    return component


def push_to_api(backend_url: str, token: str, components: list[dict]) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    base   = backend_url.rstrip("/")
    totals = {"created": 0, "updated": 0, "unchanged": 0, "errors": 0}

    for i in range(0, len(components), CHUNK_SIZE):
        chunk = components[i : i + CHUNK_SIZE]
        resp  = requests.post(
            f"{base}/api/modules/ai-impact/component-onboarding/bulk",
            headers=headers,
            json={"components": chunk},
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        for key in totals:
            totals[key] += result.get(key, 0)
        print(f"  Chunk {i // CHUNK_SIZE + 1}: {result}")

    return totals


def clear_existing(backend_url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(
        f"{backend_url.rstrip('/')}/api/modules/ai-impact/component-onboarding",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"  Cleared existing data: {resp.status_code}")


def main() -> None:
    jira_email  = env("JIRA_EMAIL")
    jira_token  = env("JIRA_TOKEN")
    backend_url = env("ORG_PULSE_BACKEND_URL")
    api_token   = env("ORG_PULSE_API_TOKEN")

    synced_at = datetime.now(timezone.utc).isoformat()

    print("=== Component Onboarding Sync ===")
    print(f"Synced at : {synced_at}")
    print(f"JQL       : {JQL}\n")

    jira = JiraClient(JIRA_BASE_URL, jira_email, jira_token)

    print("[1/4] Fetching issues from Jira (with changelog)…")
    issues = jira.search_jql(JQL, JIRA_FIELDS, expand=JIRA_EXPAND)
    print(f"  Found {len(issues)} issues")

    print("\n[2/4] Processing issues…")
    components = []
    for idx, issue in enumerate(issues, 1):
        key = issue["key"]
        print(f"  [{idx}/{len(issues)}] {key}", end="", flush=True)

        yaml_content = jira.get_attachment_content(key, ATTACHMENT_FILENAME)
        if yaml_content:
            try:
                yaml_data = yaml.safe_load(yaml_content) or {}
                # Normalise: the YAML uses an "inputs:" top-level key
                if "inputs" in yaml_data and isinstance(yaml_data["inputs"], dict):
                    yaml_data = yaml_data["inputs"]
            except yaml.YAMLError as exc:
                print(f" — YAML parse error: {exc}", end="")
                yaml_data = {}
        else:
            yaml_data = {}

        linked_features, feature_titles = jira.get_linked_feature_keys(issue)
        validation_date  = jira.extract_validation_date(issue)

        component = build_component(issue, yaml_data, linked_features, feature_titles, validation_date, synced_at)
        components.append(component)

        status_tag = f"{component['completionStatus']} / {component['productContext']}"
        vd_tag     = f"validated {validation_date[:10]}" if validation_date else "no validation date"
        print(f" — {status_tag} — {vd_tag}")

    print(f"\n  Built {len(components)} component records")

    print("\n[3/4] Clearing existing data…")
    clear_existing(backend_url, api_token)

    print("\n[4/4] Pushing to API…")
    totals = push_to_api(backend_url, api_token, components)

    print("\n=== Summary ===")
    print(f"  Total processed : {len(components)}")
    print(f"  Created         : {totals['created']}")
    print(f"  Updated         : {totals['updated']}")
    print(f"  Unchanged       : {totals['unchanged']}")
    print(f"  Errors          : {totals['errors']}")

    if totals["errors"]:
        print("\nERROR: Some records failed to upsert.", file=sys.stderr)
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
