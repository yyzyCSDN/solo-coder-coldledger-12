import unittest
from dataclasses import replace
from datetime import datetime, timezone

from coldledger.dispatch.urgent_assignment import (
    UrgentTask, CarrierResource, Booking,
    evaluate, screen, feasible, choose, assign,
    lock, release_lock, replan, verify, explain,
)

UTC = timezone.utc


def dt(hour, day=1):
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def make_task(**kw):
    base = dict(
        key="T1", ready_at=dt(8), cut_off=dt(12), deliver_by=dt(6, 2),
        required_quals=frozenset({"GDP"}), temp_low=2.0, temp_high=8.0,
        package_hours=30.0, drive_hours=2.0,
        handover_open=dt(14), handover_close=dt(20),
        flight_depart=dt(21), flight_arrive=dt(2, 2),
        last_mile_hours=1.5, min_handover_minutes=60, priority=0,
    )
    base.update(kw)
    return UrgentTask(**base)


def make_resource(key="R1", **kw):
    base = dict(key=key, quals=frozenset({"GDP"}), temp_min=0.0, temp_max=10.0,
                available_from=dt(7), duty_limit_hours=12.0, reliability=0.98)
    base.update(kw)
    return CarrierResource(**base)


class ScreenTests(unittest.TestCase):
    def test_timeline_built_backwards_from_flight(self):
        c = evaluate(make_task(), make_resource())
        self.assertTrue(c.feasible)
        # pickup pinned to cut-off, airport arrival at 14:00, dwell until 21:00
        self.assertEqual(c.timeline.pickup, dt(12))
        self.assertEqual(c.timeline.airport_arrive, dt(14))
        self.assertEqual(c.timeline.delivery, datetime(2026, 10, 2, 3, 30, tzinfo=UTC))
        self.assertAlmostEqual(c.timeline.duty_hours, 10.5)
        self.assertAlmostEqual(c.margin("duty"), 1.5)
        self.assertAlmostEqual(c.margin("cut_off"), 4.0)  # pickup window 08:00-12:00

    def test_duty_limit_only_visible_on_shared_timeline(self):
        # driving alone is 3.5h, well under a 9h limit; airport dwell pushes duty to 10.5h
        c = evaluate(make_task(), make_resource("R9", duty_limit_hours=9.0))
        self.assertFalse(c.feasible)
        self.assertIn("duty", c.reasons)

    def test_packaging_limit_checked_against_full_transit(self):
        c = evaluate(make_task(package_hours=12.0), make_resource())
        self.assertIn("packaging", c.reasons)

    def test_screen_collects_all_violations(self):
        bad = make_resource("BAD", quals=frozenset(), temp_min=4.0,
                            available_from=dt(13), duty_limit_hours=9.0)
        c = evaluate(make_task(), bad)
        self.assertEqual(set(c.reasons), {"qualification", "temperature", "cut_off", "duty"})
        self.assertIn("infeasible", explain(c))

    def test_disabled_resource_is_infeasible(self):
        c = evaluate(make_task(), make_resource("OFF", enabled=False))
        self.assertIn("disabled", c.reasons)

    def test_screen_sorted_and_feasible_filtered(self):
        rows = screen(make_task(), [make_resource("B"), make_resource("A", duty_limit_hours=9.0)])
        self.assertEqual([c.resource_key for c in rows], ["A", "B"])
        self.assertEqual([c.resource_key for c in feasible(make_task(), rows and [make_resource("B"), make_resource("A", duty_limit_hours=9.0)])], ["B"])

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(make_task(ready_at=datetime(2026, 10, 1, 8)), make_resource())


