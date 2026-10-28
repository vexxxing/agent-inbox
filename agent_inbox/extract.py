"""Turn a fetched document into a list of items a watcher can test.

Every extractor returns a list of Item(key, value, extra). A single-value
watcher looks at the first item; a list watcher (`all: true`, or any RSS
watcher) looks at each item and remembers which keys it has already seen.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

MAX_VALUE_CHARS = 2000


@dataclass
class Item:
    key: str
    value: object
    extra: dict = field(default_factory=dict)


class ExtractError(ValueError):
    pass


def _clip(value):
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value).strip()
        return value[:MAX_VALUE_CHARS]
    return value


def extract(spec: dict, text: str) -> list[Item]:
    kinds = [k for k in ("css", "json", "rss", "regex") if k in spec]
    if len(kinds) != 1:
        raise ExtractError("extract needs exactly one of css, json, rss, regex")
    kind = kinds[0]
    items = {"css": _css, "json": _json, "rss": _rss, "regex": _regex}[kind](spec, text)
    if spec.get("number"):
        items = [Item(i.key, parse_number(i.value), i.extra) for i in items]
        items = [i for i in items if i.value is not None]
    if kind != "rss" and not spec.get("all"):
        items = items[:1]
    return items


# -- CSS ----------------------------------------------------------------------

def _css(spec: dict, text: str) -> list[Item]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ExtractError("css extract needs beautifulsoup4 "
                           "(pip install -r requirements.txt)") from exc
    soup = BeautifulSoup(text, "html.parser")
    attr = spec.get("attr", "text")
    items = []
    for node in soup.select(spec["css"]):
        value = node.get_text(" ") if attr == "text" else node.get(attr)
        if value is None:
            continue
        value = _clip(value if isinstance(value, str) else " ".join(value))
        items.append(Item(key=value, value=value))
    return items


# -- JSON path ----------------------------------------------------------------

_TOKEN_RE = re.compile(r"""\.?([A-Za-z_$][\w$-]*)|\[(-?\d+|\*|"[^"]*"|'[^']*')\]""")


def parse_path(path: str) -> list:
    """`a.b[0].c`, `items[*].price`, `["odd key"].x` -> tokens."""
    tokens, pos = [], 0
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    while pos < len(path):
        match = _TOKEN_RE.match(path, pos)
        if not match:
            raise ExtractError(f"cannot parse json path at {path[pos:]!r}")
        name, bracket = match.groups()
        if name is not None:
            tokens.append(name)
        elif bracket == "*":
            tokens.append("*")
        elif bracket.lstrip("-").isdigit():
            tokens.append(int(bracket))
        else:
            tokens.append(bracket[1:-1])
        pos = match.end()
    return tokens


def walk(data, tokens: list) -> list:
    nodes = [data]
    for token in tokens:
        nxt = []
        for node in nodes:
            if token == "*":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
            elif isinstance(token, int):
                if isinstance(node, list) and -len(node) <= token < len(node):
                    nxt.append(node[token])
            elif isinstance(node, dict) and token in node:
                nxt.append(node[token])
        nodes = nxt
    return nodes


def _json(spec: dict, text: str) -> list[Item]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ExtractError(f"response is not JSON: {exc}") from exc
    items = []
    for node in walk(data, parse_path(spec["json"])):
        key = json.dumps(node, sort_keys=True, ensure_ascii=True)[:500]
        items.append(Item(key=key, value=_clip(node)))
    return items


# -- RSS / Atom ---------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(node, path: str, ns: str = "") -> str:
    found = node.find(path if not ns else ns + path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _rss(spec: dict, text: str) -> list[Item]:
    if "<!DOCTYPE" in text[:2048].upper() or "<!ENTITY" in text.upper():
        # Refuse DTDs outright: no entity expansion tricks from untrusted feeds.
        raise ExtractError("feeds with a DOCTYPE or ENTITY are not accepted")
    try:
        # Parse the decoded text, not re-encoded bytes: the bytes would be read
        # with the feed's declared encoding a second time (mojibake for any
        # feed that declares something other than UTF-8).
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ExtractError(f"feed is not valid XML: {exc}") from exc
    field_name = spec.get("field", "title")
    entries = []
    for item in root.iter("item"):  # RSS 2.0
        entries.append({
            "title": _text(item, "title"), "link": _text(item, "link"),
            "id": _text(item, "guid"), "published": _text(item, "pubDate"),
            "summary": _text(item, "description")})
    for entry in root.iter(_ATOM + "entry"):  # Atom
        links = entry.findall(_ATOM + "link")
        # The entry's own page is rel="alternate" (or no rel), not "self" or "replies".
        link = next((l for l in links if l.get("rel", "alternate") == "alternate"),
                    links[0] if links else None)
        entries.append({
            "title": _text(entry, "title", _ATOM),
            "link": link.get("href", "") if link is not None else "",
            "id": _text(entry, "id", _ATOM),
            "published": _text(entry, "updated", _ATOM) or _text(entry, "published", _ATOM),
            "summary": _text(entry, "summary", _ATOM)})
    items = []
    for entry in entries:
        entry = {k: _clip(v) for k, v in entry.items()}
        key = entry["id"] or entry["link"] or entry["title"]
        if field_name == "any":
            value = " ".join(entry[k] for k in ("title", "summary"))
        else:
            value = entry.get(field_name, "")
        items.append(Item(key=key, value=value, extra={"item": entry}))
    return items


# -- regex --------------------------------------------------------------------

def _regex(spec: dict, text: str) -> list[Item]:
    try:
        pattern = re.compile(spec["regex"], re.IGNORECASE if spec.get("ignore_case") else 0)
    except re.error as exc:
        raise ExtractError(f"bad regex: {exc}") from exc
    group = spec.get("group", 1 if pattern.groups else 0)
    items = []
    for match in pattern.finditer(text):
        value = _clip(match.group(group))
        items.append(Item(key=value, value=value))
    return items


# -- numbers ------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")


def parse_number(value):
    """'$1,299.00' -> 1299.0, 42 -> 42.0, 'sold out' -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _NUM_RE.search(value)
    if not match:
        return None
    return float(match.group(0).replace(",", ""))
