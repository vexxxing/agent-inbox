import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from helpers import FIXTURES, ROOT, fixture_url
from agent_inbox import event as ev
from agent_inbox import watch

T0 = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)


def example(name, fixture):
    spec = yaml.safe_load((ROOT / "watchers" / f"{name}.yml").read_text())
    spec.setdefault("name", name)
    spec["enabled"] = True
    spec["fetch"]["url"] = fixture_url(fixture)
    return spec


class Harness:
    """Runs watchers the way the scheduled workflow does, including a state
    file written to disk and read back between runs."""

    def __init__(self, *specs):
        self.specs = list(specs)
        self.dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.dir.name) / "watchers.json"
        self.posted = []
        self.now = T0

    def point(self, name, fixture):
        for spec in self.specs:
            if spec["name"] == name:
                spec["fetch"]["url"] = fixture_url(fixture)

    def run(self, post=None, advance=dt.timedelta(hours=4), **kw):
        self.now += advance
        state = watch.load_state(self.state_path)
        before = len(self.posted)
        summary = watch.run(self.specs, state, post or self.posted.append, now=self.now,
                            allow_file=True, log=lambda *_: None, **kw)
        watch.save_state(self.state_path, state)
        self.last_summary = summary
        return self.posted[before:]


class ExampleWatcherTests(unittest.TestCase):
    def test_showtimes_fire_once_per_new_showtime(self):
        h = Harness(example("showtimes", "showtimes_none.html"))
        self.assertEqual(h.run(), [])
        h.point("showtimes", "showtimes_two.html")
        events = h.run()
        self.assertEqual([e["payload"]["value"] for e in events], ["Fri 7:00 PM", "Fri 9:30 PM"])
        self.assertEqual({e["type"] for e in events}, {"showtimes.available"})
        self.assertEqual(h.run(), [], "same showtimes never fire twice")
        h.point("showtimes", "showtimes_three.html")
        self.assertEqual([e["payload"]["value"] for e in h.run()], ["Sat 2:15 PM"])
        self.assertEqual(h.run(), [])

    def test_price_threshold_is_edge_triggered_and_rearms(self):
        h = Harness(example("price-drop", "price_high.html"))
        self.assertEqual(h.run(), [])
        h.point("price-drop", "price_low.html")
        first = h.run()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["payload"]["value"], 18.5)
        self.assertEqual(first[0]["payload"]["message"], "Price is under 20")
        self.assertEqual(h.run(), [], "still low: no repeat")
        h.point("price-drop", "price_high.html")
        self.assertEqual(h.run(), [])
        h.point("price-drop", "price_low.html")
        second = h.run()
        self.assertEqual(len(second), 1, "fires again after going back up")
        self.assertNotEqual(first[0]["id"], second[0]["id"])

    def test_rss_primes_then_fires_for_new_matches(self):
        spec = example("rss-keyword", "feed.xml")
        spec["notify"] = "quiet"  # the example holds matches for the digest
        h = Harness(spec)
        self.assertEqual(h.run(), [], "items already in the feed are not news")
        h.point("rss-keyword", "feed_new.xml")
        events = h.run()
        self.assertEqual(len(events), 1)
        item = events[0]["payload"]["item"]
        self.assertEqual(item["title"], "Product LAUNCH next week")
        self.assertEqual(item["link"], "https://news.example.com/3")
        body = ev.to_comment(events[0])
        self.assertEqual(ev.from_comment(body), events[0])
        self.assertEqual(h.run(), [])

    def test_weather_threshold(self):
        h = Harness(example("weather-alert", "weather.json"))
        events = h.run()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["value"], 85)
        self.assertEqual(h.run(), [])
        h.point("weather-alert", "weather_dry.json")
        self.assertEqual(h.run(), [])
        h.point("weather-alert", "weather.json")
        self.assertEqual(len(h.run()), 1)

    def test_examples_ship_disabled_and_valid(self):
        watchers = watch.load_watchers(ROOT / "watchers")
        self.assertEqual({w["name"] for w in watchers},
                         {"showtimes", "price-drop", "rss-keyword", "weather-alert",
                          "order-status"})
        self.assertTrue(all(w["enabled"] is False for w in watchers))
        state = {"version": 1, "watchers": {}}
        summary = watch.run(watchers, state, self.fail, log=lambda *_: None)
        self.assertEqual(summary["checked"], 0, "disabled watchers make no requests")

    def test_only_runs_a_disabled_watcher_for_trying_it_out(self):
        spec = example("price-drop", "price_low.html")
        spec["enabled"] = False
        state = {"version": 1, "watchers": {}}
        posted = []
        watch.run([spec], state, posted.append, allow_file=True, only="price-drop",
                  log=lambda *_: None)
        self.assertEqual(len(posted), 1)


