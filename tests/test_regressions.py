"""One test per bug found in the pre-release review, so none comes back."""

import datetime as dt
import email.message
import http.client
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from helpers import ROOT
from agent_inbox import event as ev
from agent_inbox import relay, watch
from agent_inbox.extract import extract
from agent_inbox.github import GitHub, GitHubError

T0 = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)


def spec(**over):
    base = {"name": "w", "fetch": {"url": "https://example.com/"},
            "extract": {"regex": r"price (\S+)"}, "condition": {"below": 20},
            "event": {"type": "price.below"}}
    base.update(over)
    watch.check_watcher(base)
    return base


def run(specs, state, pages, now=T0, **kw):
    """Run watchers against canned pages: pages maps watcher name -> text or
    an exception to raise."""
    posted = []

    def fetcher(fetch_spec, allow_file=False):
        page = pages[fetch_spec["url"]]
        if isinstance(page, Exception):
            raise page
        return page

    summary = watch.run(specs, state, posted.append, now=now, fetcher=fetcher,
                        log=lambda *_: None, **kw)
    return posted, summary


def fresh():
    return {"version": 1, "watchers": {}}


class WatcherRegressionTests(unittest.TestCase):
    def test_blocked_page_does_not_rearm_and_refire(self):
        # Was: an empty or blocked page counted as "price not low", re-armed
        # the watcher, and the same low price fired again when the page came back.
        s, state = spec(), fresh()
        url = s["fetch"]["url"]
        self.assertEqual(len(run([s], state, {url: "price 18"})[0]), 1)
        posted, summary = run([s], state, {url: "Access denied"}, now=T0 + dt.timedelta(hours=1))
        self.assertEqual((posted, summary["errors"]), ([], 1))
        posted, _ = run([s], state, {url: "price 18"}, now=T0 + dt.timedelta(hours=2))
        self.assertEqual(posted, [], "still the same low price: no second alert")

    def test_exists_false_still_means_absent_is_a_hit(self):
        s, state = spec(condition={"exists": False}, event={"type": "sold.out"}), fresh()
        posted, summary = run([s], state, {s["fetch"]["url"]: "nothing here"})
        self.assertEqual((len(posted), summary["errors"]), (1, 0))

    def test_changed_event_id_survives_a_retry(self):
        # Was: the id held the run's clock, so a retry after a failed post
        # got a new id and dedupe could not catch the repeat.
        s = spec(condition={"changed": True}, event={"type": "value.changed"})
        url = s["fetch"]["url"]
        ids = []
        for attempt in range(2):
            state = fresh()
            run([s], state, {url: "price 1"})
            posted, _ = run([s], state, {url: "price 2"}, now=T0 + dt.timedelta(hours=1 + attempt))
            ids.append(posted[0]["id"])
        self.assertEqual(ids[0], ids[1])

    def test_changed_back_and_forth_gets_fresh_ids(self):
        s, state = spec(condition={"changed": True}, event={"type": "v.c"}), fresh()
        url, ids = s["fetch"]["url"], []
        for i, page in enumerate(["price 1", "price 2", "price 1", "price 2"]):
            posted, _ = run([s], state, {url: page}, now=T0 + dt.timedelta(hours=i))
            ids += [e["id"] for e in posted]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3, "A->B twice is two events, not one")

    def test_equals_on_text_that_is_not_a_number(self):
        # Was: "sold out" equals "available" passed, because both parse to no number.
        item = watch.Item("k", "sold out")
        self.assertFalse(watch.condition_holds({"equals": "available"}, item))
        self.assertTrue(watch.condition_holds({"not_equals": "available"}, item))
        self.assertTrue(watch.condition_holds({"equals": "sold out"}, item))

    def test_equals_and_not_equals_agree_on_numbers(self):
        item = watch.Item("k", "20.0")
        self.assertTrue(watch.condition_holds({"equals": 20}, item))
        self.assertFalse(watch.condition_holds({"not_equals": 20}, item))

    def test_late_cron_does_not_skip_every_other_run(self):
        # Was: 60 s of slack, so a 30m watcher seen 26 minutes after a late
        # run was skipped, halving how often it ran.
        state = {"last_run": "2026-10-27T12:05:00Z"}
        self.assertTrue(watch._is_due(state, 1800, dt.datetime(2026, 10, 27, 12, 31, tzinfo=dt.timezone.utc)))
        self.assertFalse(watch._is_due(state, 1800, dt.datetime(2026, 10, 27, 12, 20, tzinfo=dt.timezone.utc)))

    def test_empty_first_look_does_not_prime_a_list(self):
        # Was: a blocked first fetch primed nothing but marked the watcher as
        # primed, so every item already on the page fired on the next run.
        s = spec(extract={"regex": r"show (\S+)", "all": True}, condition={"exists": True},
                 event={"type": "showtimes.available"})
        url, state = s["fetch"]["url"], fresh()
        run([s], state, {url: "Access denied"})
        posted, _ = run([s], state, {url: "show 7pm show 9pm"}, now=T0 + dt.timedelta(hours=1))
        self.assertEqual(posted, [], "the first real look still only primes")

    def test_flood_of_new_items_is_spread_over_runs(self):
        # Was: a feed that suddenly listed 200 new items posted 200 comments in
        # one run, well past GitHub's 80-a-minute content limit.
        s = spec(extract={"regex": r"item (\d+)", "all": True}, condition={"exists": True},
                 event={"type": "feed.new"}, prime=False, max_events=5)
        url, state = s["fetch"]["url"], fresh()
        page = " ".join(f"item {i}" for i in range(12))
        counts = [len(run([s], state, {url: page}, now=T0 + dt.timedelta(hours=h))[0])
                  for h in range(4)]
        self.assertEqual(counts, [5, 5, 2, 0])

    def test_one_broken_connection_does_not_stop_other_watchers(self):
        # Was: http.client errors (not OSError) escaped and ended the whole run.
        a = spec(name="a", fetch={"url": "https://a.example/"})
        b = spec(name="b", fetch={"url": "https://b.example/"})
        posted, summary = run([a, b], fresh(), {
            "https://a.example/": http.client.IncompleteRead(b"partial"),
            "https://b.example/": "price 5"})
        self.assertEqual((summary["errors"], len(posted)), (1, 1))

    def test_unknown_only_name_is_an_error(self):
        with self.assertRaisesRegex(watch.WatcherError, "no watcher named"):
            run([spec()], fresh(), {}, only="pirce-drop")

    def test_bad_number_in_condition_is_caught_at_check_time(self):
        with self.assertRaisesRegex(watch.WatcherError, "must be a number"):
            spec(condition={"below": "twenty"})

    def test_bad_counts_are_caught_at_check_time(self):
        for key, value in (("max_events", 0), ("max_seen", "lots"), ("notify_errors_after", -1)):
            with self.assertRaisesRegex(watch.WatcherError, "whole number"):
                spec(**{key: value})

    def test_misspelt_fetch_key_is_caught(self):
        with self.assertRaisesRegex(watch.WatcherError, "unknown fetch keys"):
            spec(fetch={"url": "https://example.com/", "header": {"A": "b"}})

    def test_headers_cannot_read_workflow_tokens(self):
        # Was: "Bearer ${GITHUB_TOKEN}" would send the workflow's token to any site.
        with self.assertRaisesRegex(watch.WatcherError, "may not read"):
            spec(fetch={"url": "https://example.com/",
                        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}})
        with mock.patch.dict(os.environ, {"INBOX_TOKEN": "x"}), \
                self.assertRaisesRegex(watch.WatcherError, "may not read"):
            watch.fetch({"url": "https://example.com/",
                         "headers": {"Authorization": "${INBOX_TOKEN}"}})

    def test_missing_header_variable_is_an_error_not_blank(self):
        os.environ.pop("SHOP_API_KEY", None)
        with self.assertRaisesRegex(watch.WatcherError, "not set"):
            watch.fetch({"url": "https://example.com/", "headers": {"X-Key": "${SHOP_API_KEY}"}})

    def test_unknown_charset_falls_back_to_utf8(self):
        headers = email.message.Message()
        headers["Content-Type"] = "text/html; charset=no-such-charset"

        class Resp(io.BytesIO):
            def __init__(self):
                super().__init__("price 18 €".encode())
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            self.assertEqual(watch.fetch({"url": "https://example.com/"}), "price 18 €")


