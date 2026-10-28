"""Shared test helpers: fixture paths and a fake GitHub API server."""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))


def fixture_url(name: str) -> str:
    return (FIXTURES / name).as_uri()


class FakeGitHub:
    """Just enough of the GitHub REST API for agent-inbox, kept in memory."""

    def __init__(self, page_size: int = 100, draft_supported: bool = True):
        self.issues = {}          # number -> {"labels": [...], "pull_request": bool}
        self.comments = {}        # number -> [body, ...]
        self.labels = set()
        self.refs = {"main": "sha-main"}
        self.files = {}
        self.page_size = page_size
        self.draft_supported = draft_supported
        self.requests = []
        self.next_number = 1
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add_pr(self, labels=("agent-inbox",)) -> int:
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {"labels": list(labels), "pull_request": True, "state": "open"}
        self.comments[number] = []
        return number

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=None, headers=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                assert self.headers["Authorization"].startswith("Bearer ")
                path, _, query = self.path.partition("?")
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                m = re.fullmatch(r"/repos/o/r/issues", path)
                if m:
                    label = params.get("labels")
                    items = [{"number": n, "labels": i["labels"],
                              **({"pull_request": {}} if i["pull_request"] else {})}
                             for n, i in sorted(fake.issues.items(), reverse=True)
                             if label in i["labels"] and i["state"] == "open"]
                    return self._send(200, items)
                m = re.fullmatch(r"/repos/o/r/issues/(\d+)/comments", path)
                if m:
                    allc = fake.comments.get(int(m.group(1)), [])
                    size = min(int(params.get("per_page", 30)), fake.page_size)
                    page = int(params.get("page", 1))
                    pages = max(1, -(-len(allc) // size))
                    chunk = allc[(page - 1) * size: page * size]
                    links = []
                    base = f"{fake.url}{path}?per_page={params.get('per_page', 30)}"
                    if page < pages:
                        links.append(f'<{base}&page={page + 1}>; rel="next"')
                        links.append(f'<{base}&page={pages}>; rel="last"')
                    if page > 1:
                        links.append(f'<{base}&page={page - 1}>; rel="prev"')
                    return self._send(200, [{"body": b} for b in chunk],
                                      {"Link": ", ".join(links)} if links else None)
                if path == "/repos/o/r":
                    return self._send(200, {"default_branch": "main"})
                m = re.fullmatch(r"/repos/o/r/git/ref/heads/(.+)", path)
                if m and m.group(1) in fake.refs:
                    return self._send(200, {"object": {"sha": fake.refs[m.group(1)]}})
                return self._send(404, {"message": "Not Found"})

            def do_POST(self):
                body = self._body()
                fake.requests.append(("POST", self.path, body))
                m = re.fullmatch(r"/repos/o/r/issues/(\d+)/comments", self.path)
                if m:
                    fake.comments[int(m.group(1))].append(body["body"])
                    return self._send(201, {"id": len(fake.comments[int(m.group(1))])})
                if self.path == "/repos/o/r/labels":
                    if body["name"] in fake.labels:
                        return self._send(422, {"message": "already_exists"})
                    fake.labels.add(body["name"])
                    return self._send(201, body)
                if self.path == "/repos/o/r/git/refs":
                    name = body["ref"].removeprefix("refs/heads/")
                    if name in fake.refs:
                        return self._send(422, {"message": "Reference already exists"})
                    fake.refs[name] = body["sha"]
                    return self._send(201, {})
                if self.path == "/repos/o/r/pulls":
                    if body.get("draft") and not fake.draft_supported:
                        return self._send(422, {"message": "Draft pull requests are not supported in this repository."})
                    number = fake.add_pr(labels=())
                    fake.issues[number]["draft"] = body.get("draft")
                    return self._send(201, {"number": number})
                m = re.fullmatch(r"/repos/o/r/issues/(\d+)/labels", self.path)
                if m:
                    fake.issues[int(m.group(1))]["labels"].extend(body["labels"])
                    return self._send(200, [])
                if self.path == "/repos/o/r/dispatches":
                    return self._send(204)
                return self._send(404, {"message": "Not Found"})

            def do_PUT(self):
                body = self._body()
                fake.requests.append(("PUT", self.path, body))
                m = re.fullmatch(r"/repos/o/r/contents/(.+)", self.path)
                if m:
                    fake.files[(body["branch"], m.group(1))] = body["content"]
                    return self._send(201, {})
                return self._send(404, {"message": "Not Found"})

        return Handler
