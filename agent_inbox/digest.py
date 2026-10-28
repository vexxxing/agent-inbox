"""The daily digest: one inbox event that sums up every enabled watcher.

It does two jobs. It delivers the events that `notify: digest` watchers held
back, and it shows the watchers are alive on days when nothing happened, so
silence doesn't look like breakage. It runs at the end of a scheduled
watchers run, so it costs no extra Actions minutes and shares the same state.
"""

from __future__ import annotations

import datetime as _dt
import re

from . import event as ev

DEFAULT_CONFIG = {"enabled": "auto", "at": "08:00"}
# Held events listed per watcher before the rest are only counted.
MAX_LISTED = 20


def load_config(path="inbox.yml") -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return config
    section = data.get("digest") or {}
    if not isinstance(section, dict):
        raise ValueError("inbox.yml: digest must be a mapping")
    unknown = set(section) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"inbox.yml: unknown digest keys {sorted(unknown)}")
    config.update(section)
    if config["enabled"] not in (True, False, "auto"):
        raise ValueError("inbox.yml: digest.enabled must be true, false or auto")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(config["at"])):
        raise ValueError("inbox.yml: digest.at must be a UTC time like 08:00")
    return config


def wanted(config: dict, watchers: list[dict]) -> bool:
    """On when asked for, or (auto) when an enabled watcher holds events for it."""
    if config["enabled"] == "auto":
        return any(w.get("enabled", True) and w.get("notify") == "digest" for w in watchers)
    return bool(config["enabled"])


def is_due(config: dict, state: dict, now: _dt.datetime) -> bool:
    """Once a day, on the first run at or after digest.at (UTC)."""
    hour, minute = (int(x) for x in str(config["at"]).split(":"))
    today = now.date().isoformat()
    if (state.get("digest") or {}).get("last_date") == today:
        return False
    return (now.hour, now.minute) >= (hour, minute)


def build(watchers: list[dict], state: dict, now: _dt.datetime,
          max_listed: int = MAX_LISTED, extra: bool = False) -> dict:
    now_iso = now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, new, failing = [], 0, 0
    for spec in watchers:
        if not spec.get("enabled", True):
            continue
        wstate = state["watchers"].get(spec["name"], {})
        held = wstate.get("held", [])
        posted = int(wstate.get("posted", 0))
        more = max(0, len(held) - max_listed) + int(wstate.get("held_dropped", 0))
        row = {"watcher": spec["name"], "notify": spec.get("notify", "quiet"),
               "last_check": wstate.get("last_run"), "posted": posted,
               "status": "error" if wstate.get("errors") else "ok"}
        if wstate.get("last_error"):
            row["error"] = wstate["last_error"]
        if held:
            row["held"] = [{"type": e["type"], "id": e["id"], "time": e["time"],
                            "payload": e["payload"]} for e in held[:max_listed]]
        if more:
            row["held_more"] = more
        rows.append(row)
        new += posted + len(held) + int(wstate.get("held_dropped", 0))
        failing += row["status"] == "error"
    watches = f"{len(rows)} watch{'es' if len(rows) != 1 else ''}"
    parts = [watches, f"{new} new" if new else "nothing new"]
    if failing:
        parts.append(f"{failing} failing")
    last = (state.get("digest") or {}).get("last_time")
    payload = {"summary": ", ".join(parts), "since": last, "watchers": rows}
    return ev.make_event("inbox.digest", "digest", payload,
                         # One per day, so a retried run dedupes; an extra digest
                         # asked for by hand gets its own id.
                         id=ev.stable_id("digest", now.date().isoformat(),
                                         now_iso if extra else ""), time=now_iso)


def build_fitting(watchers: list[dict], state: dict, now: _dt.datetime,
                  extra: bool = False) -> dict:
    """build(), listing fewer held events per watcher until it fits one comment."""
    for listed in (MAX_LISTED, 10, 5, 2, 0):
        try:
            event = build(watchers, state, now, max_listed=listed, extra=extra)
            if len(ev.to_comment(event)) <= ev.MAX_COMMENT_CHARS:
                return event
        except ev.EventError:
            continue
    raise ev.EventError("digest too large even without held events")


def clear(state: dict, now: _dt.datetime) -> None:
    """Forget what the digest just reported."""
    for wstate in state["watchers"].values():
        for key in ("held", "held_dropped", "posted"):
            wstate.pop(key, None)
    state["digest"] = {"last_date": now.date().isoformat(),
                       "last_time": now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")}


def run(watchers: list[dict], state: dict, post, config: dict, *,
        now: _dt.datetime | None = None, force: bool = False, log=print) -> dict | None:
    """Post the digest if it's wanted and due (or forced). If posting fails,
    nothing is cleared, so the next run tries again with the same id."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if not force and not (wanted(config, watchers) and is_due(config, state, now)):
        return None
    event = build_fitting(watchers, state, now, extra=force)
    post(event)
    clear(state, now)
    log(f"[digest] {event['payload']['summary']}")
    return event

