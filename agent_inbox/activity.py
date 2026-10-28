"""Repository activity: turn GitHub's own events on this repository (a star,
a fork, a new issue, pull request, discussion or comment) into inbox events,
and poll the traffic API for new clones and views.

The Repo activity workflow runs `python -m agent_inbox activity` on each of
those events and `python -m agent_inbox traffic` on a schedule.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import event as ev

SOURCE = "github"
MAX_TITLE = 200

# (GitHub event, action) -> inbox event type. Anything else is ignored.
TYPES = {
    ("watch", "started"): "repo.starred",
    ("fork", None): "repo.forked",
    ("issues", "opened"): "repo.issue_opened",
    ("issue_comment", "created"): "repo.comment",
    ("pull_request_target", "opened"): "repo.pr_opened",
    ("pull_request", "opened"): "repo.pr_opened",
    ("discussion", "created"): "repo.discussion",
    ("discussion_comment", "created"): "repo.discussion_comment",
}


def _title(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_TITLE else text[:MAX_TITLE - 1] + "…"


def _is_inbox(item: dict, inbox_number: int | None) -> bool:
    from .github import INBOX_BRANCH, INBOX_LABEL
    if inbox_number and item.get("number") == inbox_number:
        return True
    labels = {(label or {}).get("name") for label in item.get("labels") or []}
    if INBOX_LABEL in labels:
        return True
    return ((item.get("head") or {}).get("ref") == INBOX_BRANCH)


def event_from_github(event_name: str, request: dict, *, run_id: str = "",
                      inbox_number: int | None = None,
                      include_owner: bool = False) -> dict | None:
    """The inbox event for one GitHub event, or None when it should be skipped:
    an action we don't report, the owner's or a bot's own activity, or anything
    on the inbox pull request itself (which would echo the inbox into itself)."""
    repo = request.get("repository") or {}
    if event_name == "workflow_dispatch":
        return ev.make_event("repo.test", SOURCE, {
            "test": True,
            "note": "Test event from Repo activity > Run workflow. Not real activity.",
            "repository": repo.get("full_name"),
        }, id=ev.stable_id("repo-test", run_id) if run_id else None)

    action = request.get("action")
    etype = TYPES.get((event_name, action)) or TYPES.get((event_name, None))
    if not etype:
        return None
    sender = request.get("sender") or {}
    login = str(sender.get("login") or "")
    owner = str((repo.get("owner") or {}).get("login") or "")
    if sender.get("type") == "Bot" or login.endswith("[bot]"):
        return None
    if not include_owner and login and login.lower() == owner.lower():
        return None

    payload = {"by": login, "by_url": sender.get("html_url"),
               "repository": repo.get("full_name")}
    if event_name == "watch":
        payload["stars"] = repo.get("stargazers_count")
        payload["url"] = f"{repo.get('html_url')}/stargazers"
    elif event_name == "fork":
        fork = request.get("forkee") or {}
        payload.update(fork=fork.get("full_name"), url=fork.get("html_url"),
                       forks=repo.get("forks_count"))
    else:
        item_key = {"issues": "issue", "issue_comment": "issue",
                    "pull_request_target": "pull_request", "pull_request": "pull_request",
                    "discussion": "discussion", "discussion_comment": "discussion"}[event_name]
        item = request.get(item_key) or {}
        if item_key != "discussion" and _is_inbox(item, inbox_number):
            return None
        payload.update(number=item.get("number"), title=_title(item.get("title")),
                       url=item.get("html_url"))
        if event_name in ("issue_comment", "discussion_comment"):
            comment = request.get("comment") or {}
            payload["on"] = ("pull_request" if "pull_request" in item
                             else "discussion" if item_key == "discussion" else "issue")
            payload["url"] = comment.get("html_url") or payload["url"]
            payload["excerpt"] = _title(comment.get("body"))
    payload = {k: v for k, v in payload.items() if v is not None}
    eid = ev.stable_id(etype.replace("repo.", "repo-"), run_id or json.dumps(payload))
    return ev.make_event(etype, SOURCE, payload, id=eid)


def main_activity(github=None) -> int:
    """Entry point for the Repo activity workflow."""
    from .github import GitHub
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        request = json.load(fh)
    pinned = os.environ.get("INBOX_PR", "").strip()
    event = event_from_github(
        event_name, request,
        run_id=f"{os.environ.get('GITHUB_RUN_ID', '')}",
        inbox_number=int(pinned) if pinned.isdigit() else None,
        include_owner=os.environ.get("INCLUDE_OWNER", "").lower() == "true")
    if event is None:
        print(f"skipped {event_name} ({request.get('action')}): not reported")
        return 0
    github = github or GitHub.from_env()
    if github.post_event(event):
        print(f"posted {event['type']} {event['id']}")
    else:
        print(f"skipped duplicate {event['id']}")
    return 0


# -- traffic ---------------------------------------------------------------

def traffic_event(clones: dict, views: dict, state: dict) -> dict | None:
    """Compare the traffic API's daily counts with what we saw last time.
    Returns one repo.traffic event when anything went up (or on the first
    run, as a baseline), and updates `state` in place."""
    seen = state.setdefault("days", {})
    first = not seen
    new = {"clones": 0, "cloners": 0, "views": 0, "visitors": 0}
    for kind, data, total_key, unique_key in (("clones", clones, "clones", "cloners"),
                                              ("views", views, "views", "visitors")):
        for day in data.get(kind) or []:
            stamp = str(day.get("timestamp", ""))[:10]
            if not stamp:
                continue
            old = seen.setdefault(stamp, {}).get(kind, {"count": 0, "uniques": 0})
            count, uniques = int(day.get("count") or 0), int(day.get("uniques") or 0)
            new[total_key] += max(0, count - int(old.get("count", 0)))
            new[unique_key] += max(0, uniques - int(old.get("uniques", 0)))
            seen[stamp][kind] = {"count": max(count, int(old.get("count", 0))),
                                 "uniques": max(uniques, int(old.get("uniques", 0)))}
    # GitHub keeps 14 days; keep a little more so late updates still diff.
    for stamp in sorted(seen)[:-16]:
        del seen[stamp]
    if not first and not any(new.values()):
        return None
    payload = {
        "baseline": first,
        "new": new,
        "last_14_days": {"clones": clones.get("count"), "cloners": clones.get("uniques"),
                         "views": views.get("count"), "visitors": views.get("uniques")},
        "note": "Counts only; GitHub does not say who cloned or visited.",
    }
    return ev.make_event("repo.traffic", SOURCE, payload)


def main_traffic(state_path: str, github=None, poster=None) -> int:
    from .github import GitHub, GitHubError
    github = github or GitHub(os.environ.get("GITHUB_REPOSITORY", ""),
                              os.environ.get("TRAFFIC_TOKEN")
                              or os.environ.get("GITHUB_TOKEN", ""))
    try:
        clones, _ = github.request("GET", github._repo("/traffic/clones"))
        views, _ = github.request("GET", github._repo("/traffic/views"))
    except GitHubError as exc:
        if exc.status in (401, 403, 404):
            print("::notice::traffic counts are not readable with this token "
                  f"(GitHub API {exc.status}). They need a token with Administration: "
                  "read on this repository, saved as the TRAFFIC_TOKEN secret.")
            return 0
        raise
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {}
    event = traffic_event(clones or {}, views or {}, state)
    if event:
        (poster or GitHub.from_env()).post_event(event)
        print(f"posted repo.traffic {json.dumps(event['payload']['new'])}")
    else:
        print("traffic unchanged")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))
    return 0
