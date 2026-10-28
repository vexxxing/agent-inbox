"""The webhook relay: turn a repository_dispatch (or a manual
workflow_dispatch) into one inbox event, after checking the shared secret and
the filters in inbox.yml."""

from __future__ import annotations

import fnmatch
import hmac
import json
import os
from pathlib import Path

from . import event as ev

DEFAULT_CONFIG = {
    "require_secret": True,
    "allow_manual": True,
    "allow_types": ["*"],
    "deny_types": [],
    "allow_sources": ["*"],
    "max_payload_bytes": ev.DEFAULT_MAX_PAYLOAD_BYTES,
    "drop_fields": ["secret", "token", "password", "authorization", "api_key", "apikey"],
}


class RelayRejected(ValueError):
    """The request was well-formed enough to read but is not allowed in."""


def load_config(path: str | Path = "inbox.yml") -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return config
    relay = data.get("relay") or {}
    unknown = set(relay) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"inbox.yml: unknown relay keys {sorted(unknown)}")
    config.update(relay)
    return config


def _drop(value, fields: set[str]):
    """Remove sensitive-looking keys anywhere in the payload."""
    if isinstance(value, dict):
        return {k: _drop(v, fields) for k, v in value.items()
                if str(k).lower() not in fields}
    if isinstance(value, list):
        return [_drop(v, fields) for v in value]
    return value


def _matches(value: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns or [])


def event_from_request(event_name: str, request: dict, config: dict,
                       secret: str | None) -> dict:
    """Validate an Actions trigger payload and return the inbox event."""
    if event_name == "repository_dispatch":
        etype = request.get("action") or ""
        body = request.get("client_payload") or {}
        if not isinstance(body, dict):
            raise RelayRejected("client_payload must be an object")
        supplied = body.get("secret")
        payload = body.get("payload", {})
        if config["require_secret"]:
            if not secret:
                raise RelayRejected("INBOX_SECRET is not set in repository secrets; "
                                    "refusing all events (set require_secret: false "
                                    "in inbox.yml to allow unsigned events)")
            if not isinstance(supplied, str) or not hmac.compare_digest(
                    supplied.encode(), secret.encode()):
                raise RelayRejected("secret missing or wrong")
    elif event_name == "workflow_dispatch":
        if not config["allow_manual"]:
            raise RelayRejected("manual events are turned off in inbox.yml")
        inputs = request.get("inputs") or {}
        etype = inputs.get("type") or ""
        body = {"source": inputs.get("source") or "manual", "id": inputs.get("id") or None}
        raw = inputs.get("payload") or "{}"
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise RelayRejected(f"payload input is not JSON: {exc}") from None
    else:
        raise RelayRejected(f"unsupported trigger {event_name!r}")

    source = body.get("source") or "webhook"
    if not isinstance(etype, str) or not ev.NAME_RE.match(etype):
        raise RelayRejected("event type must be letters, digits, . _ : / - (max 100)")
    if not isinstance(source, str) or not ev.NAME_RE.match(source):
        raise RelayRejected("source must be letters, digits, . _ : / - (max 100)")
    if not _matches(etype, config["allow_types"]) or _matches(etype, config["deny_types"]):
        raise RelayRejected(f"event type {etype!r} is not allowed by inbox.yml")
    if not _matches(source, config["allow_sources"]):
        raise RelayRejected(f"source {source!r} is not allowed by inbox.yml")

    payload = _drop(payload, {f.lower() for f in config["drop_fields"]})
    try:
        event = ev.make_event(etype, source, payload, id=body.get("id") or None,
                              max_payload_bytes=int(config["max_payload_bytes"]))
        ev.to_comment(event)  # also too long once escaped?
    except ev.EventError as exc:
        raise RelayRejected(str(exc)) from None
    return event


def main_relay(github=None, *, config_path: str = "inbox.yml") -> int:
    """Entry point used by the relay workflow."""
    from .github import GitHub

    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        request = json.load(fh)
    config = load_config(config_path)
    try:
        event = event_from_request(event_name, request, config,
                                   os.environ.get("INBOX_SECRET"))
    except RelayRejected as exc:
        # Never echo the request: it may carry a secret or hostile text.
        print(f"::error::event rejected: {exc}")
        return 1
    github = github or GitHub.from_env()
    if github.post_event(event):
        print(f"posted {event['type']} {event['id']}")
    else:
        print(f"skipped duplicate {event['id']}")
    return 0
