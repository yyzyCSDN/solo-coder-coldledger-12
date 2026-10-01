from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable

QUALIFICATION = "qualification"
TEMPERATURE = "temperature"
CUT_OFF = "cut_off"
HANDOVER = "handover"
DUTY = "duty"
PACKAGING = "packaging"
DELIVERY = "delivery"
CONSTRAINTS = (QUALIFICATION, TEMPERATURE, CUT_OFF, HANDOVER, DUTY, PACKAGING, DELIVERY)
# margins measured in hours; temperature margin is in degrees and never enters the bottleneck
TIME_MARGINS = (CUT_OFF, HANDOVER, DUTY, PACKAGING, DELIVERY)


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return v.astimezone(timezone.utc)


@dataclass(frozen=True)
class UrgentTask:
    key: str
    ready_at: datetime
    cut_off: datetime
    deliver_by: datetime
    required_quals: frozenset
    temp_low: float
    temp_high: float
    package_hours: float
    drive_hours: float
    handover_open: datetime
    handover_close: datetime
    flight_depart: datetime
    flight_arrive: datetime
    last_mile_hours: float = 1.0
    min_handover_minutes: int = 45
    priority: int = 0

    def validate(self) -> None:
        if self.temp_low >= self.temp_high:
            raise ValueError("invalid temperature range")
        if self.package_hours <= 0:
            raise ValueError("package_hours must be positive")
        if self.drive_hours < 0 or self.last_mile_hours < 0:
            raise ValueError("negative leg duration")
        if self.min_handover_minutes < 0:
            raise ValueError("negative handover lead")
        if _utc(self.cut_off) < _utc(self.ready_at):
            raise ValueError("cut_off before ready_at")
        if _utc(self.handover_close) <= _utc(self.handover_open):
            raise ValueError("invalid handover window")
        if _utc(self.flight_arrive) <= _utc(self.flight_depart):
            raise ValueError("flight arrives before departure")
        if _utc(self.deliver_by) <= _utc(self.ready_at):
            raise ValueError("deliver_by not after ready_at")


@dataclass(frozen=True)
class CarrierResource:
    key: str
    quals: frozenset
    temp_min: float
    temp_max: float
    available_from: datetime
    duty_limit_hours: float
    reliability: float = 1.0
    enabled: bool = True

    def validate(self) -> None:
        if self.temp_min >= self.temp_max:
            raise ValueError("invalid vehicle temperature range")
        if self.duty_limit_hours <= 0:
            raise ValueError("duty limit must be positive")
        if not 0.0 <= self.reliability <= 1.0:
            raise ValueError("reliability outside 0..1")
        _utc(self.available_from)


@dataclass(frozen=True)
class Timeline:
    pickup: datetime
    airport_arrive: datetime
    handover_by: datetime
    delivery: datetime
    duty_hours: float
    transit_hours: float


@dataclass(frozen=True)
class Candidate:
    task_key: str
    resource_key: str
    feasible: bool
    reasons: tuple[str, ...]
    timeline: Timeline
    reliability: float
    margins: tuple[tuple[str, float], ...]

    def margin(self, name: str) -> float:
        return dict(self.margins)[name]

    @property
    def bottleneck(self) -> float:
        return min(dict(self.margins)[k] for k in TIME_MARGINS)


