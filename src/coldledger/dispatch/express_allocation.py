from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

# Urgent ("next-flight-out") courier allocation.
#
# The six operational dimensions are evaluated *together* as one hard gate:
#   1. qualification  (carrier license: jurisdiction / product class / expiry / suspension)
#   2. cut-off        (order tender time vs carrier booking cut-off + destination deadline)
#   3. packaging      (thermal pack band, ambient envelope, qualified insulation hours)
#   4. driver duty    (zone, certification, duty window, max duty hours, double booking)
#   5. airport slot   (destination served, handoff before slot close, slot capacity)
#   6. vehicle        (temperature band covers the product, kg capacity, double booking)
# Only options that pass every hard predicate are allowed through; the survivors are
# then ranked on timeliness vs risk. Locked tasks are never moved implicitly.

TIME_WEIGHT = 12.0
RISK_WEIGHT = 800.0
COST_WEIGHT = 0.05
OTP_WEIGHT = 100.0

# Stable order for violation codes and human-readable labels.
REASON_ORDER = (
    "qualification_suspended",
    "qualification_jurisdiction",
    "qualification_product_class",
    "qualification_expired",
    "cutoff_missed",
    "deadline_missed",
    "pack_lane_class",
    "pack_temperature_band",
    "pack_ambient_envelope",
    "pack_qualified_hours",
    "driver_zone",
    "driver_certification",
    "driver_duty_window",
    "driver_max_duty",
    "driver_conflict",
    "slot_destination",
    "slot_closed",
    "slot_capacity",
    "vehicle_zone",
    "vehicle_temperature_band",
    "vehicle_capacity",
    "vehicle_conflict",
)
REASON_LABELS = {
    "qualification_suspended": "carrier qualification is suspended",
    "qualification_jurisdiction": "carrier is not licensed for the destination jurisdiction",
    "qualification_product_class": "carrier is not licensed for the product class",
    "qualification_expired": "carrier license has expired at tender time",
    "cutoff_missed": "tender time is after the carrier booking cut-off",
    "deadline_missed": "earliest arrival is after the task deadline",
    "pack_lane_class": "thermal pack is not qualified for this lane class",
    "pack_temperature_band": "thermal pack cannot hold the required temperature band",
    "pack_ambient_envelope": "thermal pack not qualified for the forecast ambient envelope",
    "pack_qualified_hours": "transit exceeds the pack's qualified insulation hours",
    "driver_zone": "driver is not based in the pickup zone",
    "driver_certification": "driver lacks certification for the product class",
    "driver_duty_window": "ground leg falls outside the driver duty window",
    "driver_max_duty": "ground leg exceeds the driver's maximum duty hours",
    "driver_conflict": "driver is already booked over that interval by a locked task",
    "slot_destination": "airport slot does not serve the destination",
    "slot_closed": "airport handoff would happen after slot close",
    "slot_capacity": "airport slot has no remaining capacity",
    "vehicle_zone": "vehicle is not based in the pickup zone",
    "vehicle_temperature_band": "vehicle compartment band does not cover the product band",
    "vehicle_capacity": "vehicle has insufficient remaining kg capacity",
    "vehicle_conflict": "vehicle is already booked over that interval by a locked task",
}


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return v.astimezone(timezone.utc)


# ---------------------------------------------------------------- entities --
@dataclass(frozen=True)
class Driver:
    driver_id: str
    zone: str
    on_duty_from: datetime
    on_duty_until: datetime
    max_duty_hours: float
    certifications: frozenset[str] = frozenset()

    def validate(self) -> None:
        if _utc(self.on_duty_until) <= _utc(self.on_duty_from):
            raise ValueError("invalid driver duty window")
        if self.max_duty_hours <= 0:
            raise ValueError("max_duty_hours must be positive")


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    zone: str
    temp_low: float
    temp_high: float
    capacity_kg: float

    def validate(self) -> None:
        if self.temp_high < self.temp_low:
            raise ValueError("invalid vehicle temperature band")
        if self.capacity_kg <= 0:
            raise ValueError("vehicle capacity must be positive")


