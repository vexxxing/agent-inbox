"""Run the workflows' commands in order, as GitHub Actions would, against a
fake GitHub API and a local git remote: setup, a manual relay event, a
dispatch with a retry, and two scheduled watcher runs with state carried
between them on the state branch."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from helpers import ROOT, FakeGitHub, fixture_url
from agent_inbox import event as ev


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.addCleanup(self.fake.close)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = self.tmp / "repo"
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(
            "__pycache__", ".state", ".git", "tests"))
        remote = self.tmp / "remote.git"
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True,  # noqa: E731
                                        capture_output=True)
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "template")
        git("remote", "add", "origin", str(remote))
        git("push", "-q", "origin", "main")
        self.env = {**os.environ, "PYTHONPATH": str(self.repo), "GITHUB_API_URL": self.fake.url,
                    "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t", "INBOX_TOKEN": "",
                    "INBOX_PR": "", "INBOX_SECRET": "shh"}

    def sh(self, *cmd, env=None):
        return subprocess.run(list(cmd), cwd=self.repo, env={**self.env, **(env or {})},
                              capture_output=True, text=True, check=True).stdout

    def relay(self, name, request):
        path = self.tmp / "event.json"
        path.write_text(json.dumps(request))
        return subprocess.run([sys.executable, "-m", "agent_inbox", "relay"], cwd=self.repo,
                              env={**self.env, "GITHUB_EVENT_NAME": name,
                                   "GITHUB_EVENT_PATH": str(path)},
                              capture_output=True, text=True)

    def events(self, number):
        return [ev.from_comment(b) for b in self.fake.comments[number]]

    def test_full_flow(self):
        out = self.sh(sys.executable, "-m", "agent_inbox", "setup")
        number = int(out.strip().rsplit("#", 1)[1])

        # Manual "Run workflow" with the default inputs.
        r = self.relay("workflow_dispatch", {"inputs": {
            "type": "test.ping", "source": "manual", "payload": '{"hello": "world"}', "id": ""}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # A webhook, then the sender retries it, then a forged one.
        hook = {"action": "order.shipped", "client_payload": {
            "secret": "shh", "source": "shop", "id": "order-1", "payload": {"order": "1"}}}
        self.assertEqual(self.relay("repository_dispatch", hook).returncode, 0)
        self.assertIn("skipped duplicate", self.relay("repository_dispatch", hook).stdout)
        forged = json.loads(json.dumps(hook))
        forged["client_payload"]["secret"] = "guess"
        self.assertEqual(self.relay("repository_dispatch", forged).returncode, 1)

        # Turn on the price watcher, pointed at a fixture, and run the
        # scheduled job's three steps twice.
        spec_path = self.repo / "watchers" / "price-drop.yml"
        spec = yaml.safe_load(spec_path.read_text())
        spec.update(enabled=True)
        spec["fetch"]["url"] = fixture_url("price_low.html")
        spec_path.write_text(yaml.safe_dump(spec))
        for _ in range(2):
            self.sh("bash", "scripts/state.sh", "load")
            self.sh(sys.executable, "-m", "agent_inbox", "watch", "--state",
                    ".state/watchers.json", "--allow-file", "--force")
            self.sh("bash", "scripts/state.sh", "save")
            shutil.rmtree(self.repo / ".state")  # a fresh runner has no local state

        types = [e["type"] for e in self.events(number)]
        self.assertEqual(types, ["test.ping", "order.shipped", "price.below"],
                         "one ping, one order despite the retry, one price hit across two runs")
        self.assertTrue(all(e is not None for e in self.events(number)))


if __name__ == "__main__":
    unittest.main()
