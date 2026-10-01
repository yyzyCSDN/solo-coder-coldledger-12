import unittest
from datetime import datetime, timezone, timedelta, date

from coldledger.api import ColdLedgerService
from coldledger.dispatch import express_allocation as ea

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def driver(d="D1", zone="FRA", certs=frozenset({"BIO"}), until_hours=14, max_duty=12.0, frm_hours=0):
    return ea.Driver(d, zone, T0 + timedelta(hours=frm_hours), T0 + timedelta(hours=until_hours), max_duty, certs)


def vehicle(v="V1", zone="FRA", low=2.0, high=8.0, cap=100.0):
    return ea.Vehicle(v, zone, low, high, cap)


def pack(p="P1", lane="AIR", hold_low=2.0, hold_high=8.0,
         amb_low=-10.0, amb_high=35.0, qualified=48.0):
    return ea.ThermalPack(p, lane, hold_low, hold_high, amb_low, amb_high, qualified)


def slot(s="S1", dests=frozenset({"JFK"}), closes_hours=12, cap=2):
    return ea.AirportSlot(s, dests, T0 + timedelta(hours=closes_hours), cap)


def task(t="T1", locked=None, temp_low=2.0, temp_high=8.0, pc="BIO",
         dest="JFK", juris="US", ready_hours=0, deadline_hours=30,
         payload=10.0, priority=0, risk=0.0, origin="FRA"):
    return ea.ExpressTask(
        t, origin, dest, juris, pc, payload, temp_low, temp_high,
        T0 + timedelta(hours=ready_hours), T0 + timedelta(hours=deadline_hours),
        priority, risk, locked,
    )


def option(o="O1", suspended=False, juris=frozenset({"US"}), pcs=frozenset({"BIO"}),
           cutoff_hours=10, ground=2.0, transit=12.0, otp=0.95, risk=0.05,
           cost=1000.0, fc_low=0.0, fc_high=30.0, drv=None, veh=None,
           pk=None, slt=None, lane="AIR"):
    return ea.CarrierOption(
        o, f"C-{o}", juris, pcs, date(2026, 12, 31), suspended,
        T0 + timedelta(hours=cutoff_hours), ground, transit, otp, risk, cost,
        fc_low, fc_high, drv or driver(), veh or vehicle(), pk or pack(),
        slt or slot(), lane_class=lane,
    )


