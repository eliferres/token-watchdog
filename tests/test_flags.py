"""Each flag fires past its threshold and stays quiet below it."""
from __future__ import annotations

import unittest

import token_watchdog as tw

LIMITS = dict(tw.DEFAULT_THRESHOLDS)


def session(name: str, inp: int = 0, write: int = 0, read: int = 0, out: int = 0,
            largest: float = 0.0, project: str = "p") -> dict:
    tokens = {"input": inp, "cache_write_5m": write, "cache_write_1h": 0, "cache_read": read, "output": out}
    weighted = sum(tokens[k] * tw.DEFAULT_WEIGHTS[k] for k in tokens)
    return {"project": project, "session": name, "tokens": tokens, "weighted": weighted,
            "turns": 1, "largest_turn": largest or weighted}


def report(*sessions: dict) -> dict:
    return {"projects": [{"project": "p", "label": "demo-app"}],
            "sessions": list(sessions),
            "total": {"weighted": sum(s["weighted"] for s in sessions)}}


def rules(flags: list) -> list:
    return [(f["rule"], f["session"]) for f in flags]


# A healthy long session: mostly cache reads, read back about 30 times, many small calls.
HEALTHY = dict(inp=20_000, write=400_000, read=12_000_000, out=60_000, largest=90_000)


class LowCacheHitTest(unittest.TestCase):
    def test_fires_below_the_line(self) -> None:
        cold = session("cold", inp=2_000_000, write=1_000_000, read=6_000_000, largest=90_000)
        flags = tw.find_flags(report(cold), LIMITS)
        self.assertEqual(rules(flags), [("low-cache-hit", "cold")])
        # 66.7% rounds down to match the session table.
        self.assertIn("66% of input came from cache, below 80%", flags[0]["message"])

    def test_quiet_for_a_healthy_session(self) -> None:
        self.assertEqual(tw.find_flags(report(session("ok", **HEALTHY)), LIMITS), [])

    def test_small_sessions_are_not_judged(self) -> None:
        tiny = session("tiny", inp=50_000, write=100_000)
        self.assertEqual(tw.find_flags(report(tiny), LIMITS), [])


class RereadHeavyTest(unittest.TestCase):
    def test_fires_when_the_context_is_read_back_too_often(self) -> None:
        long = session("long", inp=1_000, write=100_000, read=30_000_000, out=10_000, largest=80_000)
        flags = tw.find_flags(report(long), LIMITS)
        self.assertEqual(rules(flags), [("reread-heavy", "long")])
        self.assertEqual(flags[0]["value"], 300.0)

    def test_quiet_at_the_line(self) -> None:
        edge = session("edge", inp=1_000, write=100_000, read=10_000_000, largest=80_000)
        self.assertEqual(tw.find_flags(report(edge), LIMITS), [])


class OutsizedTurnTest(unittest.TestCase):
    def test_fires_on_one_heavy_call_even_in_a_small_session(self) -> None:
        spike = session("spike", write=480_000, largest=600_000)
        flags = tw.find_flags(report(spike), LIMITS)
        self.assertEqual(rules(flags), [("outsized-turn", "spike")])
        self.assertIn("one call weighed 600k, over 500k", flags[0]["message"])


    def test_quiet_when_the_call_is_a_small_part_of_a_large_session(self) -> None:
        # A full-context cache write on a 1M-context model: big, but routine for its session.
        routine = session("routine", inp=50_000, write=2_000_000, read=80_000_000, out=200_000, largest=600_000)
        self.assertLess(600_000 / routine["weighted"], 0.10)
        self.assertEqual(tw.find_flags(report(routine), LIMITS), [])

    def test_fires_at_ten_percent_of_its_session(self) -> None:
        heavy = session("heavy", inp=100_000, write=1_000_000, read=30_000_000, largest=600_000)
        self.assertGreaterEqual(600_000 / heavy["weighted"], 0.10)
        self.assertEqual(rules(tw.find_flags(report(heavy), LIMITS)), [("outsized-turn", "heavy")])


class SessionShareTest(unittest.TestCase):
    def test_fires_past_twice_a_fair_share_among_five_judged_sessions(self) -> None:
        big = session("big", inp=20_000, write=800_000, read=60_000_000, out=60_000, largest=90_000)  # 7.3M
        others = [session(f"s{n}", **HEALTHY) for n in range(4)]  # 2.0M each
        flags = tw.find_flags(report(big, *others), LIMITS)
        self.assertEqual(rules(flags), [("session-share", "big")])
        self.assertEqual(flags[0]["message"],
                         "47% of the window's weighted total, over 2x a fair share of 5 sessions (40%)")

    def test_three_identical_small_sessions_never_fire(self) -> None:
        trio = report(*(session(f"s{n}", inp=11_000, largest=11_000) for n in range(3)))
        self.assertEqual(tw.find_flags(trio, LIMITS), [])

    def test_needs_five_sessions_over_the_size_floor(self) -> None:
        big = session("big", inp=20_000, write=800_000, read=60_000_000, out=60_000, largest=90_000)
        small = [session(f"t{n}", inp=1_000, largest=1_000) for n in range(20)]
        three = [session(f"s{n}", **HEALTHY) for n in range(3)]
        self.assertEqual(tw.find_flags(report(big, *three, *small), LIMITS), [])

    def test_quiet_when_usage_is_spread(self) -> None:
        even = report(*(session(f"s{n}", **HEALTHY) for n in range(6)))
        self.assertEqual(tw.find_flags(even, LIMITS), [])


class ThresholdsTest(unittest.TestCase):
    def test_a_raised_threshold_silences_its_flag(self) -> None:
        cold = session("cold", inp=2_000_000, write=1_000_000, read=6_000_000, largest=90_000)
        self.assertEqual(tw.find_flags(report(cold), dict(LIMITS, cache_hit_min=0.5)), [])

    def test_percentages_round_down(self) -> None:
        self.assertEqual([tw.percent(v) for v in (0.994, 1.0, 0.0, 0.29, None)], ["99%", "100%", "0%", "29%", "-"])

    def test_human_counts(self) -> None:
        self.assertEqual([tw.human(n) for n in (950, 12_400, 3_449_999)], ["950", "12k", "3.4M"])


if __name__ == "__main__":
    unittest.main()
