"""Run with: python -m unittest test_newsroom -v"""
import random
import unittest

import newsroom


class Staffing(unittest.TestCase):
    def test_small_paper(self):
        people = newsroom.staff(2, rng=random.Random(0))
        self.assertEqual([p["model"] for p in people], ["opus", "sonnet", "haiku", "haiku"])
        editor, senior = people[0], people[1]
        self.assertIsNone(editor["reportsTo"])
        self.assertEqual(senior["reportsTo"], editor["sessionId"])
        self.assertTrue(all(p["reportsTo"] == senior["sessionId"] for p in people[2:]))

    def test_big_paper_has_two_seniors_and_unique_names(self):
        people = newsroom.staff(60, rng=random.Random(0))
        self.assertEqual(sum(p["tier"] == 1 for p in people), 2)
        self.assertEqual(sum(p["model"] == "opus" for p in people), 1)
        self.assertEqual(len({p["name"] for p in people}), len(people))

    def test_monitor_names_by_tier(self):
        people = newsroom.staff(30, seniors=3, rng=random.Random(5))
        for p in people:
            adjs, nouns = newsroom.NAME_WORDS[p["tier"]]
            adj, noun = p["name"].split(" ")
            self.assertIn(adj, adjs)
            self.assertIn(noun, nouns)

    def test_every_reporter_gets_exactly_one_story(self):
        people = newsroom.staff(9, seniors=2, rng=random.Random(3))
        stories = newsroom.assign(people, pair_rate=0.5, rng=random.Random(3))
        bylines = [r["sessionId"] for s in stories for r in s["reporters"]]
        self.assertEqual(sorted(bylines), sorted(p["sessionId"] for p in people if p["tier"] == 2))
        for s in stories:  # pairs never cross desks
            self.assertEqual({r["desk"] for r in s["reporters"]}, {s["desk"]})
        self.assertEqual(len({s["slug"] for s in stories}), len(stories))

    def test_parse_review(self):
        text = "city-hall-1: APPROVE\nschools-2: REVISE add a second source\n"
        got = newsroom.parse_review(text, ["city-hall-1", "schools-2", "transit-3"])
        self.assertEqual(got, {"city-hall-1": None, "schools-2": "add a second source", "transit-3": None})


if __name__ == "__main__":
    unittest.main()