class RankTests(unittest.TestCase):
    def test_choose_weighs_margin_against_reliability(self):
        ra = make_resource("RA", duty_limit_hours=12.0, reliability=0.98)  # margin 1.5h
        rb = make_resource("RB", duty_limit_hours=11.0, reliability=0.99)  # margin 0.5h
        self.assertEqual(choose(make_task(), [rb, ra]).resource_key, "RA")
        # with margins ignored, higher reliability wins
        self.assertEqual(choose(make_task(), [rb, ra], margin_weight=0.0).resource_key, "RB")

    def test_choose_none_when_nothing_feasible(self):
        self.assertIsNone(choose(make_task(), [make_resource("RX", duty_limit_hours=9.0)]))

    def test_assign_priority_order_and_exclusive_resources(self):
        t1 = make_task(key="T1", priority=5)
        t2 = make_task(key="T2", priority=1)
        rs = [make_resource(k) for k in ("RA", "RB", "RC")]
        bookings = assign([t2, t1], rs)
        self.assertEqual([(b.task_key, b.resource_key) for b in bookings],
                         [("T1", "RA"), ("T2", "RB")])

    def test_assign_raises_when_infeasible(self):
        with self.assertRaises(ValueError):
            assign([make_task()], [make_resource("RX", duty_limit_hours=9.0)])


class LockTests(unittest.TestCase):
    def setUp(self):
        self.t1 = make_task(key="T1", priority=5)
        self.t2 = make_task(key="T2", priority=1)
        self.rs = [make_resource(k) for k in ("RA", "RB", "RC")]
        self.b1, self.b2 = assign([self.t1, self.t2], self.rs)

    def test_locked_booking_never_moved_silently(self):
        locked = lock(self.b1)
        outcome = replan([locked, self.b2], "RA", [self.t1, self.t2], self.rs)
        self.assertEqual(outcome.kept, (self.b2,))
        self.assertEqual(outcome.moved, ())
        self.assertEqual(len(outcome.stranded), 1)
        s = outcome.stranded[0]
        self.assertEqual((s.task_key, s.resource_key), ("T1", "RA"))  # held in place
        self.assertNotIn("T1", [b.task_key for b in outcome.kept + outcome.moved])

    def test_unlocked_booking_replanned_with_revision_bump(self):
        outcome = replan([self.b1, self.b2], "RA", [self.t1, self.t2], self.rs)
        self.assertEqual(outcome.stranded, ())
        self.assertEqual(len(outcome.moved), 1)
        moved = outcome.moved[0]
        self.assertEqual((moved.task_key, moved.resource_key), ("T1", "RC"))
        self.assertEqual(moved.revision, self.b1.revision + 1)

    def test_explicit_unlock_then_replan_moves_formerly_locked(self):
        unlocked = release_lock(lock(self.b1), actor="ops", reason="carrier grounded")
        outcome = replan([unlocked, self.b2], "RA", [self.t1, self.t2], self.rs)
        self.assertEqual(len(outcome.moved), 1)
        self.assertEqual(outcome.moved[0].resource_key, "RC")
        self.assertEqual(outcome.moved[0].revision, 3)  # lock + release + move

    def test_release_lock_requires_attribution(self):
        locked = lock(self.b1)
        with self.assertRaises(ValueError):
            release_lock(locked, actor="ops", reason="")
        with self.assertRaises(ValueError):
            release_lock(self.b1, actor="ops", reason="not locked")

    def test_no_replacement_strands_unlocked_booking(self):
        outcome = replan([self.b1, self.b2], "RA", [self.t1, self.t2], self.rs[:2])
        self.assertEqual(outcome.moved, ())
        self.assertIn("no feasible replacement", outcome.stranded[0].reason)

    def test_verify_flags_bookings_on_failed_resource(self):
        locked = lock(self.b1)
        rs = [replace(self.rs[0], enabled=False), *self.rs[1:]]
        violations = verify([locked, self.b2], [self.t1, self.t2], rs)
        self.assertEqual([v.task_key for v in violations], ["T1"])
        self.assertIn("disabled", violations[0].reason)

    def test_replan_rejects_unknown_tasks(self):
        with self.assertRaises(ValueError):
            replan([Booking("GHOST", "RA")], "RA", [self.t1], self.rs)


if __name__ == "__main__":
    unittest.main()
