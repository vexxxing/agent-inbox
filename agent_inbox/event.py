"""The agent-inbox event format (spec version agent-inbox/1).

An event is a small JSON object:

    {
      "spec":    "agent-inbox/1",
      "id":      "w-coffee-price-3f2a9c1e0b7d4a55",   # stable id, used for dedupe
      "type":    "price.below",                       # what happened
      "source":  "watcher:coffee-price",              # who says so
      "time":    "2026-10-27T21:40:00Z",              # when (UTC, RFC 3339)
      "payload": {...}                                # anything JSON, kept small
    }

On the inbox pull request each event is one comment: a human-readable first
line, a hidden marker, and the event as a fenced JSON block. See SPEC.md.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import re
import uuid

SPEC = "agent-inbox/1"
MARKER = "<!-- agent-inbox:event v1 -->"

# type and source show up in the human-readable line, so they are restricted
# to a safe, boring alphabet. Nothing in them can format, link or @mention.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024
# GitHub refuses comments over 65,536 characters. Escaping can make the JSON
# block several times longer than the payload, so check the rendered comment.
MAX_COMMENT_CHARS = 65000

# Characters escaped inside the JSON block so that payload text can never
# close the code fence, open HTML, or @mention someone on GitHub.
_ESCAPES = {"`": "\\u0060", "<": "\\u003c", ">": "\\u003e", "@": "\\u0040", "&": "\\u0026"}

_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class EventError(ValueError):
    """Raised when an event is malformed or not allowed."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_id(prefix: str, *parts: object) -> str:
    """A deterministic id: same inputs, same id. Used by watchers for dedupe."""
    digest = hashlib.sha256(
        json.dumps([str(p) for p in parts], ensure_ascii=True).encode()
    ).hexdigest()[:16]
    clean = re.sub(r"[^A-Za-z0-9._:-]", "-", prefix)[:100].strip("-") or "e"
    return f"{clean}-{digest}"


def make_event(type: str, source: str, payload=None, *, id: str | None = None,
               time: str | None = None,
               max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> dict:
    """Build and validate an event. Raises EventError on bad input."""
    event = {
        "spec": SPEC,
        "id": id or f"e-{uuid.uuid4().hex}",
        "type": type,
        "source": source,
        "time": time or utc_now(),
        "payload": {} if payload is None else payload,
    }
    validate(event, max_payload_bytes=max_payload_bytes)
    return event


def validate(event: dict, *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> None:
    if not isinstance(event, dict):
        raise EventError("event must be a JSON object")
    if event.get("spec") != SPEC:
        raise EventError(f"spec must be {SPEC!r}")
    for key, pattern in (("type", NAME_RE), ("source", NAME_RE), ("id", ID_RE)):
        value = event.get(key)
        if not isinstance(value, str) or not pattern.match(value):
            raise EventError(f"{key} must match {pattern.pattern}")
    time = event.get("time")
    if not isinstance(time, str):
        raise EventError("time must be an RFC 3339 string")
    try:
        parsed = _dt.datetime.fromisoformat(time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventError(f"time is not RFC 3339: {time!r}") from exc
    if parsed.tzinfo is None:
        raise EventError(f"time needs a time zone (use Z for UTC): {time!r}")
    try:
        size = len(json.dumps(event.get("payload"), ensure_ascii=False).encode())
    except (TypeError, ValueError) as exc:
        raise EventError("payload must be JSON-serialisable") from exc
    if size > max_payload_bytes:
        raise EventError(f"payload is {size} bytes, limit is {max_payload_bytes}")
    extra = set(event) - {"spec", "id", "type", "source", "time", "payload"}
    if extra:
        raise EventError(f"unknown top-level fields: {sorted(extra)}")


def _safe_json(event: dict) -> str:
    text = json.dumps(event, ensure_ascii=False, indent=2, sort_keys=False)
    for char, escaped in _ESCAPES.items():
        text = text.replace(char, escaped)
    return text


def to_comment(event: dict) -> str:
    """Render an event as the body of an inbox comment."""
    validate(event, max_payload_bytes=10 ** 9)
    headline = f"**{event['type']}** from `{event['source']}` at {event['time']}"
    body = f"{headline}\n\n{MARKER}\n```json\n{_safe_json(event)}\n```\n"
    if len(body) > MAX_COMMENT_CHARS:
        raise EventError(f"event renders to {len(body)} characters; GitHub comments "
                         f"hold about {MAX_COMMENT_CHARS}. Send a smaller payload")
    return body


def from_comment(body: str) -> dict | None:
    """Parse an inbox comment back into an event. Returns None if the comment
    is not an agent-inbox event (so agents can skip ordinary comments)."""
    if not isinstance(body, str) or MARKER not in body:
        return None
    match = _FENCE_RE.search(body, body.index(MARKER))
    if not match:
        return None
    text = match.group(1)
    if "&" in text:
        # The writer escapes every & as \u0026, so a raw & means the body was
        # HTML-escaped in transit (Claude's PR wake, for one, turns { into
        # &#123;). Undo that; it cannot change a block the writer produced.
        text = html.unescape(text)
    try:
        event = json.loads(text)
        validate(event, max_payload_bytes=10 ** 9)
    except (ValueError, EventError):
        return None
    return event
