"""Jira REST API client for the component onboarding pipeline."""

import base64
import time
import requests


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.rstrip("/")
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None, max_retries: int = 3) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(max_retries):
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 2 ** attempt))
                print(f"  Rate limited — waiting {wait}s before retry {attempt + 1}/{max_retries}")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Failed after {max_retries} retries: GET {path}")

    def search_jql(self, jql: str, fields: list[str], expand: list[str] = None) -> list[dict]:
        """Fetch all issues matching JQL using cursor-based pagination."""
        all_issues = []
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": 100,
        }
        if expand:
            params["expand"] = ",".join(expand)

        while True:
            data = self._get("/rest/api/3/search/jql", params=params)
            issues = data.get("issues", [])
            all_issues.extend(issues)

            next_page_token = data.get("nextPageToken")
            if not next_page_token or data.get("isLast", True):
                break
            params["nextPageToken"] = next_page_token

        return all_issues

    def get_attachment_content(self, issue_key: str, filename: str) -> str | None:
        """Download a named attachment from a Jira issue. Returns text content or None."""
        data = self._get(f"/rest/api/3/issue/{issue_key}", params={"fields": "attachment"})
        attachments = data.get("fields", {}).get("attachment", [])

        for attachment in attachments:
            if attachment.get("filename") == filename:
                content_url = attachment.get("content")
                if not content_url:
                    return None
                response = requests.get(content_url, headers=self.headers, timeout=30)
                response.raise_for_status()
                return response.text

        return None

    def get_linked_feature_keys(
        self,
        issue: dict,
        target_prefixes: tuple[str, ...] = ("RHAISTRAT-", "RHAIRFE-"),
    ) -> tuple[list[str], dict[str, str]]:
        """Return (keys, featureTitles) for linked issues matching target_prefixes.

        keys: deduplicated list of matching issue keys (preserve order)
        featureTitles: {key: summary} for issues where the summary was included
        """
        links = issue.get("fields", {}).get("issuelinks", [])
        keys = []
        titles: dict[str, str] = {}
        for link in links:
            for direction in ("inwardIssue", "outwardIssue"):
                linked = link.get(direction) or {}
                key = linked.get("key", "")
                if any(key.startswith(prefix) for prefix in target_prefixes):
                    keys.append(key)
                    summary = (linked.get("fields") or {}).get("summary", "")
                    if summary:
                        titles[key] = summary
        unique_keys = list(dict.fromkeys(keys))
        return unique_keys, {k: titles[k] for k in unique_keys if k in titles}

    def extract_validation_date(self, issue: dict, label: str = "validation-successful") -> str | None:
        """Find the earliest date the given label was added, from the issue changelog.

        Returns an ISO 8601 string or None if the label was never added or
        changelog is not present (i.e. expand=changelog was not requested).
        """
        changelog = issue.get("changelog") or {}
        histories = changelog.get("histories") or []
        earliest = None
        for history in histories:
            for item in history.get("items", []):
                if item.get("field") != "labels":
                    continue
                after  = set((item.get("toString")  or "").split())
                before = set((item.get("fromString") or "").split())
                if label in after and label not in before:
                    date = history.get("created")
                    if date and (earliest is None or date < earliest):
                        earliest = date
        return earliest