class ExpressAllocationTests(unittest.TestCase):
    def test_screen_reports_all_dimensions_at_once(self):
        bad = option(
            "BAD",
            suspended=True,
            juris=frozenset({"EU"}),
            pcs=frozenset({"DRY"}),
            cutoff_hours=-1,            # tender 08:00 > cutoff 07:00
            transit=36.0,               # arrives 36h > deadline 30h
            fc_low=-25.0, fc_high=45.0,
            drv=driver("DB", zone="MUC", certs=frozenset({"DRY"}),
                       frm_hours=4, until_hours=6, max_duty=1.0),
            veh=vehicle("VB", zone="MUC", low=15.0, high=25.0, cap=5.0),
            slt=slot("SB", dests=frozenset({"LAX"})),
            pk=pack(qualified=24.0),
        )
        lines = ColdLedgerService().screen_express(task(), [bad])
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0].feasible)
        for code in (
            "qualification_suspended", "qualification_jurisdiction",
            "qualification_product_class", "cutoff_missed", "deadline_missed",
            "pack_ambient_envelope", "pack_qualified_hours",
            "driver_zone", "driver_certification", "driver_duty_window",
            "driver_max_duty", "slot_destination",
            "vehicle_zone", "vehicle_temperature_band", "vehicle_capacity",
        ):
            self.assertIn(code, lines[0].reasons)

    def test_gate_then_rank_timeliness_vs_risk(self):
        fast_risky = option("FAST", ground=1.0, transit=6.0, risk=0.10, otp=0.90, cost=1200.0,
                            slt=slot("S1", closes_hours=10))
        slow_safe = option("SAFE", ground=3.0, transit=18.0, risk=0.02, otp=0.99, cost=800.0,
                           slt=slot("SW", closes_hours=12))
        # Risk-neutral low-risk cargo: the fast option wins on timeliness.
        plan = ea.allocate([task(risk=0.0)], [fast_risky, slow_safe])
        self.assertEqual(plan.assigned_option("T1"), "FAST")
        # High-risk cargo tips the trade-off toward the safe carrier.
        plan = ea.allocate([task(risk=1.0)], [fast_risky, slow_safe])
        self.assertEqual(plan.assigned_option("T1"), "SAFE")

    def test_locked_task_reserves_capacity_and_wins_contention(self):
        tight = option("TIGHT", veh=vehicle(cap=15.0),
                       slt=slot(cap=1, closes_hours=12))
        wide = option("WIDE", drv=driver("D2"), veh=vehicle("VW", cap=100.0),
                      slt=slot("SW", closes_hours=12))
        t_locked = task("LOCKED", locked="TIGHT", priority=5)
        t_free = task("FREE")
        plan = ea.allocate([t_locked, t_free], [tight, wide])
        self.assertEqual(plan.assigned_option("LOCKED"), "TIGHT")
        self.assertEqual(plan.assigned_option("FREE"), "WIDE")

    def test_locked_option_failing_gate_is_rejected_not_moved(self):
        good = option("GOOD")
        bad = option("BAD", suspended=True)
        plan = ea.allocate([task("L", locked="BAD"), task("F")], [good, bad])
        self.assertIsNone(plan.assigned_option("L"))
        self.assertEqual(plan.assigned_option("F"), "GOOD")
        rj = next(r for r in plan.rejections if r.task_id == "L")
        self.assertTrue(rj.locked)
        self.assertIn("qualification_suspended", rj.reasons)

    def test_unknown_locked_option_raises(self):
        with self.assertRaises(ValueError):
            ea.allocate([task(locked="GHOST")], [option()])

    def test_failover_keeps_locked_and_raises(self):
        o1 = option("O1", veh=vehicle(cap=15.0), slt=slot(cap=1))
        o2 = option("O2", drv=driver("D2"), veh=vehicle("VW", cap=100.0), slt=slot("SW"))
        t_locked = task("LOCKED", locked="O1", priority=5)
        t_free = task("FREE")
        plan = ea.allocate([t_locked, t_free], [o1, o2])
        with self.assertRaises(ea.LockedAssignmentAtRisk) as ctx:
            ea.replan(plan, [t_locked, t_free], [o1, o2], ["O1"])
        self.assertEqual(ctx.exception.stranded_task_ids, ["LOCKED"])
        # The unlocked task must be recovered onto the surviving carrier.
        self.assertEqual(ctx.exception.partial_plan.assigned_option("FREE"), "O2")

    def test_failover_reassigns_unlocked_without_stranded_locked(self):
        o1 = option("O1")
        o2 = option("O2", drv=driver("D2"), veh=vehicle("VW"), slt=slot("SW"))
        plan = ea.allocate([task()], [o1, o2])
        self.assertEqual(plan.assigned_option("T1"), "O1")
        new_plan = ea.replan(plan, [task()], [o1, o2], ["O1"])
        self.assertEqual(new_plan.assigned_option("T1"), "O2")

    def test_failover_with_unknown_failed_resource_is_noop(self):
        plan = ea.allocate([task()], [option()])
        self.assertIs(ea.replan(plan, [task()], [option()], ["NOPE"]), plan)

    def test_driver_double_booking_is_hard_constraint(self):
        o = option()
        t_locked = task("LOCKED", locked="O1")
        # Another free task uses the same driver and vehicle in the same window.
        t_free = task("FREE")
        plan = ea.allocate([t_locked, t_free], [o])
        self.assertEqual(plan.assigned_option("LOCKED"), "O1")
        self.assertIsNone(plan.assigned_option("FREE"))
        rj = next(r for r in plan.rejections if r.task_id == "FREE")
        self.assertIn("driver_conflict", rj.reasons)
        self.assertIn("vehicle_conflict", rj.reasons)

    def test_slot_capacity_is_hard_constraint(self):
        o = option(slt=slot(cap=1))
        # Separate drivers/vehicles so only the slot constraint bites.
        o2 = option("O2", drv=driver("D2"), veh=vehicle("V2"), slt=slot(cap=1))
        t1 = task("T1")
        t2 = task("T2")
        plan = ea.allocate([t1, t2], [o, o2])
        self.assertIsNone(plan.assigned_option("T2"))
        rj = next(r for r in plan.rejections if r.task_id == "T2")
        self.assertIn("slot_capacity", rj.reasons)

    def test_service_wiring_and_explain(self):
        svc = ColdLedgerService()
        plan = svc.allocate_express([task()], [option()])
        self.assertTrue(svc.require_complete_express(plan, [task()]))
        self.assertTrue(any("T1 -> O1" in line for line in svc.explain_express(plan)))
        with self.assertRaises(ValueError):
            svc.require_complete_express(
                svc.allocate_express([task()], [option(suspended=True)]), [task()]
            )


if __name__ == "__main__":
    unittest.main()
