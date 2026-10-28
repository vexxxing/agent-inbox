import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT, FakeGitHub
from agent_inbox import event as ev
from agent_inbox import relay
from agent_inbox.github import GitHub

SECRET = "s3cret-value"


def dispatch(event_type="order.shipped", **client):
    client.setdefault("secret", SECRET)
    return {"action": event_type, "client_payload": client}


class RelayValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = relay.load_config(ROOT / "inbox.yml")

    def build(self, request, name="repository_dispatch", secret=SECRET, **config):
        return relay.event_from_request(name, request, {**self.config, **config}, secret)

    def test_good_dispatch(self):
        e = self.build(dispatch(source="shop", id="order-1234-shipped",
                                payload={"order": "1234", "token": "x",
                                         "nested": [{"Password": "y", "ok": 1}]}))
        self.assertEqual((e["type"], e["source"], e["id"]),
                         ("order.shipped", "shop", "order-1234-shipped"))
        self.assertEqual(e["payload"], {"order": "1234", "nested": [{"ok": 1}]},
                         "sensitive-looking keys are dropped at any depth")
        self.assertNotIn(SECRET, json.dumps(e))

    def test_defaults(self):
        e = self.build(dispatch())
        self.assertEqual(e["source"], "webhook")
        self.assertEqual(e["payload"], {})
        self.assertTrue(e["id"].startswith("e-"))

    def test_secret_is_required(self):
        for request in (dispatch(secret="wrong"), dispatch(secret=None),
                        {"action": "x", "client_payload": {}}):
            with self.subTest(request=request), self.assertRaises(relay.RelayRejected):
                self.build(request)
        with self.assertRaisesRegex(relay.RelayRejected, "INBOX_SECRET is not set"):
            self.build(dispatch(), secret="")
        self.build({"action": "x", "client_payload": {}}, secret="", require_secret=False)

    def test_filters(self):
        cfg = dict(allow_types=["order.*"], deny_types=["order.cancelled"],
                   allow_sources=["shop"])
        self.build(dispatch(source="shop"), **cfg)
        for request in (dispatch("deploy.done", source="shop"),
                        dispatch("order.cancelled", source="shop"),
                        dispatch(source="someone-else")):
            with self.subTest(request=request), self.assertRaises(relay.RelayRejected):
                self.build(request, **cfg)

    def test_bad_shapes(self):
        for request in (dispatch("has spaces"), dispatch(source="@x"),
                        dispatch(id="not ok"), dispatch(payload={"x": "y" * 20000}),
                        {"action": "a", "client_payload": ["list"]}):
            with self.subTest(request=str(request)[:60]), self.assertRaises(relay.RelayRejected):
                self.build(request)
        with self.assertRaises(relay.RelayRejected):
            self.build({}, name="push")

    def test_manual_runs(self):
        req = {"inputs": {"type": "test.ping", "source": "", "payload": '{"hello": "world"}'}}
        e = self.build(req, name="workflow_dispatch", secret="")
        self.assertEqual((e["type"], e["source"], e["payload"]),
                         ("test.ping", "manual", {"hello": "world"}))
        with self.assertRaises(relay.RelayRejected):
            self.build({"inputs": {"type": "t", "payload": "{oops"}}, name="workflow_dispatch")
        with self.assertRaises(relay.RelayRejected):
            self.build(req, name="workflow_dispatch", allow_manual=False)

    def test_config_typos_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.yml"
            path.write_text("relay:\n  alow_types: ['x']\n")
            with self.assertRaises(ValueError):
                relay.load_config(path)
        self.assertEqual(relay.load_config("/nonexistent/inbox.yml"), relay.DEFAULT_CONFIG)


class RelayEndToEndTests(unittest.TestCase):
    """The relay entry point against a fake GitHub, as the workflow runs it."""

    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.inbox = self.fake.add_pr()
        self.gh = GitHub("o/r", "token", api_url=self.fake.url, retries=0)

    def run_relay(self, request, name="repository_dispatch", secret=SECRET):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(request, fh)
        self.addCleanup(os.unlink, fh.name)
        env = {"GITHUB_EVENT_NAME": name, "GITHUB_EVENT_PATH": fh.name,
               "INBOX_SECRET": secret, "INBOX_PR": ""}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            code = relay.main_relay(self.gh, config_path=str(ROOT / "inbox.yml"))
        return code, out.getvalue()

    def test_posts_and_dedupes(self):
        code, out = self.run_relay(dispatch(id="evt-1", payload={"n": 1}))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.comments[self.inbox]), 1)
        posted = ev.from_comment(self.fake.comments[self.inbox][0])
        self.assertEqual(posted["payload"], {"n": 1})
        code, out = self.run_relay(dispatch(id="evt-1", payload={"n": 1}))
        self.assertIn("skipped duplicate", out)
        self.assertEqual(len(self.fake.comments[self.inbox]), 1)

    def test_rejection_does_not_echo_the_request(self):
        code, out = self.run_relay(dispatch(secret="leaked-guess", payload={"x": "hostile"}))
        self.assertEqual(code, 1)
        self.assertIn("::error::event rejected", out)
        self.assertNotIn("leaked-guess", out)
        self.assertNotIn("hostile", out)
        self.assertEqual(self.fake.comments[self.inbox], [])


if __name__ == "__main__":
    unittest.main()