class FeedRegressionTests(unittest.TestCase):
    def test_feed_declaring_latin1_is_not_garbled(self):
        # Was: decoded text was re-encoded as UTF-8 and then read as Latin-1.
        feed = ('<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel>'
                "<item><title>Café opens</title><guid>1</guid></item></channel></rss>")
        self.assertEqual(extract({"rss": True}, feed)[0].value, "Café opens")

    def test_atom_entry_link_is_the_page_not_self(self):
        feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>t</title>'
                '<link rel="self" href="https://x.example/api/1"/>'
                '<link rel="alternate" href="https://x.example/posts/1"/></entry></feed>')
        self.assertEqual(extract({"rss": True}, feed)[0].extra["item"]["link"],
                         "https://x.example/posts/1")


class EventRegressionTests(unittest.TestCase):
    def test_time_without_zone_is_rejected(self):
        with self.assertRaisesRegex(ev.EventError, "time zone"):
            ev.make_event("a.b", "s", time="2026-10-27T21:40:00")

    def test_payload_that_escapes_past_comment_limit_is_refused_by_relay(self):
        # Was: 16 KB of "<" passed the size check, became ~96 KB once escaped,
        # and GitHub rejected the comment with a bare 422.
        config = relay.load_config(ROOT / "inbox.yml")
        request = {"action": "big", "client_payload": {"secret": "s", "payload": "<" * 15000}}
        with self.assertRaisesRegex(relay.RelayRejected, "characters"):
            relay.event_from_request("repository_dispatch", request, config, "s")


