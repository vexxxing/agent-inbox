import os
import unittest
from unittest import mock

from helpers import FakeGitHub
from agent_inbox import event as ev
from agent_inbox.github import GitHub, GitHubError


class GitHubClientTests(unittest.TestCase):
    def make(self, **kw):
        fake = FakeGitHub(**kw)
        self.addCleanup(fake.close)
        return fake, GitHub("o/r", "token", api_url=fake.url, retries=0)

    def test_find_inbox_prefers_newest_labelled_pr(self):
        fake, gh = self.make()
        fake.add_pr(labels=("other",))
        old = fake.add_pr()
        new = fake.add_pr()
        fake.issues[new]["state"] = "closed"
        with mock.patch.dict(os.environ, {"INBOX_PR": ""}):
            self.assertEqual(gh.find_inbox(), old)
        with mock.patch.dict(os.environ, {"INBOX_PR": "42"}):
            self.assertEqual(gh.find_inbox(), 42)

    def test_no_inbox_is_a_clear_error(self):
        _, gh = self.make()
        with mock.patch.dict(os.environ, {"INBOX_PR": ""}), \
                self.assertRaisesRegex(GitHubError, "Setup inbox"):
            gh.find_inbox()

    def test_dedupe_reads_the_newest_page(self):
        fake, gh = self.make(page_size=5)
        n = fake.add_pr()
        for i in range(12):  # three pages: 5, 5, 2
            fake.comments[n].append(ev.to_comment(ev.make_event("t", "s", id=f"id-{i}")))
        fake.comments[n].append("an ordinary human comment")
        ids = gh.recent_event_ids(n, limit=6)
        self.assertEqual(ids, {"id-7", "id-8", "id-9", "id-10", "id-11"})
        self.assertFalse(gh.post_event(ev.make_event("t", "s", id="id-11"), number=n))
        self.assertTrue(gh.post_event(ev.make_event("t", "s", id="id-99"), number=n))
        self.assertEqual(len(fake.comments[n]), 14)

    def test_setup_creates_everything_once(self):
        fake, gh = self.make()
        with mock.patch.dict(os.environ, {"INBOX_PR": ""}):
            number = gh.setup_inbox()
            self.assertTrue(fake.issues[number]["draft"])
            self.assertIn("agent-inbox", fake.issues[number]["labels"])
            self.assertIn("agent-inbox", fake.refs)
            self.assertIn(("agent-inbox", ".agent-inbox/INBOX.md"), fake.files)
            self.assertEqual(gh.setup_inbox(), number, "second run finds the same inbox")
            self.assertEqual(sum(1 for m, p, _ in fake.requests if p == "/repos/o/r/pulls"), 1)

    def test_setup_falls_back_when_drafts_are_unavailable(self):
        fake, gh = self.make(draft_supported=False)
        with mock.patch.dict(os.environ, {"INBOX_PR": ""}):
            number = gh.setup_inbox()
        self.assertFalse(fake.issues[number]["draft"])

    def test_errors_and_input_checks(self):
        _, gh = self.make()
        with self.assertRaises(GitHubError) as ctx:
            gh.request("GET", "/repos/o/r/nothing")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(ValueError):
            GitHub("not a repo", "t")
        with self.assertRaises(ValueError):
            GitHub("o/r", "")

    def test_send_command_makes_a_dispatch(self):
        from agent_inbox.__main__ import main
        fake, _ = self.make()
        env = {"INBOX_DISPATCH_TOKEN": "t", "INBOX_SECRET": "abc", "GITHUB_API_URL": fake.url}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(main(["send", "--repo", "o/r", "--type", "x.y",
                                   "--payload", '{"a": 1}', "--id", "i-1"]), 0)
        method, path, body = fake.requests[-1]
        self.assertEqual((method, path), ("POST", "/repos/o/r/dispatches"))
        self.assertEqual(body, {"event_type": "x.y", "client_payload": {
            "payload": {"a": 1}, "source": "cli", "id": "i-1", "secret": "abc"}})


if __name__ == "__main__":
    unittest.main()
