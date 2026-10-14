"""YAML-план: разбор, наследование значений по дереву, проверка формы.

Формат — см. README.md. Коротко:

    defaults: {assignee: me, type: Задание, priority: Обычное}
    projects:
      - identifier: my-sub
        name: Мой подпроект
        parent: my-root
    items:
      - key: epic-1
        project: my-root
        type: Сводка задачи
        subject: ...
        defaults: {category: Бэкенд, sprint: Спринт 1}   # для потомков
        children:
          - key: task-1
            subject: ...
            start: 2026-10-07
            due: 2026-10-08
            estimate: 6h
            follows: [other-key]
      - id: 84            # существующая задача: меняются только перечисленные поля
        parent: epic-1
    sprints:              # спринты проекта: API их не создаёт — apply сверяет с сервером
      - {name: Спринт 1, start: 2026-10-06, finish: 2026-10-16}
    calendar:
      hours_per_day: 8
      schedule: true      # даты задач без дат — по оценке, связям и спринту (schedule.py)
      non_working: {2026-11-04: Перенос выходного}   # apply добавляет в календарь экземпляра
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Поля, которые наследуются от defaults (плана или узла-предка).
INHERITABLE = {"project", "type", "status", "priority", "assignee", "responsible",
               "category", "sprint", "version"}
# Поля, которыми управляет opsync. Статус задаётся только при создании:
# дальше его двигают люди, и повторный прогон не должен откатывать их работу.
MANAGED = {"subject", "description", "type", "priority", "assignee", "responsible",
           "category", "sprint", "version", "parent", "start", "due", "date",
           "days", "estimate", "project"}
KNOWN = MANAGED | {"key", "id", "status", "children", "defaults", "follows", "precedes", "auto"}


TOP_LEVEL = {"name", "defaults", "projects", "items", "sprints", "calendar"}


class PlanError(ValueError):
    pass


@dataclass
class ProjectSpec:
    identifier: str
    name: str
    parent: str | None = None
    description: str | None = None
    members: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)   # роли участников; иначе — ваши в родителе


@dataclass
class SprintSpec:
    name: str
    start: dt.date
    finish: dt.date
    project: str                         # где спринт заведён; подпроектам — через доступ


@dataclass
class CalendarSpec:
    hours_per_day: float = 8
    schedule: bool = False               # apply ставит даты задачам без дат (schedule.py)
    lanes: dict[str, str] = field(default_factory=dict)  # категория → параллельная дорожка
    capacity: dict[str, float] = field(default_factory=dict)  # дорожка → часов в день
    non_working: dict[dt.date, str] = field(default_factory=dict)


@dataclass
class Node:
    key: str
    fields: dict[str, Any]
    parent_key: str | None
    adopt_id: int | None = None          # существующая задача
    explicit: set[str] = field(default_factory=set)  # поля, заданные в YAML явно
    has_children: bool = False

    @property
    def follows(self) -> list[str]:
        return list(self.fields.get("follows") or [])

    @property
    def precedes(self) -> list[str]:
        return list(self.fields.get("precedes") or [])

    @property
    def label(self) -> str:
        return self.fields.get("subject") or f"#{self.adopt_id}"


@dataclass
class Plan:
    name: str
    path: Path
    projects: list[ProjectSpec]
    nodes: dict[str, Node]               # в порядке обхода: родитель раньше детей
    sprints: list[SprintSpec] = field(default_factory=list)
    calendar: CalendarSpec = field(default_factory=CalendarSpec)

    def sprint(self, name: str | None) -> SprintSpec | None:
        return next((s for s in self.sprints if s.name == name), None)

    def ordered(self) -> list[Node]:
        return list(self.nodes.values())


def load(path: Path) -> Plan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = set(raw) - TOP_LEVEL
    if unknown:
        raise PlanError(f"неизвестные разделы плана {sorted(unknown)}")
    defaults = raw.get("defaults") or {}
    projects = [ProjectSpec(**p) for p in raw.get("projects") or []]
    nodes: dict[str, Node] = {}
    for item in raw.get("items") or []:
        _walk(item, defaults, None, nodes)
    _check_refs(nodes)
    return Plan(name=raw.get("name") or path.stem, path=path, projects=projects, nodes=nodes,
                sprints=_sprints(raw.get("sprints") or [], defaults.get("project")),
                calendar=_calendar(raw.get("calendar") or {}))


def _sprints(raw: list[dict], default_project: str | None) -> list[SprintSpec]:
    out: list[SprintSpec] = []
    for s in raw:
        name = s.get("name")
        unknown = set(s) - {"name", "start", "finish", "project"}
        if not name or unknown:
            raise PlanError(f"спринт {name!r}: нужны name, start, finish"
                            + (f"; неизвестные поля {sorted(unknown)}" if unknown else ""))
        start, finish = s.get("start"), s.get("finish")
        if not isinstance(start, dt.date) or not isinstance(finish, dt.date):
            raise PlanError(f"спринт «{name}»: start и finish — даты YYYY-MM-DD")
        if start > finish:
            raise PlanError(f"спринт «{name}»: start позже finish")
        project = s.get("project") or default_project
        if not project:
            raise PlanError(f"спринт «{name}»: не указан project")
        if any(o.name == name for o in out):
            raise PlanError(f"повтор спринта «{name}»")
        out.append(SprintSpec(name, start, finish, project))
    return out


def _calendar(raw: dict) -> CalendarSpec:
    unknown = set(raw) - {"hours_per_day", "non_working", "schedule", "lanes", "capacity"}
    if unknown:
        raise PlanError(f"calendar: неизвестные поля {sorted(unknown)}")
    days = raw.get("non_working") or {}
    if not isinstance(days, dict) or not all(isinstance(d, dt.date) for d in days):
        raise PlanError("calendar.non_working: словарь «дата: название»")
    hpd = float(raw.get("hours_per_day", 8))
    if hpd <= 0:
        raise PlanError("calendar.hours_per_day должно быть больше 0")
    lanes = raw.get("lanes") or {}
    if not isinstance(lanes, dict):
        raise PlanError("calendar.lanes: словарь «категория: дорожка»")
    return CalendarSpec(hours_per_day=hpd, schedule=bool(raw.get("schedule", False)),
                        non_working={d: str(n or "") for d, n in days.items()},
                        lanes={str(k): str(v) for k, v in lanes.items()},
                        capacity=_capacity(raw.get("capacity") or {}))


def _capacity(raw: dict) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise PlanError("calendar.capacity: словарь «дорожка: часов в день»")
    out = {str(k): float(v) for k, v in raw.items()}
    if any(v <= 0 for v in out.values()):
        raise PlanError("calendar.capacity: часов в день должно быть больше 0")
    return out


def _walk(item: dict, inherited: dict, parent_key: str | None, nodes: dict[str, Node]) -> None:
    unknown = set(item) - KNOWN
    if unknown:
        raise PlanError(f"{item.get('key') or item.get('id')}: неизвестные поля {sorted(unknown)}")
    adopt_id = item.get("id")
    key = item.get("key") or (f"#{adopt_id}" if adopt_id else None)
    if not key:
        raise PlanError(f"у элемента нет key: {item.get('subject')!r}")
    if key in nodes:
        raise PlanError(f"повтор key: {key}")
    explicit = {k for k in item if k in MANAGED or k == "status"}
    fields: dict[str, Any] = {}
    if adopt_id is None:
        fields.update({k: v for k, v in inherited.items() if k in INHERITABLE})
    fields.update({k: v for k, v in item.items() if k not in {"children", "defaults"}})
    if "parent" in item:
        parent_key = item["parent"]
    elif adopt_id is not None and parent_key:
        explicit.add("parent")
    if adopt_id is None and not fields.get("subject"):
        raise PlanError(f"{key}: нет subject")
    _check_dates(key, fields)
    children = item.get("children") or []
    node = Node(key=key, fields=fields, parent_key=parent_key, adopt_id=adopt_id,
                explicit=explicit, has_children=bool(children))
    nodes[key] = node
    # Тип и статус узла детям не передаются: сводка не делает детей сводками.
    own = {k: v for k, v in fields.items() if k in INHERITABLE - {"type", "status"}}
    child_inherited = {**inherited, **own}
    child_inherited.update(item.get("defaults") or {})
    for child in children:
        _walk(child, child_inherited, key, nodes)


def _check_dates(key: str, f: dict) -> None:
    for name in ("start", "due", "date"):
        if name in f and not isinstance(f[name], dt.date):
            raise PlanError(f"{key}: {name} должно быть датой YYYY-MM-DD")
    if "date" in f and ("start" in f or "due" in f):
        raise PlanError(f"{key}: для вехи используйте только date")
    if f.get("start") and f.get("due") and f["start"] > f["due"]:
        raise PlanError(f"{key}: start позже due")


def _check_refs(nodes: dict[str, Node]) -> None:
    for n in nodes.values():
        if n.parent_key and n.parent_key not in nodes:
            raise PlanError(f"{n.key}: parent {n.parent_key!r} не найден в плане")
        for ref in n.follows + n.precedes:
            if ref not in nodes:
                raise PlanError(f"{n.key}: связь с {ref!r} — такого key нет")


_DURATION = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(h|d|m|min|ч|д|м|мин)?\s*$", re.I)


def hours(value: Any, hours_per_day: float = 8) -> float:
    """'4h', '0.5d', '1,5д', '30m', 6 → часы."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION.match(str(value))
    if not m:
        raise PlanError(f"не понимаю длительность {value!r}: нужно 4h, 0.5d или 30m")
    num = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "h").lower()
    if unit in ("d", "д"):
        return num * hours_per_day
    if unit in ("m", "min", "м", "мин"):
        return num / 60
    return num


def iso_hours(h: float) -> str:
    minutes = round(h * 60)
    hh, mm = divmod(minutes, 60)
    return f"PT{hh}H" + (f"{mm}M" if mm else "")


_ISO = re.compile(r"^P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def iso_to_hours(value: str) -> float:
    """ISO 8601 длительность → часы. OpenProject пишет 24 ч как P1D: день = 24 ч."""
    m = _ISO.match(value or "")
    if not m:
        return -1.0
    d, h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
    return d * 24 + h + mi / 60 + sec / 3600
