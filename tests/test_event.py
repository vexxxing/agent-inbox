import json
import unittest

from helpers import ROOT  # noqa: F401  (puts the package on sys.path)
from agent_inbox import event as ev


class EventFormatTests(unittest.TestCase):
    def test_round_trip(self):
        e = ev.make_event("price.below", "watcher:shop", {"value": 18.5})
        body = ev.to_comment(e)
        self.assertTrue(body.startswith("**price.below** from `watcher:shop` at "))
        self.assertEqual(ev.from_comment(body), e)

    def test_hostile_payload_cannot_break_out(self):
        nasty = "```\n</details><img src=x> @everyone & <!-- hi -->"
        e = ev.make_event("t.x", "s", {"text": nasty, nasty: 1})
        body = ev.to_comment(e)
        fenced = body.split("```json\n", 1)[1]
        self.assertEqual(fenced.count("```"), 1, "only the closing fence remains")
        for bad in ("<", ">", "@", "&", "`"):
            self.assertNotIn(bad, fenced.split("\n```")[0])
        self.assertEqual(ev.from_comment(body)["payload"]["text"], nasty)

    def test_html_escaped_in_transit(self):
        # Claude's PR wake delivers { and } as &#123; and &#125;.
        nasty = "a & b <c> @d"
        e = ev.make_event("test.timer", "live-test", {"text": nasty})
        body = ev.to_comment(e).replace("{", "&#123;").replace("}", "&#125;")
        self.assertEqual(ev.from_comment(body), e)

    def test_ordinary_comments_are_ignored(self):
        self.assertIsNone(ev.from_comment("LGTM"))
        self.assertIsNone(ev.from_comment(ev.MARKER + "\nno fence"))
        self.assertIsNone(ev.from_comment(ev.MARKER + "\n```json\n{not json}\n```"))
        self.assertIsNone(ev.from_comment(None))

    def test_tampered_event_is_ignored(self):
        e = ev.make_event("a.b", "s", {})
        body = ev.to_comment(e).replace('"agent-inbox/1"', '"other/9"')
        self.assertIsNone(ev.from_comment(body))

    def test_validation(self):
        bad = [
            dict(type="has space", source="s"),
            dict(type="t", source="@mention"),
            dict(type="t", source="s", id="no spaces please"),
            dict(type="", source="s"),
            dict(type="t", source="s", time="yesterday"),
            dict(type="x" * 101, source="s"),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ev.EventError):
                ev.make_event(payload={}, **kwargs)
        with self.assertRaises(ev.EventError):
            ev.make_event("t", "s", {"big": "x" * 100}, max_payload_bytes=50)
        with self.assertRaises(ev.EventError):
            ev.make_event("t", "s", {"bad": object()})
        with self.assertRaises(ev.EventError):
            ev.validate({**ev.make_event("t", "s"), "extra": 1})

    def test_stable_ids(self):
        a = ev.stable_id("w-x", "k", 1)
        self.assertEqual(a, ev.stable_id("w-x", "k", 1))
        self.assertNotEqual(a, ev.stable_id("w-x", "k", 2))
        self.assertRegex(ev.stable_id("weird name!", "k"), ev.ID_RE)
        ev.make_event("t", "s", id=a)  # usable as an event id

    def test_spec_example_parses(self):
        spec = (ROOT / "SPEC.md").read_text()
        comment = spec.split("<!-- example-start -->", 1)[1].split("<!-- example-end -->", 1)[0]
        event = ev.from_comment(comment.strip("\n"))
        self.assertIsNotNone(event, "the example comment in SPEC.md must parse")
        self.assertEqual(event["type"], "showtimes.available")
        json.dumps(event)


if __name__ == "__main__":
    unittest.main()