def evaluate(task: UrgentTask, resource: CarrierResource) -> Candidate:
    """Build one timeline for the pair and check every hard constraint against it.

    The schedule is built backwards from the flight: arriving at the airport as
    late as allowed keeps dwell, duty and thermal exposure minimal. Constraints
    interact only through this shared timeline, so feasibility is a property of
    the whole plan, never of a single check.
    """
    task.validate()
    resource.validate()
    drive = timedelta(hours=task.drive_hours)
    handover_by = min(_utc(task.handover_close),
                      _utc(task.flight_depart) - timedelta(minutes=task.min_handover_minutes))
    latest_arrive = min(handover_by, _utc(task.cut_off) + drive)
    earliest_pickup = max(_utc(task.ready_at), _utc(resource.available_from))
    earliest_arrive = earliest_pickup + drive
    arrive = latest_arrive if earliest_arrive <= latest_arrive else earliest_arrive
    pickup = arrive - drive
    delivery = _utc(task.flight_arrive) + timedelta(hours=task.last_mile_hours)
    dwell = max(0.0, (_utc(task.flight_depart) - arrive).total_seconds() / 3600.0)
    duty = task.drive_hours + dwell + task.last_mile_hours
    transit = (delivery - pickup).total_seconds() / 3600.0
    timeline = Timeline(pickup, arrive, handover_by, delivery, duty, transit)
    reasons = []
    if not resource.enabled:
        reasons.append("disabled")
    if not set(task.required_quals) <= set(resource.quals):
        reasons.append(QUALIFICATION)
    if not (resource.temp_min <= task.temp_low and task.temp_high <= resource.temp_max):
        reasons.append(TEMPERATURE)
    if pickup > _utc(task.cut_off):
        reasons.append(CUT_OFF)
    if arrive < _utc(task.handover_open) or arrive > handover_by:
        reasons.append(HANDOVER)
    if duty > resource.duty_limit_hours:
        reasons.append(DUTY)
    if transit > task.package_hours:
        reasons.append(PACKAGING)
    if delivery > _utc(task.deliver_by):
        reasons.append(DELIVERY)
    # margins measure the feasible region, not the chosen just-in-time point:
    # window widths on the arrival side, best-case consumption on duty/thermal
    margins = (
        (CUT_OFF, (_utc(task.cut_off) - earliest_pickup).total_seconds() / 3600.0),
        (HANDOVER, (handover_by - earliest_arrive).total_seconds() / 3600.0),
        (DUTY, resource.duty_limit_hours - duty),
        (PACKAGING, task.package_hours - transit),
        (DELIVERY, (_utc(task.deliver_by) - delivery).total_seconds() / 3600.0),
        (TEMPERATURE, min(task.temp_low - resource.temp_min, resource.temp_max - task.temp_high)),
    )
    return Candidate(task.key, resource.key, not reasons, tuple(reasons),
                     timeline, resource.reliability, margins)


def screen(task: UrgentTask, resources: Iterable[CarrierResource]) -> list[Candidate]:
    """Phase 1: feasibility of every resource, with rejection reasons."""
    return sorted((evaluate(task, r) for r in resources), key=lambda c: c.resource_key)


def feasible(task: UrgentTask, resources: Iterable[CarrierResource]) -> list[Candidate]:
    return [c for c in screen(task, resources) if c.feasible]


def _score(c: Candidate, margin_weight: float, reliability_weight: float) -> float:
    return (c.timeline.delivery.timestamp()
            - margin_weight * c.bottleneck
            - reliability_weight * c.reliability)


def rank(candidates: Iterable[Candidate], margin_weight: float = 1800.0,
         reliability_weight: float = 3600.0) -> list[Candidate]:
    """Phase 2: trade timeliness against risk inside the feasible set only.

    Scores are in seconds: earlier delivery is better; each hour of bottleneck
    margin is worth margin_weight seconds, full reliability reliability_weight.
    """
    rows = [c for c in candidates if c.feasible]
    return sorted(rows, key=lambda c: (_score(c, margin_weight, reliability_weight),
                                       c.timeline.delivery, c.resource_key))


def choose(task: UrgentTask, resources: Iterable[CarrierResource],
           margin_weight: float = 1800.0, reliability_weight: float = 3600.0) -> Candidate | None:
    rows = rank(feasible(task, resources), margin_weight, reliability_weight)
    return rows[0] if rows else None


@dataclass(frozen=True)
class Booking:
    task_key: str
    resource_key: str
    locked: bool = False
    revision: int = 0


