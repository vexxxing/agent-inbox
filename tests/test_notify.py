"""Update levels (notify: quiet, milestones, digest, live) and the daily digest."""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from helpers import ROOT, fixture_url
from test_watch import T0, Harness, example
from agent_inbox import digest
from agent_inbox import event as ev
from agent_inbox import watch

ON = {"enabled": True, "at": "08:00"}
QUIET = lambda *_: None  # noqa: E731


class MilestoneTests(unittest.TestCase):
    def test_order_status_posts_each_step_then_the_delivery(self):
        h = Harness(example("order-status", "order_shipped.json"))
        self.assertEqual(h.run(), [], "the first look only records the status")
        h.point("order-status", "order_out_for_delivery.json")
        step = h.run()
        self.assertEqual([e["type"] for e in step], ["watcher.milestone"])
        self.assertEqual(step[0]["payload"]["value"], "out_for_delivery")
        self.assertEqual(step[0]["payload"]["event_type"], "order.delivered")
        self.assertEqual(h.run(), [], "no change, no news")
        h.point("order-status", "order_delivered.json")
        self.assertEqual([e["type"] for e in h.run()], ["order.delivered"],
                         "the hit itself, not a milestone as well")
        self.assertEqual(h.run(), [])

    def test_quiet_is_the_default_and_posts_only_the_hit(self):
        spec = example("order-status", "order_shipped.json")
        del spec["notify"]
        h = Harness(spec)
        h.run()
        h.point("order-status", "order_out_for_delivery.json")
        self.assertEqual(h.run(), [])
        h.point("order-status", "order_delivered.json")
        self.assertEqual([e["type"] for e in h.run()], ["order.delivered"])

    def test_milestone_ids_are_stable_for_retries(self):
        spec = example("order-status", "order_shipped.json")
        h = Harness(spec)
        h.run()
        h.point("order-status", "order_out_for_delivery.json")

        def broken(_event):
            raise RuntimeError("GitHub is down")
        h.run(post=broken)
        first = h.run()
        self.assertEqual(len(first), 1, "the step is retried after a failed post")
        again = watch.check_one(spec, {"last_seen": watch._h('"shipped"'), "armed": True},
                                Path(ROOT / "tests/fixtures/order_out_for_delivery.json")
                                .read_text(), "2026-10-28T00:00:00Z")
        self.assertEqual(again[0]["id"], first[0]["id"])