@dataclass(frozen=True)
class ThermalPack:
    pack_id: str
    lane_class: str
    hold_low: float
    hold_high: float
    ambient_low: float
    ambient_high: float
    qualified_hours: float

    def validate(self) -> None:
        if self.hold_high < self.hold_low or self.ambient_high < self.ambient_low:
            raise ValueError("invalid thermal pack band")
        if self.qualified_hours <= 0:
            raise ValueError("qualified_hours must be positive")


@dataclass(frozen=True)
class AirportSlot:
    slot_id: str
    destinations: frozenset[str]
    closes_at: datetime
    capacity: int

    def validate(self) -> None:
        if not self.destinations:
            raise ValueError("slot serves no destination")
        if self.capacity <= 0:
            raise ValueError("slot capacity must be positive")


@dataclass(frozen=True)
class CarrierOption:
    option_id: str
    carrier: str
    jurisdictions: frozenset[str]
    product_classes: frozenset[str]
    license_valid_to: date
    suspended: bool
    cutoff_at: datetime
    ground_hours: float
    transit_hours: float
    on_time_rate: float
    risk: float
    cost: float
    forecast_low: float
    forecast_high: float
    driver: Driver
    vehicle: Vehicle
    pack: ThermalPack
    slot: AirportSlot
    lane_class: str = "*"

    def validate(self) -> None:
        if not 0.0 < self.ground_hours <= self.transit_hours:
            raise ValueError("require 0 < ground_hours <= transit_hours")
        if not 0.0 <= self.risk <= 1.0:
            raise ValueError("risk outside 0..1")
        if not 0.0 <= self.on_time_rate <= 1.0:
            raise ValueError("on_time_rate outside 0..1")
        if self.cost < 0:
            raise ValueError("cost must be non-negative")
        if self.lane_class not in {self.pack.lane_class, "*"}:
            raise ValueError("option lane_class is not served by its thermal pack")
        self.driver.validate()
        self.vehicle.validate()
        self.pack.validate()
        self.slot.validate()


@dataclass(frozen=True)
class ExpressTask:
    task_id: str
    origin: str
    destination: str
    jurisdiction: str
    product_class: str
    payload_kg: float
    temp_low: float
    temp_high: float
    ready_at: datetime
    deadline_at: datetime
    priority: int = 0
    risk: float = 0.0
    locked_option: str | None = None

    def validate(self) -> None:
        if self.payload_kg <= 0:
            raise ValueError("payload_kg must be positive")
        if self.temp_high < self.temp_low:
            raise ValueError("invalid required temperature band")
        if _utc(self.deadline_at) < _utc(self.ready_at):
            raise ValueError("deadline is before ready time")
        if not 0.0 <= self.risk <= 1.0:
            raise ValueError("risk outside 0..1")
        if not self.origin or not self.destination or not self.jurisdiction:
            raise ValueError("origin, destination and jurisdiction are required")


# ------------------------------------------------------------- reservations --
@dataclass(frozen=True)
class Reservations:
    """Capacity already consumed by tasks that cannot be moved (locked assignments)."""
    vehicle_kg: Mapping[str, float] = field(default_factory=dict)
    slot_used: Mapping[str, int] = field(default_factory=dict)
    intervals: Mapping[str, tuple[tuple[datetime, datetime, str], ...]] = field(default_factory=dict)

    def occupy(self, option: CarrierOption, task: ExpressTask,
               pickup: datetime, handoff: datetime) -> "Reservations":
        vehicle_kg = dict(self.vehicle_kg)
        vehicle_kg[option.vehicle.vehicle_id] = (
            vehicle_kg.get(option.vehicle.vehicle_id, 0.0) + task.payload_kg
        )
        slot_used = dict(self.slot_used)
        slot_used[option.slot.slot_id] = slot_used.get(option.slot.slot_id, 0) + 1
        intervals = dict(self.intervals)
        for key in (f"driver:{option.driver.driver_id}", f"vehicle:{option.vehicle.vehicle_id}"):
            intervals[key] = intervals.get(key, ()) + ((pickup, handoff, task.task_id),)
        return Reservations(vehicle_kg, slot_used, intervals)

    def overlaps(self, key: str, start: datetime, end: datetime) -> bool:
        for a, b, _ in self.intervals.get(key, ()):
            if start < b and a < end:
                return True
        return False


