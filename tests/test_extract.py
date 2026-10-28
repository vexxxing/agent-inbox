import unittest

from helpers import FIXTURES
from agent_inbox.extract import ExtractError, extract, parse_number, parse_path


def read(name):
    return (FIXTURES / name).read_text()


class ExtractTests(unittest.TestCase):
    def test_css_text_all(self):
        items = extract({"css": ".showtime", "all": True}, read("showtimes_three.html"))
        self.assertEqual([i.value for i in items], ["Fri 7:00 PM", "Fri 9:30 PM", "Sat 2:15 PM"])

    def test_css_first_and_attribute_number(self):
        items = extract({"css": "[itemprop=price]", "attr": "content", "number": True},
                        read("price_low.html"))
        self.assertEqual([i.value for i in items], [18.5])
        self.assertEqual(extract({"css": ".showtime"}, read("showtimes_none.html")), [])

    def test_json_paths(self):
        self.assertEqual(parse_path('$.a.b[0]["odd key"][*]'), ["a", "b", 0, "odd key", "*"])
        doc = read("weather.json")
        self.assertEqual(extract({"json": "daily.precipitation_probability_max[1]"}, doc)[0].value, 85)
        self.assertEqual(extract({"json": "daily.precipitation_probability_max[-1]"}, doc)[0].value, 85)
        many = extract({"json": "daily.time[*]", "all": True}, doc)
        self.assertEqual([i.value for i in many], ["2026-10-27", "2026-10-28"])
        self.assertEqual(extract({"json": "nope.missing[3]"}, doc), [])
        with self.assertRaises(ExtractError):
            extract({"json": "a..b"}, doc)
        with self.assertRaises(ExtractError):
            extract({"json": "a"}, "<html>")

    def test_rss_and_atom(self):
        items = extract({"rss": True}, read("feed.xml"))
        self.assertEqual([i.key for i in items], ["n1", "n2"])
        self.assertEqual(items[0].extra["item"]["link"], "https://news.example.com/1")
        atom = extract({"rss": True, "field": "any"}, read("atom.xml"))
        self.assertEqual(atom[0].key, "tag:a")
        self.assertEqual(atom[0].value, "Launch notes hello")
        self.assertEqual(atom[0].extra["item"]["link"], "https://blog.example.com/a")

    def test_rss_refuses_entities(self):
        with self.assertRaises(ExtractError):
            extract({"rss": True}, read("evil.xml"))

    def test_regex(self):
        text = "Tickets: 12 left. VIP: 3 left."
        self.assertEqual(extract({"regex": r"(\d+) left"}, text)[0].value, "12")
        self.assertEqual(len(extract({"regex": r"(\d+) left", "all": True}, text)), 2)
        with self.assertRaises(ExtractError):
            extract({"regex": "("}, text)

    def test_exactly_one_kind(self):
        with self.assertRaises(ExtractError):
            extract({"css": "a", "regex": "b"}, "")
        with self.assertRaises(ExtractError):
            extract({}, "")

    def test_numbers(self):
        self.assertEqual(parse_number("$1,299.00"), 1299.0)
        self.assertEqual(parse_number("-3.5 C"), -3.5)
        self.assertEqual(parse_number(7), 7.0)
        self.assertIsNone(parse_number("sold out"))
        self.assertIsNone(parse_number(True))


if __name__ == "__main__":
    unittest.main()
