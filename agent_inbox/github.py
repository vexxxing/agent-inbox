"""A tiny GitHub REST client (standard library only).

Only what the inbox needs: find the inbox pull request, read recent comments
for dedupe, post a comment, and the few calls `setup` makes.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import event as ev

INBOX_LABEL = "agent-inbox"
INBOX_BRANCH = "agent-inbox"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, repo: str, token: str, api_url: str | None = None,
                 retries: int = 3):
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo or ""):
            raise ValueError(f"repo must look like owner/name, got {repo!r}")
        if not token:
            raise ValueError("a GitHub token is required")
        self.repo = repo
        self.token = token
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL")
                        or "https://api.github.com").rstrip("/")
        self.retries = retries

    @classmethod
    def from_env(cls) -> "GitHub":
        token = os.environ.get("INBOX_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        return cls(os.environ.get("GITHUB_REPOSITORY", ""), token)

    # -- plumbing -----------------------------------------------------------

    def request(self, method: str, path: str, body=None, *, full_url: str | None = None):
        url = full_url or f"{self.api_url}{path}"
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-inbox",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    link = resp.headers.get("Link", "")
                    return (json.loads(raw) if raw else None), link
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                limited = exc.code == 429 or (
                    exc.code == 403 and "rate limit" in detail.lower())
                if (exc.code >= 500 or limited) and attempt < self.retries:
                    # Secondary rate limits ask for a pause of a minute or
                    # more; GitHub says how long in Retry-After.
                    wait = min(2 ** attempt, 10)
                    if limited:
                        try:
                            wait = int(exc.headers.get("Retry-After") or 60)
                        except (TypeError, ValueError):
                            wait = 60
                        wait = min(max(wait, 1), 120)
                    time.sleep(wait)
                    continue
                raise GitHubError(exc.code, detail) from None
            except urllib.error.URLError:
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise

    def _repo(self, suffix: str) -> str:
        return f"/repos/{self.repo}{suffix}"

    # -- the inbox ----------------------------------------------------------

    def find_inbox(self) -> int:
        """Number of the inbox PR: INBOX_PR if set, otherwise the newest open
        pull request carrying the agent-inbox label."""
        pinned = os.environ.get("INBOX_PR", "").strip()
        if pinned:
            return int(pinned)
        items, _ = self.request(
            "GET", self._repo(f"/issues?labels={INBOX_LABEL}&state=open"
                              "&sort=created&direction=desc&per_page=10"))
        for item in items or []:
            if "pull_request" in item:
                return int(item["number"])
        raise GitHubError(404, "no open pull request labelled "
                               f"'{INBOX_LABEL}'; run the Setup inbox workflow first")

    def recent_comment_bodies(self, number: int, limit: int = 100) -> list[str]:
        """Bodies of the newest `limit` comments (the API lists oldest first,
        so jump to the last page)."""
        path = self._repo(f"/issues/{number}/comments?per_page=100")
        items, link = self.request("GET", path)
        last = _link(link, "last")
        bodies = [c.get("body", "") for c in items or []]
        if last:
            tail, link = self.request("GET", "", full_url=last)
            bodies = [c.get("body", "") for c in tail or []]
            prev = _link(link, "prev")
            if len(bodies) < limit and prev:
                before, _ = self.request("GET", "", full_url=prev)
                bodies = [c.get("body", "") for c in before or []] + bodies
        return bodies[-limit:]

    def recent_event_ids(self, number: int, limit: int = 100) -> set[str]:
        ids = set()
        for body in self.recent_comment_bodies(number, limit):
            parsed = ev.from_comment(body)
            if parsed:
                ids.add(parsed["id"])
        return ids

    def comment(self, number: int, body: str) -> dict:
        result, _ = self.request("POST", self._repo(f"/issues/{number}/comments"),
                                 {"body": body})
        return result

    def post_event(self, event: dict, *, dedupe: bool = True,
                   number: int | None = None) -> bool:
        """Post one event to the inbox. Returns False if it was a duplicate."""
        number = number or self.find_inbox()
        if dedupe and event["id"] in self.recent_event_ids(number):
            return False
        self.comment(number, ev.to_comment(event))
        return True

    # -- setup --------------------------------------------------------------

    def setup_inbox(self, title: str = "Agent inbox") -> int:
        """Create the label, the inbox branch and the draft inbox PR.
        Safe to run twice: returns the existing inbox if there is one."""
        try:
            return self.find_inbox()
        except GitHubError as exc:
            if exc.status != 404:
                raise
        try:
            self.request("POST", self._repo("/labels"), {
                "name": INBOX_LABEL, "color": "5319e7",
                "description": "Event inbox for agents. Do not merge."})
        except GitHubError as exc:
            if exc.status != 422:  # 422 = label already exists
                raise
        repo, _ = self.request("GET", self._repo(""))
        base = repo["default_branch"]
        ref, _ = self.request("GET", self._repo(f"/git/ref/heads/{base}"))
        try:
            self.request("POST", self._repo("/git/refs"), {
                "ref": f"refs/heads/{INBOX_BRANCH}", "sha": ref["object"]["sha"]})
        except GitHubError as exc:
            if exc.status != 422:  # branch already exists
                raise
        # A PR needs a diff, so the branch carries one small marker file.
        import base64
        marker = ("# Agent inbox\n\nThis branch exists only so the inbox pull "
                  "request has a diff. Never merge it.\n")
        path = self._repo("/contents/.agent-inbox/INBOX.md")
        try:
            self.request("PUT", path, {
                "message": "Create agent inbox marker",
                "content": base64.b64encode(marker.encode()).decode(),
                "branch": INBOX_BRANCH})
        except GitHubError as exc:
            if exc.status != 422:  # file already there
                raise
        pr_body = {
            "title": title, "head": INBOX_BRANCH, "base": base, "draft": True,
            "body": ("Every comment on this pull request is an event for an agent.\n\n"
                     "Do not merge or close it. See SPEC.md for the event format.")}
        try:
            pr, _ = self.request("POST", self._repo("/pulls"), pr_body)
        except GitHubError as exc:
            if exc.status == 403:
                raise GitHubError(403, "GitHub Actions may not create pull requests "
                                  "in this repository. Turn on Settings > Actions > "
                                  "General > 'Allow GitHub Actions to create and "
                                  "approve pull requests', or run setup locally "
                                  "(see README)") from None
            # Draft PRs are not offered on every plan (for example private
            # repos on GitHub Free). Fall back to an ordinary PR.
            if exc.status != 422 or "draft" not in str(exc).lower():
                raise
            pr_body["draft"] = False
            pr, _ = self.request("POST", self._repo("/pulls"), pr_body)
        number = int(pr["number"])
        self.request("POST", self._repo(f"/issues/{number}/labels"),
                     {"labels": [INBOX_LABEL]})
        return number


def _link(header: str, rel: str) -> str | None:
    for part in (header or "").split(","):
        match = re.search(r'<([^>]+)>\s*;\s*rel="([^"]+)"', part)
        if match and match.group(2) == rel:
            return match.group(1)
    return None