def assign(tasks: Iterable[UrgentTask], resources: Iterable[CarrierResource], **weights) -> list[Booking]:
    """Dedicated runs: one resource serves at most one task."""
    rs = [r for r in resources if r.enabled]
    taken: set[str] = set()
    out: list[Booking] = []
    for t in sorted(tasks, key=lambda x: (-x.priority, _utc(x.cut_off), x.key)):
        best = choose(t, [r for r in rs if r.key not in taken], **weights)
        if best is None:
            raise ValueError(f"no feasible carrier for urgent task {t.key}")
        out.append(Booking(t.key, best.resource_key))
        taken.add(best.resource_key)
    return out


def lock(booking: Booking) -> Booking:
    if booking.locked:
        raise ValueError("booking already locked")
    return replace(booking, locked=True, revision=booking.revision + 1)


def release_lock(booking: Booking, actor: str, reason: str) -> Booking:
    """The only way a locked task may be moved: explicit, attributed, audited."""
    if not booking.locked:
        raise ValueError("booking is not locked")
    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason required to release a lock")
    return replace(booking, locked=False, revision=booking.revision + 1)


@dataclass(frozen=True)
class Stranded:
    task_key: str
    resource_key: str
    reason: str


@dataclass(frozen=True)
class ReplanOutcome:
    kept: tuple[Booking, ...]
    moved: tuple[Booking, ...]
    stranded: tuple[Stranded, ...]


def replan(bookings: Iterable[Booking], failed_resource: str, tasks: Iterable[UrgentTask],
           resources: Iterable[CarrierResource], **weights) -> ReplanOutcome:
    """A failed resource never silently moves locked work.

    Locked bookings stay bound to the failed resource and surface as stranded
    exceptions for manual disposition; only unlocked bookings are re-planned
    through the full two-phase screen, and each move bumps its revision.
    """
    rows = list(bookings)
    task_map = {t.key: t for t in tasks}
    missing = {b.task_key for b in rows} - set(task_map)
    if missing:
        raise ValueError(f"bookings reference unknown tasks: {sorted(missing)}")
    kept = tuple(b for b in rows if b.resource_key != failed_resource)
    taken = {b.resource_key for b in kept}
    pool = [r for r in resources if r.enabled and r.key != failed_resource and r.key not in taken]
    moved: list[Booking] = []
    stranded: list[Stranded] = []
    retry = []
    for b in rows:
        if b.resource_key != failed_resource:
            continue
        if b.locked:
            stranded.append(Stranded(b.task_key, b.resource_key,
                                     "resource failed: locked booking held in place"))
        else:
            retry.append(b)
    retry.sort(key=lambda b: (-task_map[b.task_key].priority,
                              _utc(task_map[b.task_key].cut_off), b.task_key))
    for b in retry:
        best = choose(task_map[b.task_key], pool, **weights)
        if best is None:
            stranded.append(Stranded(b.task_key, b.resource_key,
                                     "resource failed: no feasible replacement"))
            continue
        moved.append(Booking(b.task_key, best.resource_key, False, b.revision + 1))
        pool = [r for r in pool if r.key != best.resource_key]
    return ReplanOutcome(kept, tuple(moved), tuple(stranded))


def verify(bookings: Iterable[Booking], tasks: Iterable[UrgentTask],
           resources: Iterable[CarrierResource]) -> list[Stranded]:
    """Re-check every booking against current constraints, e.g. after a failure."""
    task_map = {t.key: t for t in tasks}
    res_map = {r.key: r for r in resources}
    out = []
    for b in bookings:
        t, r = task_map.get(b.task_key), res_map.get(b.resource_key)
        if t is None or r is None:
            out.append(Stranded(b.task_key, b.resource_key, "unknown task or resource"))
            continue
        c = evaluate(t, r)
        if not c.feasible:
            out.append(Stranded(b.task_key, b.resource_key, ",".join(c.reasons)))
    return out


def explain(candidate: Candidate) -> str:
    if candidate.feasible:
        return (f"{candidate.task_key} -> {candidate.resource_key}: feasible, "
                f"delivers {candidate.timeline.delivery.isoformat()}, "
                f"bottleneck margin {candidate.bottleneck:.2f}h")
    return f"{candidate.task_key} -> {candidate.resource_key}: infeasible ({', '.join(candidate.reasons)})"