# --------------------------------------------------------------- hard gate --
def evaluate(task: ExpressTask, option: CarrierOption,
             reservations: Reservations | None = None) -> tuple[str, ...]:
    """Return the *full* set of violated hard-constraint codes (empty == feasible).

    All six dimensions are evaluated in one pass so an option that fails several
    constraints reports all of them instead of dying at the first siloed check.
    """
    task.validate()
    option.validate()
    res = reservations or Reservations()
    reasons: set[str] = set()

    ready = _utc(task.ready_at)
    deadline = _utc(task.deadline_at)
    cutoff = _utc(option.cutoff_at)
    handoff = ready + timedelta(hours=option.ground_hours)
    arrive = ready + timedelta(hours=option.transit_hours)

    # 1. qualification
    if option.suspended:
        reasons.add("qualification_suspended")
    if task.jurisdiction not in option.jurisdictions:
        reasons.add("qualification_jurisdiction")
    if task.product_class not in option.product_classes:
        reasons.add("qualification_product_class")
    if ready.date() > option.license_valid_to:
        reasons.add("qualification_expired")

    # 2. cut-off and deadline
    if ready > cutoff:
        reasons.add("cutoff_missed")
    if arrive > deadline:
        reasons.add("deadline_missed")

    # 3. packaging / thermal insulation
    pack = option.pack
    if pack.lane_class not in {option.lane_class, "*"}:
        reasons.add("pack_lane_class")
    if pack.hold_low > task.temp_low or pack.hold_high < task.temp_high:
        reasons.add("pack_temperature_band")
    if option.forecast_low < pack.ambient_low or option.forecast_high > pack.ambient_high:
        reasons.add("pack_ambient_envelope")
    if option.transit_hours > pack.qualified_hours:
        reasons.add("pack_qualified_hours")

    # 4. driver duty
    driver = option.driver
    if driver.zone != task.origin:
        reasons.add("driver_zone")
    if task.product_class not in driver.certifications and "*" not in driver.certifications:
        reasons.add("driver_certification")
    if ready < _utc(driver.on_duty_from) or handoff > _utc(driver.on_duty_until):
        reasons.add("driver_duty_window")
    if option.ground_hours > driver.max_duty_hours:
        reasons.add("driver_max_duty")
    if res.overlaps(f"driver:{driver.driver_id}", ready, handoff):
        reasons.add("driver_conflict")

    # 5. airport handoff
    slot = option.slot
    if task.destination not in slot.destinations:
        reasons.add("slot_destination")
    if handoff > _utc(slot.closes_at):
        reasons.add("slot_closed")
    if res.slot_used.get(slot.slot_id, 0) + 1 > slot.capacity:
        reasons.add("slot_capacity")

    # 6. vehicle temperature control and capacity
    vehicle = option.vehicle
    if vehicle.zone != task.origin:
        reasons.add("vehicle_zone")
    if vehicle.temp_low > task.temp_low or vehicle.temp_high < task.temp_high:
        reasons.add("vehicle_temperature_band")
    if res.vehicle_kg.get(vehicle.vehicle_id, 0.0) + task.payload_kg > vehicle.capacity_kg:
        reasons.add("vehicle_capacity")
    if res.overlaps(f"vehicle:{vehicle.vehicle_id}", ready, handoff):
        reasons.add("vehicle_conflict")

    return tuple(code for code in REASON_ORDER if code in reasons)


@dataclass(frozen=True)
class ScreeningLine:
    task_id: str
    option_id: str
    feasible: bool
    reasons: tuple[str, ...]


def screen(task: ExpressTask, options: Iterable[CarrierOption],
           reservations: Reservations | None = None,
           exclude: frozenset[str] = frozenset()) -> list[ScreeningLine]:
    """Run the joint hard gate for every option and return the full audit matrix."""
    lines = []
    for option in sorted(options, key=lambda o: o.option_id):
        if option.option_id in exclude:
            continue
        reasons = evaluate(task, option, reservations)
        lines.append(ScreeningLine(task.task_id, option.option_id, not reasons, reasons))
    return lines


def feasible_options(task: ExpressTask, options: Iterable[CarrierOption],
                     reservations: Reservations | None = None,
                     exclude: frozenset[str] = frozenset()) -> list[CarrierOption]:
    by_id = {o.option_id: o for o in options}
    return [by_id[line.option_id] for line in screen(task, by_id.values(), reservations, exclude)
            if line.feasible]