class LiveTests(unittest.TestCase):
    def spec(self, **live):
        spec = example("order-status", "order_shipped.json")
        spec.update(notify="live", every="1h", live=live or {"at_json": "tracking.eta"})
        watch.check_watcher(spec)
        return spec

    def test_checks_speed_up_near_the_expected_time(self):
        spec = self.spec()
        eta = dt.datetime(2026, 10, 30, 17, 0, tzinfo=dt.timezone.utc)
        state = {"version": 1, "watchers": {}}
        watch.run([spec], state, QUIET, now=eta - dt.timedelta(hours=10), allow_file=True,
                  log=QUIET)
        wstate = state["watchers"]["order-status"]
        self.assertEqual(wstate["live_at"], "2026-10-30T17:00:00Z")
        far = eta - dt.timedelta(hours=5)
        self.assertEqual(watch.effective_every(spec, wstate, far), 3600)
        near = eta - dt.timedelta(minutes=90)
        self.assertEqual(watch.effective_every(spec, wstate, near), 300)
        late = eta + dt.timedelta(minutes=30)
        self.assertEqual(watch.effective_every(spec, wstate, late), 300, "late counts too")
        self.assertEqual(watch.effective_every(spec, wstate, eta + dt.timedelta(hours=3)), 3600)

    def test_due_follows_the_live_interval(self):
        spec = self.spec()
        eta = dt.datetime(2026, 10, 30, 17, 0, tzinfo=dt.timezone.utc)
        h = Harness(spec)
        h.now = eta - dt.timedelta(minutes=60)
        h.run(advance=dt.timedelta(0))
        h.run(advance=dt.timedelta(minutes=10))
        self.assertEqual(h.last_summary["checked"], 1, "10 minutes is enough inside the window")
        h.now = eta + dt.timedelta(hours=5)
        h.run(advance=dt.timedelta(0))
        h.run(advance=dt.timedelta(minutes=10))
        self.assertEqual(h.last_summary["skipped"], 1, "back to every hour outside it")

    def test_live_also_posts_milestones(self):
        h = Harness(self.spec())
        h.run()
        h.point("order-status", "order_out_for_delivery.json")
        self.assertEqual([e["type"] for e in h.run()], ["watcher.milestone"])

    def test_fixed_time_and_value(self):
        fixed = self.spec(at="2026-10-30T17:00:00Z", window="30m", every="10m")
        at = dt.datetime(2026, 10, 30, 17, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(watch.effective_every(fixed, {}, at - dt.timedelta(minutes=20)), 600)
        self.assertEqual(watch.effective_every(fixed, {}, at - dt.timedelta(minutes=40)), 3600)
        unix = self.spec(at="value")
        wstate = {}
        watch._note_live(unix, wstate, watch.Item("k", int(at.timestamp())), "")
        self.assertEqual(wstate["live_at"], "2026-10-30T17:00:00Z")
        watch._note_live(unix, wstate, watch.Item("k", "soon"), "")
        self.assertNotIn("live_at", wstate, "an unreadable time is forgotten, not guessed")

    def test_bad_live_settings_are_rejected(self):
        base = example("order-status", "order_shipped.json")
        bad = [
            {"notify": "loud"},
            {"live": {"at": "2026-10-30T17:00:00Z"}},  # live without notify: live
            {"notify": "live"},
            {"notify": "live", "live": {"at": "2026-10-30T17:00:00Z", "at_json": "eta"}},
            {"notify": "live", "live": {"at": "2026-10-30T17:00:00"}},  # no zone
            {"notify": "live", "live": {"at": "tomorrow"}},
            {"notify": "live", "live": {"at_json": "eta", "speed": 2}},
            {"notify": "live", "live": {"at_json": "eta", "every": "fast"}},
            {"notify": "live", "live": {"at": "value"},
             "extract": {"css": ".x", "all": True}},
        ]
        for over in bad:
            spec = {**base, **over}
            with self.subTest(over=over), self.assertRaises(watch.WatcherError):
                watch.check_watcher(spec)

    def test_yaml_times_load_as_times(self):
        spec = yaml.safe_load(
            "name: w\nnotify: live\nlive: {at: 2026-10-30T17:00:00Z}\n"
            "fetch: {url: 'https://example.com'}\nextract: {css: '.x'}\n"
            "event: {type: a.b}\n")
        watch.check_watcher(spec)
        at = dt.datetime(2026, 10, 30, 17, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(watch.effective_every(spec, {}, at), 300)


class DigestHoldTests(unittest.TestCase):
    def test_digest_watchers_hold_events_until_the_digest(self):
        h = Harness(example("rss-keyword", "feed.xml"))
        h.run()
        h.point("rss-keyword", "feed_new.xml")
        self.assertEqual(h.run(), [], "held, not posted")
        self.assertEqual(h.last_summary["held"], 1)
        state = watch.load_state(h.state_path)
        posted = []
        event = digest.run(h.specs, state, posted.append, digest.load_config(ROOT / "inbox.yml"),
                           now=dt.datetime(2026, 10, 28, 8, 5, tzinfo=dt.timezone.utc),
                           log=QUIET)
        self.assertEqual(posted, [event])
        self.assertEqual(event["type"], "inbox.digest")
        self.assertEqual(event["payload"]["summary"], "1 watch, 1 new")
        row = event["payload"]["watchers"][0]
        self.assertEqual(row["held"][0]["payload"]["item"]["link"], "https://news.example.com/3")
        self.assertNotIn("held", state["watchers"]["rss-keyword"], "cleared once posted")
        self.assertEqual(ev.from_comment(ev.to_comment(event)), event)

    def test_held_events_are_capped(self):
        spec = example("showtimes", "showtimes_three.html")
        spec["notify"] = "digest"
        state = {"version": 1, "watchers": {"showtimes": {
            "held": [ev.make_event("x.y", "s", {}, id=f"old-{i}") for i in range(49)]}}}
        watch.run([spec], state, QUIET, now=T0, allow_file=True, log=QUIET)
        wstate = state["watchers"]["showtimes"]
        self.assertEqual(len(wstate["held"]), watch.MAX_HELD)
        self.assertEqual(wstate["held_dropped"], 2)
        event = digest.build(watch.load_watchers(ROOT / "watchers")[:0] + [spec], state, T0)
        row = event["payload"]["watchers"][0]
        self.assertEqual(len(row["held"]), digest.MAX_LISTED)
        self.assertEqual(row["held_more"], watch.MAX_HELD - digest.MAX_LISTED + 2)

    def test_errors_still_post_at_once(self):
        spec = example("rss-keyword", "missing.xml")
        spec["notify_errors_after"] = 1
        posted = []
        watch.run([spec], {"version": 1, "watchers": {}}, posted.append, now=T0,
                  allow_file=True, log=QUIET)
        self.assertEqual([e["type"] for e in posted], ["watcher.error"])


class DigestTests(unittest.TestCase):
    def watchers(self):
        price = example("price-drop", "price_high.html")
        feed = example("rss-keyword", "feed.xml")
        off = example("weather-alert", "weather.json")
        off["enabled"] = False
        return [price, feed, off]

    def test_due_once_a_day_after_the_hour(self):
        state = {"version": 1, "watchers": {}}
        day = dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)
        self.assertFalse(digest.is_due(ON, state, day.replace(hour=7, minute=59)))
        self.assertTrue(digest.is_due(ON, state, day.replace(hour=8)))
        digest.clear(state, day.replace(hour=8, minute=20))
        self.assertFalse(digest.is_due(ON, state, day.replace(hour=23)))
        self.assertTrue(digest.is_due(ON, state, day.replace(hour=8) + dt.timedelta(days=1)))

    def test_auto_only_when_a_watcher_uses_digest(self):
        auto = {"enabled": "auto", "at": "08:00"}
        watchers = self.watchers()
        self.assertTrue(digest.wanted(auto, watchers))
        watchers[1]["enabled"] = False
        self.assertFalse(digest.wanted(auto, watchers))
        self.assertTrue(digest.wanted(ON, watchers))
        self.assertFalse(digest.wanted({**ON, "enabled": False}, self.watchers()))

    def test_quiet_day_says_nothing_new(self):
        watchers = self.watchers()
        watchers[1]["notify"] = "quiet"
        state = {"version": 1, "watchers": {}}
        watch.run(watchers, state, QUIET, now=T0, allow_file=True, log=QUIET)
        event = digest.build(watchers, state, T0)
        self.assertEqual(event["payload"]["summary"], "2 watches, nothing new")
        self.assertEqual([r["watcher"] for r in event["payload"]["watchers"]],
                         ["price-drop", "rss-keyword"], "disabled watchers are left out")
        self.assertEqual(event["payload"]["watchers"][0]["last_check"], "2026-10-27T12:00:00Z")

    def test_counts_posted_hits_and_failures(self):
        price = example("price-drop", "price_low.html")
        broken = example("weather-alert", "missing.json")
        state = {"version": 1, "watchers": {}}
        watch.run([price, broken], state, QUIET, now=T0, allow_file=True, log=QUIET)
        payload = digest.build([price, broken], state, T0)["payload"]
        self.assertEqual(payload["summary"], "2 watches, 1 new, 1 failing")
        self.assertIn("error", payload["watchers"][1])

    def test_failed_post_keeps_everything_for_the_next_run(self):
        state = {"version": 1, "watchers": {"rss-keyword": {"held": [
            ev.make_event("feed.match", "watcher:rss-keyword", {"n": 1}, id="h1")]}}}

        def broken(_event):
            raise RuntimeError("GitHub is down")
        now = dt.datetime(2026, 10, 28, 9, tzinfo=dt.timezone.utc)
        with self.assertRaises(RuntimeError):
            digest.run(self.watchers(), state, broken, ON, now=now, log=QUIET)
        self.assertEqual(len(state["watchers"]["rss-keyword"]["held"]), 1)
        self.assertTrue(digest.is_due(ON, state, now))
        posted = []
        digest.run(self.watchers(), state, posted.append, ON, now=now, log=QUIET)
        again = digest.build(self.watchers(), {"version": 1, "watchers": {}}, now)
        self.assertEqual(posted[0]["id"], again["id"], "same day, same id: a retry dedupes")

    def test_forced_digest_gets_its_own_id(self):
        state = {"version": 1, "watchers": {}}
        now = dt.datetime(2026, 10, 28, 9, tzinfo=dt.timezone.utc)
        posted = []
        digest.run(self.watchers(), state, posted.append, ON, now=now, log=QUIET)
        digest.run(self.watchers(), state, posted.append, ON, now=now + dt.timedelta(hours=1),
                   force=True, log=QUIET)
        self.assertEqual(len(posted), 2)
        self.assertNotEqual(posted[0]["id"], posted[1]["id"])

    def test_large_digest_lists_fewer_items_to_fit_one_comment(self):
        held = [ev.make_event("feed.match", "w", {"text": "<@`>" * 900}, id=f"h{i}")
                for i in range(watch.MAX_HELD)]
        state = {"version": 1, "watchers": {"rss-keyword": {"held": held}}}
        event = digest.build_fitting(self.watchers(), state, T0)
        self.assertLessEqual(len(ev.to_comment(event)), ev.MAX_COMMENT_CHARS)
        row = event["payload"]["watchers"][1]
        self.assertEqual(len(row.get("held", [])) + row["held_more"], watch.MAX_HELD)

    def test_config_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.yml"
            for bad in ({"digest": {"at": "8am"}}, {"digest": {"enabled": "yes"}},
                        {"digest": {"when": "08:00"}}, {"digest": ["08:00"]}):
                path.write_text(yaml.safe_dump(bad))
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    digest.load_config(path)
            path.write_text(yaml.safe_dump({"digest": {"enabled": True, "at": "21:30"}}))
            self.assertEqual(digest.load_config(path), {"enabled": True, "at": "21:30"})
        self.assertEqual(digest.load_config(ROOT / "inbox.yml"), digest.DEFAULT_CONFIG)

    def test_digest_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "w").mkdir()
            spec = example("price-drop", "price_high.html")
            (Path(tmp) / "w" / "price.yml").write_text(yaml.safe_dump(spec))
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            out = subprocess.run(
                [sys.executable, "-m", "agent_inbox", "digest", "--dir", f"{tmp}/w",
                 "--state", f"{tmp}/state.json", "--config", f"{tmp}/none.yml", "--dry-run"],
                capture_output=True, text=True, env=env, check=True).stdout
            event = ev.from_comment(out)
            self.assertEqual(event["type"], "inbox.digest")
            self.assertEqual(event["payload"]["summary"], "1 watch, nothing new")
            self.assertFalse((Path(tmp) / "state.json").exists(), "a dry run saves nothing")


if __name__ == "__main__":
    unittest.main()