class GitHubRegressionTests(unittest.TestCase):
    def _error(self, code, body, headers=None):
        msg = email.message.Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code, "err", msg,
                                      io.BytesIO(body.encode()))

    def test_secondary_rate_limit_waits_as_told(self):
        # Was: retried after 1, 2 and 4 seconds, well inside GitHub's pause.
        gh = GitHub("o/r", "t", retries=1)
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"{}"
        ok.__enter__.return_value.headers = {}
        calls = [self._error(403, "You have exceeded a secondary rate limit",
                             {"Retry-After": "45"}), ok]
        with mock.patch("urllib.request.urlopen", side_effect=calls), \
                mock.patch("time.sleep") as sleep:
            gh.request("GET", "/x")
        sleep.assert_called_once_with(45)

    def test_setup_explains_the_actions_pr_setting(self):
        gh = GitHub("o/r", "t", retries=0)

        def fake_request(method, path, body=None, **kw):
            if path.endswith("/pulls"):
                raise GitHubError(403, "GitHub Actions is not permitted to create pull requests.")
            if method == "GET" and "/issues?" in path:
                return [], ""
            if method == "GET" and path == "/repos/o/r":
                return {"default_branch": "main"}, ""
            if method == "GET":
                return {"object": {"sha": "abc"}}, ""
            return {}, ""

        with mock.patch.object(gh, "request", side_effect=fake_request), \
                mock.patch.dict(os.environ, {"INBOX_PR": ""}), \
                self.assertRaisesRegex(GitHubError, "Allow GitHub Actions"):
            gh.setup_inbox()


if __name__ == "__main__":
    unittest.main()