# --------------------------------------------------------- soft ranking ----
def score_option(task: ExpressTask, option: CarrierOption) -> float:
    """Timeliness vs risk vs cost; lower score wins.

    12*transit hours + 800*carrier risk*(1 + cargo risk) + 0.05*cost
    + 100*(1 - on-time rate), mirroring the weighting convention used in
    transport.route_selection. High-risk cargo doubles the carrier-risk
    penalty, tipping the trade-off toward the safer carrier.
    """
    return (
        TIME_WEIGHT * option.transit_hours
        + RISK_WEIGHT * option.risk * (1.0 + task.risk)
        + COST_WEIGHT * option.cost
        + OTP_WEIGHT * (1.0 - option.on_time_rate)
    )


# --------------------------------------------------------------- planning --
@dataclass(frozen=True)
class Assignment:
    task_id: str
    option_id: str
    pickup_at: datetime
    handoff_at: datetime
    arrive_at: datetime
    score: float
    locked: bool


@dataclass(frozen=True)
class Rejection:
    task_id: str
    option_id: str | None
    locked: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ExpressPlan:
    assignments: tuple[Assignment, ...]
    rejections: tuple[Rejection, ...]
    screening: tuple[ScreeningLine, ...]
    reservations: Reservations

    def assigned_option(self, task_id: str) -> str | None:
        for a in self.assignments:
            if a.task_id == task_id:
                return a.option_id
        return None

    def is_complete(self, tasks: Iterable[ExpressTask]) -> bool:
        assigned = {a.task_id for a in self.assignments}
        return all(t.task_id in assigned for t in tasks)


def allocate(tasks: Iterable[ExpressTask], options: Iterable[CarrierOption],
             reservations: Reservations | None = None,
             exclude: frozenset[str] = frozenset()) -> ExpressPlan:
    """Hard-gate every option first, then pick the best timeliness/risk survivor.

    Locked tasks (locked_option set) reserve capacity first and may only use the
    option they are locked to. If that option fails the gate they are rejected
    with the full reason list - never silently moved to another carrier.
    """
    task_list = list(tasks)
    option_list = list(options)
    for t in task_list:
        t.validate()
        if t.locked_option is not None and t.locked_option not in {o.option_id for o in option_list}:
            raise ValueError(f"locked option {t.locked_option} for {t.task_id} is unknown")
    by_id = {o.option_id: o for o in option_list}

    res = reservations or Reservations()
    assignments: list[Assignment] = []
    rejections: list[Rejection] = []
    screening: list[ScreeningLine] = []

    # Immovable tasks consume capacity before anything else can grab it.
    ordered = sorted(
        task_list,
        key=lambda t: (t.locked_option is None, -t.priority, _utc(t.deadline_at), t.task_id),
    )
    for task in ordered:
        ready = _utc(task.ready_at)
        if task.locked_option is not None:
            option = by_id[task.locked_option]
            reasons = evaluate(task, option, res)
            screening.append(ScreeningLine(task.task_id, option.option_id, not reasons, reasons))
            if reasons:
                rejections.append(Rejection(task.task_id, option.option_id, True, reasons))
                continue
            handoff = ready + timedelta(hours=option.ground_hours)
            arrive = ready + timedelta(hours=option.transit_hours)
            assignments.append(Assignment(
                task.task_id, option.option_id, ready, handoff, arrive,
                score_option(task, option), True,
            ))
            res = res.occupy(option, task, ready, handoff)
            continue

        lines = screen(task, option_list, res, exclude)
        screening.extend(lines)
        survivors = [by_id[line.option_id] for line in lines if line.feasible]
        if not survivors:
            all_reasons = tuple(sorted({r for line in lines for r in line.reasons}))
            rejections.append(Rejection(task.task_id, None, False, all_reasons))
            continue
        option = min(survivors, key=lambda o: (score_option(task, o), o.option_id))
        handoff = ready + timedelta(hours=option.ground_hours)
        arrive = ready + timedelta(hours=option.transit_hours)
        assignments.append(Assignment(
            task.task_id, option.option_id, ready, handoff, arrive,
            score_option(task, option), False,
        ))
        res = res.occupy(option, task, ready, handoff)

    return ExpressPlan(tuple(assignments), tuple(rejections), tuple(screening), res)


