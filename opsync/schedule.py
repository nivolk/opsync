"""Планировщик дат: оценка, связи, спринты и рабочий календарь → start/due задач.

Задачи плана делятся на:
- закреплённые — заданы и `start`, и `due`: даты не трогаются, часы оценки занимают время
  исполнителя (поровну на рабочие дни отрезка);
- вехи — `date`;
- закрытые на сервере — с их датами с сервера: сделанная работа не переписывается;
- планируемые — без `due`: `start` (если есть) значит «не раньше». Оценка раскладывается по
  рабочим дням исполнителя (`hours_per_day` в день), свободные часы в уже занятых днях
  заполняются. Начало — не раньше начала спринта и дня после всех предшественников.

Время исполнителя делится по дорожкам (`calendar.lanes`: категория → дорожка): один человек
с двумя агентами — frontend и backend — работает в двух дорожках параллельно, по
`hours_per_day` в каждой (`calendar.capacity` — своя ёмкость дорожки). Категории без
дорожки — в общей дорожке исполнителя.

Результат детерминирован: «сегодня» в расчёт не входит, иначе каждый `apply` двигал бы даты.
Проблемы (выход за спринт, перегрузка, порядок связей) — предупреждения, не ошибки.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .plan import Node, Plan, PlanError, hours

DAY = dt.timedelta(days=1)


@dataclass
class Calendar:
    hours_per_day: float = 8
    weekend: frozenset[int] = frozenset({6, 7})          # isoweekday
    holidays: frozenset[dt.date] = frozenset()

    def working(self, d: dt.date) -> bool:
        return d.isoweekday() not in self.weekend and d not in self.holidays

    def next_working(self, d: dt.date) -> dt.date:
        """Сам день, если рабочий, иначе ближайший рабочий после него."""
        for _ in range(366):
            if self.working(d):
                return d
            d += DAY
        raise PlanError("в календаре нет рабочих дней на год вперёд")

    def working_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        out, d = [], start
        while d <= end:
            if self.working(d):
                out.append(d)
            d += DAY
        return out


@dataclass
class Window:
    name: str
    start: dt.date
    finish: dt.date


@dataclass
class Slot:
    key: str
    kind: str                            # план | закреплено | веха | закрыта | бэклог | без дат
    start: dt.date | None
    due: dt.date | None
    hours: float | None
    assignee: str | None
    sprint: str | None


@dataclass
class Schedule:
    slots: dict[str, Slot] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    load: dict[str, dict[dt.date, float]] = field(default_factory=dict)  # исполнитель → день → ч
    windows: dict[str, Window] = field(default_factory=dict)            # спринты задач плана

    def planned(self) -> dict[str, tuple[dt.date, dt.date]]:
        """Даты, которые планировщик назначил сам (для apply)."""
        return {k: (s.start, s.due) for k, s in self.slots.items()
                if s.kind == "план" and s.start and s.due}


def schedule(plan: Plan, cal: Calendar,
             window: Callable[[Node], Window | None] = lambda n: None,
             closed: dict[str, tuple[dt.date | None, dt.date | None]] | None = None) -> Schedule:
    """closed: key → (start, due) задач, закрытых на сервере, — их даты не пересчитываются."""
    out = Schedule()
    closed = closed or {}
    leaves = [n for n in plan.ordered() if not n.has_children]
    deps = _deps(plan, leaves)
    order = _topo(leaves, deps)
    used: dict[str, dict[dt.date, float]] = defaultdict(lambda: defaultdict(float))
    ends: dict[str, dt.date | None] = {}

    lanes = plan.calendar.lanes

    def who(n: Node) -> str | None:
        """Исполнитель и его дорожка: «me» или «me/бэкенд»."""
        a = n.fields.get("assignee")
        if not a:
            return None
        lane = lanes.get(n.fields.get("category") or "")
        return f"{str(a).lower()}/{lane}" if lane else str(a).lower()

    def per_day(person: str | None) -> float:
        lane = person.split("/", 1)[1] if person and "/" in person else None
        return plan.calendar.capacity.get(lane or "", cal.hours_per_day)

    def estimate(n: Node) -> float | None:
        e = n.fields.get("estimate")
        return None if e is None else hours(e, cal.hours_per_day)

    # 1. закреплённые задачи занимают время исполнителя до планирования остальных
    for n in leaves:
        f = n.fields
        if n.key in closed:
            continue
        if f.get("start") and f.get("due") and who(n) and estimate(n):
            days = cal.working_days(f["start"], f["due"])
            for d in days:
                used[who(n)][d] += estimate(n) / len(days)

    # 2. в порядке связей
    for n in order:
        f = n.fields
        w = window(n)
        sprint = w.name if w else f.get("sprint")
        if w:
            out.windows[w.name] = w
        known = [ends[k] for k in deps[n.key] if ends.get(k)]
        pred_end = max(known) if known else None
        est = estimate(n)
        if n.key in closed:
            start, due = closed[n.key]
            ends[n.key] = due
            out.slots[n.key] = Slot(n.key, "закрыта", start, due, est, who(n), sprint)
            continue
        if f.get("date"):
            ends[n.key] = f["date"]
            if pred_end and f["date"] <= pred_end:
                out.warnings.append(f"{n.key}: веха {f['date']:%d.%m} не позже предшественников "
                                    f"(они заканчиваются {pred_end:%d.%m})")
            out.slots[n.key] = Slot(n.key, "веха", f["date"], f["date"], None, who(n), sprint)
            continue
        if n.adopt_id or f.get("due"):
            start, due = f.get("start"), f.get("due")
            ends[n.key] = due
            if n.adopt_id and not due:
                out.slots[n.key] = Slot(n.key, "без дат", start, due, est, who(n), sprint)
                continue
            if pred_end and start and start <= pred_end:
                out.warnings.append(f"{n.key}: начало {start:%d.%m} не после предшественников "
                                    f"({pred_end:%d.%m})")
            capacity = len(cal.working_days(start, due)) * per_day(who(n)) if start else None
            if est and capacity is not None and est > capacity:
                out.warnings.append(f"{n.key}: оценка {est:g} ч не помещается в "
                                    f"{start:%d.%m}–{due:%d.%m}")
            _check_window(n.key, start, due, w, out)
            out.slots[n.key] = Slot(n.key, "закреплено", start, due, est, who(n), sprint)
            continue

        # планируемая задача
        anchors = [d for d in (f.get("start"), w.start if w else None,
                               pred_end + DAY if pred_end else None) if d]
        if not anchors:                  # бэклог: ни спринта, ни start, ни связей — без дат
            ends[n.key] = None
            out.slots[n.key] = Slot(n.key, "бэклог", None, None, est, who(n), sprint)
            continue
        earliest = cal.next_working(max(anchors))
        days_field = f.get("days")
        if est is None and not days_field:
            out.warnings.append(f"{n.key}: нет оценки (estimate) — даты не рассчитаны")
            ends[n.key] = None
            out.slots[n.key] = Slot(n.key, "без дат", None, None, None, who(n), sprint)
            continue
        if est is None:                  # длительность в рабочих днях, без учёта загрузки
            start, due = earliest, earliest
            for _ in range(int(days_field) - 1):
                due = cal.next_working(due + DAY)
        else:
            # без исполнителя задачи не делят время между собой
            calendar = used[who(n)] if who(n) else defaultdict(float)
            start, due = _allocate(earliest, est, cal, calendar, per_day(who(n)))
        ends[n.key] = due
        _check_window(n.key, start, due, w, out)
        out.slots[n.key] = Slot(n.key, "план", start, due, est, who(n), sprint)

    for person, days in used.items():
        out.load[person] = dict(sorted(days.items()))
        limit = per_day(person)
        over = {d: h for d, h in out.load[person].items() if h > limit + 0.01}
        if over:
            peak = max(over.values())
            out.warnings.append(
                f"перегрузка {person}: {len(over)} дн. с {min(over):%d.%m} по {max(over):%d.%m}, "
                f"до {peak:.1f} ч в день при {limit:g} — это закреплённые даты, "
                "планировщик их не двигает")
    return out


def _allocate(earliest: dt.date, est: float, cal: Calendar,
              used: dict[dt.date, float], per_day: float) -> tuple[dt.date, dt.date]:
    """Часы оценки — в свободное время исполнителя, день за днём начиная с earliest."""
    d = cal.next_working(earliest)
    if est <= 0:
        return d, d
    first = last = None
    left = est
    while left > 0.001:
        free = per_day - used[d]
        if free > 0.001:
            take = min(free, left)
            used[d] += take
            left -= take
            first, last = first or d, d
        d = cal.next_working(d + DAY)
    return first, last


def _check_window(key: str, start, due, w: Window | None, out: Schedule) -> None:
    if not w:
        return
    if (start and start < w.start) or (due and due > w.finish):
        out.warnings.append(f"{key}: {_span(start, due)} выходит за «{w.name}» "
                            f"{w.start:%d.%m}–{w.finish:%d.%m}")


def _span(a, b) -> str:
    return "–".join(f"{d:%d.%m}" for d in (a, b) if d)


def _descendant_leaves(plan: Plan, key: str) -> list[str]:
    node = plan.nodes[key]
    if not node.has_children:
        return [key]
    out: list[str] = []
    for n in plan.ordered():
        if n.parent_key == key:
            out += _descendant_leaves(plan, n.key)
    return out


def _deps(plan: Plan, leaves: list[Node]) -> dict[str, set[str]]:
    """Лист → листья, которые должны закончиться раньше (связи сводок — через их листья)."""
    deps: dict[str, set[str]] = {n.key: set() for n in leaves}
    for n in plan.ordered():
        pairs = [(n.key, e) for e in n.follows] + [(later, n.key) for later in n.precedes]
        for later, earlier in pairs:
            for a in _descendant_leaves(plan, later):
                deps[a].update(_descendant_leaves(plan, earlier))
    return deps


def _topo(leaves: list[Node], deps: dict[str, set[str]]) -> list[Node]:
    position = {n.key: i for i, n in enumerate(leaves)}
    done: set[str] = set()
    out: list[Node] = []
    pending = list(leaves)
    while pending:
        ready = [n for n in pending if deps[n.key] <= done]
        if not ready:
            raise PlanError("цикл в связях follows/precedes: "
                            + ", ".join(sorted(n.key for n in pending)))
        n = min(ready, key=lambda x: position[x.key])
        out.append(n)
        done.add(n.key)
        pending.remove(n)
    return out
