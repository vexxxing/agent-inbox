"""Static checks on the GitHub Actions workflows, plus a real run of the
state script against a local git remote."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from helpers import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML reads the bare key `on` as True (YAML 1.1); GitHub reads "on".
    data["on"] = data.pop(True, data.get("on"))
    return data


class WorkflowTests(unittest.TestCase):
    def test_all_parse_and_have_jobs(self):
        files = sorted(p.name for p in WORKFLOWS.glob("*.yml"))
        self.assertEqual(files, ["relay.yml", "setup.yml", "tests.yml", "watchers.yml"])
        for name in files:
            with self.subTest(name=name):
                wf = load(name)
                self.assertIn("on", wf)
                self.assertIn("permissions", wf, "every workflow sets least privilege")
                for job in wf["jobs"].values():
                    self.assertIn("timeout-minutes", job)
                    for step in job["steps"]:
                        self.assertTrue("uses" in step or "run" in step)
                        if "uses" in step:
                            self.assertRegex(step["uses"], r"^[\w-]+/[\w-]+@v\d+$")

    def test_no_untrusted_expressions_inside_scripts(self):
        """Event data must reach scripts through env, never ${{ }} in run:,
        or a crafted payload could inject shell commands."""
        for path in WORKFLOWS.glob("*.yml"):
            for job in load(path.name)["jobs"].values():
                for step in job["steps"]:
                    with self.subTest(file=path.name, step=step.get("name")):
                        self.assertNotIn("${{", step.get("run", ""))

    def test_relay(self):
        wf = load("relay.yml")
        self.assertIn("repository_dispatch", wf["on"])
        self.assertIn("workflow_dispatch", wf["on"])
        self.assertNotIn("concurrency", wf, "a concurrency group would drop queued events")
        self.assertEqual(wf["permissions"]["contents"], "read")
        env = wf["jobs"]["relay"]["steps"][-1]["env"]
        self.assertEqual(env["INBOX_SECRET"], "${{ secrets.INBOX_SECRET }}")

    def test_watchers(self):
        wf = load("watchers.yml")
        cron = wf["on"]["schedule"][0]["cron"]
        self.assertEqual(len(cron.split()), 5)
        self.assertEqual(wf["jobs"]["watch"]["if"], "vars.WATCHERS_ENABLED == 'true'",
                         "off by default so a fresh copy spends no minutes")
        self.assertFalse(wf["concurrency"]["cancel-in-progress"])
        steps = [s.get("run", "") for s in wf["jobs"]["watch"]["steps"]]
        self.assertTrue(any("state.sh load" in s for s in steps))
        save = wf["jobs"]["watch"]["steps"][-1]
        self.assertEqual((save["if"], save["run"]), ("always()", "bash scripts/state.sh save"))

    def test_tests_skip_the_inbox_pr(self):
        wf = load("tests.yml")
        self.assertIn(".agent-inbox/**", wf["on"]["pull_request"]["paths-ignore"])


class StateScriptTests(unittest.TestCase):
    """scripts/state.sh against a throwaway bare repository."""

    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout

    def test_load_save_round_trip(self):
        script = str(ROOT / "scripts" / "state.sh")
        with tempfile.TemporaryDirectory() as tmp:
            remote, work = Path(tmp) / "remote.git", Path(tmp) / "work"
            self.git("init", "--bare", "-q", str(remote), cwd=tmp)
            self.git("init", "-q", "-b", "main", str(work), cwd=tmp)
            self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit",
                     "--allow-empty", "-qm", "init", cwd=work)
            self.git("remote", "add", "origin", str(remote), cwd=work)
            self.git("push", "-q", "origin", "main", cwd=work)

            def sh(cmd):
                return subprocess.run(["bash", script, cmd], cwd=work, check=True,
                                      capture_output=True, text=True).stdout

            self.assertIn("starting fresh", sh("load"))
            self.assertIn("nothing to save", sh("save"))
            (work / ".state").mkdir(exist_ok=True)
            (work / ".state" / "watchers.json").write_text(json.dumps({"n": 1}))
            self.assertIn("saved", sh("save"))
            self.assertIn("loaded", sh("load"))
            self.assertEqual(json.loads((work / ".state" / "watchers.json").read_text()), {"n": 1})
            self.assertIn("unchanged", sh("save"))
            (work / ".state" / "watchers.json").write_text(json.dumps({"n": 2}))
            sh("save")
            sh("load")
            self.assertEqual(json.loads((work / ".state" / "watchers.json").read_text()), {"n": 2})
            log = self.git("log", "--oneline", "agent-inbox-state", cwd=remote)
            self.assertEqual(len(log.strip().splitlines()), 1, "state branch never grows")
            self.assertEqual(self.git("log", "--oneline", "main", cwd=remote).count("\n"), 1,
                             "main is untouched")
            files = self.git("ls-tree", "--name-only", "agent-inbox-state", cwd=remote)
            self.assertEqual(files.split(), ["watchers.json"])


class DocsTests(unittest.TestCase):
    def test_required_files(self):
        for name in ("README.md", "SPEC.md", "SECURITY.md", "HANDLERS.md", "LIMITS.md", "LICENSE",
                     "NOTICE", "requirements.txt", "inbox.yml"):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn("Apache License", (ROOT / "LICENSE").read_text())
        self.assertIn("Version 2.0, January 2004", (ROOT / "LICENSE").read_text())

    def test_readme_curl_is_valid_json(self):
        readme = (ROOT / "README.md").read_text()
        bodies = re.findall(r"-d '(\{.*?\})'", readme, re.DOTALL)
        self.assertTrue(bodies)
        for body in bodies:
            data = json.loads(body)
            self.assertIn("event_type", data)
            self.assertIn("secret", data["client_payload"])


if __name__ == "__main__":
    unittest.main()
