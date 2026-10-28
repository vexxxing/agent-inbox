"""The watcher runner: check each watchers/*.yml, post hits to the inbox.

No AI runs here. A watcher fetches one URL, extracts a value, tests a plain
condition, and posts an event when the condition becomes true. State is kept
in one JSON file so the same hit never fires twice.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from . import event as ev
from .extract import ExtractError, Item, extract, parse_number

MAX_BYTES = 2 * 1024 * 1024
DEFAULT_EVERY = "30m"
DEFAULT_ERROR_THRESHOLD = 3
DEFAULT_MAX_SEEN = 500
# GitHub caps content creation at 80 requests a minute and 500 an hour. A feed
# that suddenly lists 200 items should not post 200 comments in one run.
DEFAULT_MAX_EVENTS = 20
USER_AGENT = "agent-inbox-watcher/1"

_CONDITION_KEYS = {"exists", "contains", "not_contains", "matches", "below",
                   "above", "at_most", "at_least", "equals", "not_equals", "changed"}
_EXTRACT_KEYS = {"css", "json", "rss", "regex", "attr", "all", "number", "field",
                 "group", "ignore_case"}
_FETCH_KEYS = {"url", "headers", "timeout"}
_NUMBER_CONDITIONS = ("below", "above", "at_most", "at_least")
# Header values may read environment variables, but never the tokens the
# workflow itself holds: a watcher URL is a third-party server.
_SECRET_ENV_RE = re.compile(r"^(GITHUB_|ACTIONS_|RUNNER_|INBOX_)")
_TOP_KEYS = {"name", "enabled", "every", "fetch", "extract", "condition", "event",
             "fire", "prime", "notify_errors_after", "description", "max_seen", "max_events"}


class WatcherError(ValueError):
    pass


# -- loading and validation -----------------------------------------------------

def parse_duration(text) -> int:
    """'45s', '30m', '2h', '1d' -> seconds."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(text))
    if not match:
        raise WatcherError(f"bad duration {text!r}: use e.g. 30m, 2h, 1d")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def load_watchers(directory: Path) -> list[dict]:
    import yaml  # PyYAML, the one required dependency

    watchers, names = [], set()
    for path in sorted(Path(directory).glob("*.y*ml")):
        with open(path, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        if not isinstance(spec, dict):
            raise WatcherError(f"{path.name}: top level must be a mapping")
        spec.setdefault("name", path.stem)
        check_watcher(spec, path.name)
        if spec["name"] in names:
            raise WatcherError(f"{path.name}: duplicate watcher name {spec['name']!r}")
        names.add(spec["name"])
        watchers.append(spec)
    return watchers


def check_watcher(spec: dict, where: str = "watcher") -> None:
    def fail(msg):
        raise WatcherError(f"{where}: {msg}")

    unknown = set(spec) - _TOP_KEYS
    if unknown:
        fail(f"unknown keys {sorted(unknown)}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", str(spec.get("name", ""))):
        fail("name must be letters, digits, '.', '_' or '-' (max 64)")
    fetch = spec.get("fetch")
    if not isinstance(fetch, dict) or not isinstance(fetch.get("url"), str):
        fail("fetch.url is required")
    if not re.match(r"^(https?|file):", fetch["url"]):
        fail("fetch.url must start with https://, http:// or file:")
    if set(fetch) - _FETCH_KEYS:
        fail(f"unknown fetch keys {sorted(set(fetch) - _FETCH_KEYS)}")
    if "headers" in fetch and not isinstance(fetch["headers"], dict):
        fail("fetch.headers must be a mapping")
    for value in (fetch.get("headers") or {}).values():
        for var in re.findall(r"\$\{([A-Za-z0-9_]+)\}", str(value)):
            if _SECRET_ENV_RE.match(var):
                fail(f"fetch.headers may not read ${{{var}}}: GITHUB_*, ACTIONS_*, "
                     "RUNNER_* and INBOX_* are kept from third-party sites")
    try:
        if float(fetch.get("timeout", 20)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        fail("fetch.timeout must be a positive number of seconds")
    extract_spec = spec.get("extract")
    if not isinstance(extract_spec, dict):
        fail("extract is required")
    if set(extract_spec) - _EXTRACT_KEYS:
        fail(f"unknown extract keys {sorted(set(extract_spec) - _EXTRACT_KEYS)}")
    if sum(k in extract_spec for k in ("css", "json", "rss", "regex")) != 1:
        fail("extract needs exactly one of css, json, rss, regex")
    condition = spec.get("condition", {"exists": True})
    if not isinstance(condition, dict) or not condition:
        fail("condition must be a mapping")
    if set(condition) - _CONDITION_KEYS:
        fail(f"unknown condition keys {sorted(set(condition) - _CONDITION_KEYS)}")
    for key in _NUMBER_CONDITIONS:
        if key in condition:
            try:
                float(condition[key])
            except (TypeError, ValueError):
                fail(f"condition.{key} must be a number")
    if "matches" in condition:
        try:
            re.compile(condition["matches"])
        except re.error as exc:
            fail(f"condition.matches is not a valid regex: {exc}")
    event_spec = spec.get("event")
    if not isinstance(event_spec, dict) or not ev.NAME_RE.match(str(event_spec.get("type", ""))):
        fail("event.type is required (letters, digits, . _ : / -)")
    if spec.get("fire", "edge") not in ("edge", "always"):
        fail("fire must be edge or always")
    for key in ("max_events", "max_seen", "notify_errors_after"):
        if key in spec and (not isinstance(spec[key], int) or isinstance(spec[key], bool)
                            or spec[key] < (0 if key == "notify_errors_after" else 1)):
            fail(f"{key} must be a whole number")
    parse_duration(spec.get("every", DEFAULT_EVERY))


# -- state ---------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict) and isinstance(state.get("watchers"), dict):
            return state
    except FileNotFoundError:
        pass
    except ValueError:
        print(f"warning: {path} is not valid JSON, starting fresh", file=sys.stderr)
    return {"version": 1, "watchers": {}}


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# -- fetching ------------------------------------------------------------------

def fetch(spec: dict, allow_file: bool = False) -> str:
    url = spec["url"]
    if url.startswith("file:") and not allow_file:
        raise WatcherError("file: URLs are only allowed with --allow-file")
    agent = USER_AGENT
    if os.environ.get("GITHUB_REPOSITORY"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        agent += f" (+{server}/{os.environ['GITHUB_REPOSITORY']})"
    headers = {"User-Agent": agent, "Accept": "*/*"}
    headers.update({str(k): str(v) for k, v in (spec.get("headers") or {}).items()})
    # Header values may reference environment variables, for API keys kept in
    # repository secrets: "Bearer ${MY_API_KEY}".
    def env(match):
        name = match.group(1)
        if _SECRET_ENV_RE.match(name):
            raise WatcherError(f"headers may not read ${{{name}}}")
        if name not in os.environ:
            raise WatcherError(f"headers read ${{{name}}}, which is not set; add it to "
                               "the env of the Check watchers step")
        return os.environ[name]
    headers = {k: re.sub(r"\$\{([A-Za-z0-9_]+)\}", env, v) for k, v in headers.items()}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=float(spec.get("timeout", 20))) as resp:
        raw = resp.read(MAX_BYTES + 1)
        charset = resp.headers.get_content_charset() if hasattr(resp, "headers") else None
    if len(raw) > MAX_BYTES:
        raise WatcherError(f"response larger than {MAX_BYTES} bytes")
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:  # the server named a charset Python doesn't know
        return raw.decode("utf-8", errors="replace")


# -- conditions ----------------------------------------------------------------

def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def condition_holds(condition: dict, item: Item | None) -> bool:
    """All listed tests must pass. `changed` is handled by the runner."""
    exists = item is not None and item.value not in (None, "", [], {})
    if "exists" in condition and bool(condition["exists"]) != exists:
        return False
    tests = set(condition) - {"exists", "changed"}
    if not tests:
        return "exists" in condition or "changed" in condition or exists
    if not exists:
        return False
    text = _as_text(item.value)
    for key in tests:
        want = condition[key]
        if key in ("contains", "not_contains"):
            options = want if isinstance(want, list) else [want]
            hit = any(str(o).lower() in text.lower() for o in options)
            if hit != (key == "contains"):
                return False
        elif key == "matches":
            if not re.search(want, text):
                return False
        elif key in ("below", "above", "at_most", "at_least"):
            number = parse_number(item.value)
            if number is None:
                return False
            limit = float(want)
            ok = {"below": number < limit, "above": number > limit,
                  "at_most": number <= limit, "at_least": number >= limit}[key]
            if not ok:
                return False
        elif key in ("equals", "not_equals"):
            if _equal(item.value, text, want) != (key == "equals"):
                return False
    return True


def _equal(value, text: str, want) -> bool:
    """Same text, or both numbers and numerically equal ("20.0" == 20)."""
    if text == _as_text(want):
        return True
    a, b = parse_number(value), parse_number(want)
    return a is not None and b is not None and a == b


# -- the runner ----------------------------------------------------------------

def _is_due(wstate: dict, every: int, now: _dt.datetime) -> bool:
    last = wstate.get("last_run")
    if not last:
        return True
    try:
        then = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    # Scheduled runs start minutes late and unevenly, so a 30m watcher on a
    # 30m cron can see a 26-minute gap. Allow a quarter of the interval (at
    # least a minute, at most ten) or it would skip every other run.
    slack = max(60, min(every // 4, 600))
    return (now - then).total_seconds() >= every - slack


def _payload(spec: dict, item: Item | None, extra: dict | None = None) -> dict:
    payload = {"watcher": spec["name"], "url": spec["fetch"]["url"]}
    message = (spec.get("event") or {}).get("message")
    if message:
        payload["message"] = str(message)[:500]
    if item is not None:
        payload["value"] = item.value
        payload.update(item.extra)
    if extra:
        payload.update(extra)
    return payload


def check_one(spec: dict, wstate: dict, text: str, now_iso: str) -> list[dict]:
    """Decide which events a single fetched document produces, and update the
    watcher's state in place. Pure: no network, no posting."""
    name = spec["name"]
    etype = spec["event"]["type"]
    source = f"watcher:{name}"
    condition = spec.get("condition") or {"exists": True}
    fire = spec.get("fire", "edge")
    items = extract(spec["extract"], text)
    events = []
    is_list = "rss" in spec["extract"] or spec["extract"].get("all")

    if is_list:
        seen = wstate.get("seen")
        first_run = seen is None
        seen = list(seen or [])
        seen_set = set(seen)
        prime = spec.get("prime", True)
        if first_run and not items:
            return events  # an empty first look (a blocked page?) primes nothing
        limit = int(spec.get("max_events", DEFAULT_MAX_EVENTS))
        for item in items:
            if not condition_holds(condition, item):
                continue
            key = _h(item.key)
            if key in seen_set and fire == "edge":
                continue
            if len(events) >= limit and not (first_run and prime):
                break  # the rest stay unseen and fire on the next run
            if key not in seen_set:
                seen.append(key)
                seen_set.add(key)
            if first_run and prime:
                continue
            events.append(ev.make_event(
                etype, source, _payload(spec, item),
                id=ev.stable_id(f"w-{name}", item.key) if fire == "edge"
                else ev.stable_id(f"w-{name}", item.key, now_iso),
                time=now_iso))
        wstate["seen"] = seen[-int(spec.get("max_seen", DEFAULT_MAX_SEEN)):]
        return events

    item = items[0] if items else None
    if item is None and "exists" not in condition:
        # Nothing matched. Usually the page changed or blocked the request;
        # treating that as "condition false" would re-arm the watcher and
        # fire the same hit again once the page is back.
        raise ExtractError("nothing on the page matched the extractor")
    value_key = None if item is None else _h(_as_text(item.value))
    if condition.get("changed"):
        previous = wstate.get("last_value")
        seen_before = "last_value" in wstate
        wstate["last_value"] = value_key
        if not seen_before:
            return []  # the first look only records the value
        if value_key != previous and condition_holds(condition, item):
            changes = int(wstate.get("changes", 0)) + 1
            wstate["changes"] = changes
            # No clock in the id: a retry after a failed post must dedupe.
            events.append(ev.make_event(
                etype, source, _payload(spec, item),
                id=ev.stable_id(f"w-{name}", "changed", changes, value_key), time=now_iso))
        return events

    holds = condition_holds(condition, item)
    armed = wstate.get("armed", True)
    if holds and (armed or fire == "always"):
        fires = int(wstate.get("fires", 0)) + 1
        wstate["fires"] = fires
        events.append(ev.make_event(
            etype, source, _payload(spec, item),
            id=ev.stable_id(f"w-{name}", "edge", fires, value_key), time=now_iso))
    wstate["armed"] = not holds
    return events


def run(watchers: list[dict], state: dict, post, *, now: _dt.datetime | None = None,
        allow_file: bool = False, force: bool = False, only: str | None = None,
        fetcher=fetch, log=print) -> dict:
    """Check every due watcher. `post(event)` sends one event; if it raises,
    that watcher's state is left unchanged so the hit is retried next run."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = {"checked": 0, "skipped": 0, "events": 0, "errors": 0}
    if only and only not in {w["name"] for w in watchers}:
        raise WatcherError(f"no watcher named {only!r}")
    for spec in watchers:
        name = spec["name"]
        if only and name != only:
            continue
        if not spec.get("enabled", True) and only != name:
            summary["skipped"] += 1
            continue
        wstate = state["watchers"].setdefault(name, {})
        if not force and not _is_due(wstate, parse_duration(spec.get("every", DEFAULT_EVERY)), now):
            summary["skipped"] += 1
            continue
        summary["checked"] += 1
        trial = json.loads(json.dumps(wstate))  # work on a copy until posted
        try:
            text = fetcher(spec["fetch"], allow_file=allow_file)
            events = check_one(spec, trial, text, now_iso)
        except Exception as exc:  # noqa: BLE001 - one bad watcher must not stop the rest
            summary["errors"] += 1
            wstate["last_run"] = now_iso
            wstate["errors"] = int(wstate.get("errors", 0)) + 1
            wstate["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            log(f"[{name}] error: {wstate['last_error']}")
            threshold = int(spec.get("notify_errors_after", DEFAULT_ERROR_THRESHOLD))
            if threshold and wstate["errors"] == threshold:
                try:
                    post(ev.make_event(
                        "watcher.error", f"watcher:{name}",
                        {"watcher": name, "consecutive_errors": threshold,
                         "error": wstate["last_error"]},
                        id=ev.stable_id(f"w-{name}", "error", now_iso), time=now_iso))
                except Exception as post_exc:  # noqa: BLE001
                    log(f"[{name}] could not post error event: {post_exc}")
            continue
        try:
            for event in events:
                post(event)
                summary["events"] += 1
                log(f"[{name}] {event['type']} {event['id']}")
        except Exception as exc:  # noqa: BLE001 - keep other watchers running
            summary["errors"] += 1
            log(f"[{name}] could not post, will retry next run: {exc}")
            continue
        trial["last_run"] = now_iso
        trial["errors"] = 0
        trial.pop("last_error", None)
        state["watchers"][name] = trial
        if not events:
            log(f"[{name}] no hit")
    # Forget state for watchers that no longer exist.
    names = {w["name"] for w in watchers}
    for gone in set(state["watchers"]) - names:
        del state["watchers"][gone]
    return summary