def require_complete(plan: ExpressPlan, tasks: Iterable[ExpressTask]) -> ExpressPlan:
    if plan.is_complete(tasks):
        return plan
    details = "; ".join(
        f"{r.task_id}{'[' + r.option_id + ']' if r.option_id else ''}: {', '.join(r.reasons)}"
        for r in plan.rejections
    )
    raise ValueError(f"no feasible carrier for: {details}")


# ------------------------------------------------------- failover handling --
class LockedAssignmentAtRisk(RuntimeError):
    """A locked task was sitting on a resource that just failed.

    The task is deliberately left on the dead resource in ``partial_plan`` and
    surfaced here; callers must arrange an explicit override or contingency.
    """

    def __init__(self, stranded_task_ids: list[str], partial_plan: ExpressPlan):
        self.stranded_task_ids = stranded_task_ids
        self.partial_plan = partial_plan
        super().__init__(
            "locked tasks stranded on failed resource, not reassigned: "
            + ", ".join(stranded_task_ids)
        )


def replan(plan: ExpressPlan, tasks: Iterable[ExpressTask],
           options: Iterable[CarrierOption], failed_options: Iterable[str]) -> ExpressPlan:
    """Re-allocate after resources become unavailable.

    - unlocked assignments on failed resources are re-planned elsewhere;
    - locked assignments (and tasks pre-locked to a failed option) are never
      moved - a LockedAssignmentAtRisk is raised with the partial recovery plan;
    - capacity consumed by every surviving locked task stays reserved.
    """
    option_list = list(options)
    by_id = {o.option_id: o for o in option_list}
    failed = {f for f in failed_options if f in by_id}
    if not failed:
        return plan

    task_list = list(tasks)
    tasks_by_id = {t.task_id: t for t in task_list}

    stranded = [
        a.task_id for a in plan.assignments
        if a.locked and a.option_id in failed
    ]
    kept = [a for a in plan.assignments if a.option_id not in failed]

    # Rebuild reservations from the surviving assignments (all of which are
    # either locked or untouched unlocked tasks - they keep their capacity).
    res = Reservations()
    for a in sorted(kept, key=lambda x: (x.locked is False, x.task_id)):
        task = tasks_by_id[a.task_id]
        option = by_id[a.option_id]
        res = res.occupy(option, task, _utc(a.pickup_at), _utc(a.handoff_at))

    kept_ids = {a.task_id for a in kept}
    retry = [t for t in task_list if t.task_id not in kept_ids]

    # Pre-locked tasks pointing at a dead resource are stranded, never swapped.
    stranded_pre = list(stranded)
    retry: list[ExpressTask] = []
    for t in task_list:
        if t.task_id in kept_ids:
            continue
        if t.locked_option is not None and t.locked_option in failed:
            if t.task_id not in stranded_pre:
                stranded_pre.append(t.task_id)
            continue
        retry.append(t)

    recovered = allocate(retry, option_list, res, exclude=frozenset(failed))
    partial = ExpressPlan(
        assignments=tuple(kept) + recovered.assignments,
        rejections=recovered.rejections,
        screening=recovered.screening,
        reservations=recovered.reservations,
    )
    if stranded_pre:
        raise LockedAssignmentAtRisk(stranded_pre, partial)
    return partial


# ----------------------------------------------------------------- explain --
def explain_rejection(reasons: Iterable[str]) -> list[str]:
    return [REASON_LABELS.get(code, code) for code in reasons]


def explain(plan: ExpressPlan) -> list[str]:
    rows = [
        f"{a.task_id} -> {a.option_id} "
        f"(handoff {_utc(a.handoff_at):%Y-%m-%d %H:%M}, score {a.score:.1f}"
        f"{', locked' if a.locked else ''})"
        for a in plan.assignments
    ]
    for r in plan.rejections:
        target = f"[{r.option_id}]" if r.option_id else "(all options screened out)"
        rows.append(f"{r.task_id} REJECTED {target}: {', '.join(explain_rejection(r.reasons))}")
    return rows