class RunnerTests(unittest.TestCase):
    def spec(self, **over):
        spec = {"name": "w", "every": "30m", "fetch": {"url": fixture_url("price_low.html")},
                "extract": {"css": "[itemprop=price]", "attr": "content"},
                "condition": {"below": 20}, "event": {"type": "price.below"}}
        spec.update(over)
        watch.check_watcher(spec)
        return spec

    def test_every_is_respected_unless_forced(self):
        h = Harness(self.spec(fire="always"))
        self.assertEqual(len(h.run()), 1)
        self.assertEqual(h.run(advance=dt.timedelta(minutes=10)), [])
        self.assertEqual(h.last_summary["skipped"], 1)
        self.assertEqual(len(h.run(advance=dt.timedelta(minutes=1), force=True)), 1)
        self.assertEqual(len(h.run(advance=dt.timedelta(minutes=30))), 1)

    def test_failed_post_is_retried_with_the_same_id(self):
        h = Harness(self.spec())

        def broken(_event):
            raise OSError("GitHub is down")

        self.assertEqual(h.run(post=broken), [])
        self.assertEqual(h.last_summary["errors"], 1)
        retried = h.run()
        self.assertEqual(len(retried), 1)
        again = Harness(self.spec())
        self.assertEqual(again.run()[0]["id"], retried[0]["id"],
                         "same hit, same id, so the relay can dedupe a double post")

    def test_fetch_errors_report_once_after_threshold(self):
        h = Harness(self.spec(fetch={"url": fixture_url("missing.html")}))
        results = [h.run() for _ in range(5)]
        flat = [e for r in results for e in r]
        self.assertEqual([e["type"] for e in flat], ["watcher.error"])
        self.assertEqual(flat[0]["payload"]["consecutive_errors"], 3)
        state = watch.load_state(h.state_path)
        self.assertEqual(state["watchers"]["w"]["errors"], 5)
        h.point("w", "price_low.html")
        self.assertEqual(len(h.run()), 1)
        self.assertEqual(watch.load_state(h.state_path)["watchers"]["w"]["errors"], 0)

    def test_file_urls_need_opt_in(self):
        state = {"version": 1, "watchers": {}}
        summary = watch.run([self.spec()], state, self.fail, log=lambda *_: None)
        self.assertEqual(summary["errors"], 1)
        self.assertIn("allow-file", state["watchers"]["w"]["last_error"])

    def test_changed_condition(self):
        h = Harness(self.spec(condition={"changed": True}))
        self.assertEqual(h.run(), [], "first look only records the value")
        self.assertEqual(h.run(), [])
        h.point("w", "price_high.html")
        changed = h.run()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["payload"]["value"], "24.99")
        self.assertEqual(h.run(), [])

    def test_conditions(self):
        from agent_inbox.extract import Item
        holds = watch.condition_holds
        self.assertTrue(holds({"contains": ["x", "Launch"]}, Item("k", "big LAUNCH")))
        self.assertFalse(holds({"not_contains": "launch"}, Item("k", "big LAUNCH")))
        self.assertTrue(holds({"matches": r"^\d+ seats$"}, Item("k", "12 seats")))
        self.assertTrue(holds({"at_least": 70, "at_most": 90}, Item("k", 85)))
        self.assertFalse(holds({"above": 85}, Item("k", 85)))
        self.assertFalse(holds({"below": 5}, Item("k", "sold out")))
        self.assertTrue(holds({"equals": 3}, Item("k", "3.0")))
        self.assertTrue(holds({"not_equals": "closed"}, Item("k", "open")))
        self.assertTrue(holds({"exists": False}, None))
        self.assertFalse(holds({"exists": True}, None))
        self.assertFalse(holds({"contains": "x"}, None))

    def test_state_is_pruned_and_capped(self):
        h = Harness(self.spec(name="list", extract={"regex": r"(\d+)", "all": True},
                              condition={"exists": True}, prime=False, max_seen=3,
                              fetch={"url": fixture_url("weather.json")}))
        h.run()
        state = watch.load_state(h.state_path)
        self.assertLessEqual(len(state["watchers"]["list"]["seen"]), 3)
        h.specs = [self.spec()]
        h.run()
        self.assertNotIn("list", watch.load_state(h.state_path)["watchers"])

    def test_bad_watchers_are_rejected(self):
        good = self.spec()
        bad = [
            {"fetch": {"url": "ftp://x"}},
            {"extract": {"css": "a", "json": "b"}},
            {"extract": {"css": "a", "selector": "b"}},
            {"condition": {"bigger_than": 3}},
            {"condition": {"matches": "("}},
            {"event": {"type": "has space"}},
            {"every": "soon"},
            {"fire": "sometimes"},
            {"name": "../escape"},
            {"surprise": True},
        ]
        for change in bad:
            spec = copy.deepcopy(good)
            spec.update(change)
            with self.subTest(change=change), self.assertRaises(watch.WatcherError):
                watch.check_watcher(spec)


class CommandLineTests(unittest.TestCase):
    def test_dry_run_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = example("price-drop", "price_low.html")
            (Path(tmp) / "w").mkdir()
            (Path(tmp) / "w" / "price.yml").write_text(yaml.safe_dump(spec))
            cmd = [sys.executable, "-m", "agent_inbox", "watch", "--dir", f"{tmp}/w",
                   "--state", f"{tmp}/state.json", "--dry-run", "--allow-file", "--save-state"]
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            out = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True).stdout
            self.assertIn(ev.MARKER, out)
            self.assertIn('"events": 1', out)
            out2 = subprocess.run(cmd + ["--force"], capture_output=True, text=True, env=env,
                                  check=True).stdout
            self.assertIn('"events": 0', out2)
            parsed = subprocess.run([sys.executable, "-m", "agent_inbox", "parse"], env=env,
                                    input=out.split('{"checked"')[0], capture_output=True,
                                    text=True, check=True).stdout
            self.assertEqual(json.loads(parsed)["type"], "price.below")

    def test_check_command_on_the_repo(self):
        out = subprocess.run([sys.executable, "-m", "agent_inbox", "check"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("5 watchers (0 enabled)", out)


if __name__ == "__main__":
    unittest.main()
