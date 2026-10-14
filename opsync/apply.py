"""Приведение OpenProject к плану: проекты → задачи → связи → режим планирования."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog, instance_calendar
from .client import ApiError, Client, href_id
from .config import state_path
from .plan import Node, Plan, ProjectSpec, hours, iso_hours, iso_to_hours
from .schedule import Calendar, Schedule, Window, schedule

API = "/api/v3"


@dataclass
class Report:
    problems: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unchanged: int = 0

    def problem(self, where: str, what: str) -> None:
        self.problems[where].add(what)


class Applier:
    def __init__(self, client: Client, plan: Plan, dry_run: bool, allow_missing: bool):
        self.c = client
        self.plan = plan
        self.dry = dry_run
        self.allow_missing = allow_missing
        self.cat = Catalog.load(client)
        self.report = Report()
        self.state_path = state_path(plan.path, plan.name)
        self.state: dict[str, int] = (
            json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        )
        self.ids: dict[str, int] = {}           # key → id (известные к этому моменту)
        self.wps: dict[int, dict] = {}          # id → текущее представление
        self.pending_projects: set[str] = set()  # будут созданы (dry-run)
        self._types_cache: dict[int, set[str]] = {}
        self._dated: set[int] = set()           # даты уже в отчёте update (для dry-run)

    # ------------------------------------------------------------------ run
    def run(self) -> Report:
        self.ensure_projects()
        self.check_sprints()
        self.ensure_calendar()
        if self.plan.calendar.schedule:
            self.apply_schedule()
        payloads = self.payloads = {n.key: self.resolve(n) for n in self.plan.ordered()}
        if self.report.problems and not self.allow_missing:
            return self.report
        self.match_existing()
        for node in self.plan.ordered():
            self.upsert(node, payloads[node.key])
        self.ensure_relations()
        self.finalize_scheduling()
        return self.report

    # ------------------------------------------------------------- projects
    def ensure_projects(self) -> None:
        for spec in self.plan.projects:
            if spec.identifier in self.cat.projects:
                continue
            if not spec.roles and not spec.parent:
                self.report.problem(f"проект {spec.identifier}",
                                    "нет parent — укажите roles участников")
                continue
            for name in spec.roles:
                self._lookup(lambda: self.cat.role(name), f"проект {spec.identifier}", "роль")
            if self.report.problems:
                continue
            if self.dry:
                self.pending_projects.add(spec.identifier)
                self.report.created.append(f"проект «{spec.name}»")
                continue
            self.create_project(spec)
        if not self.dry:
            self.cat.reload_projects()

    def create_project(self, spec: ProjectSpec) -> None:
        body: dict[str, Any] = {"name": spec.name, "identifier": spec.identifier, "_links": {}}
        if spec.description:
            body["description"] = {"format": "markdown", "raw": spec.description}
        if spec.parent:
            parent = self.cat.project(spec.parent)
            body["_links"]["parent"] = {"href": parent["_links"]["self"]["href"]}
        project = self.c.post("/projects", body)
        self.report.created.append(f"проект «{spec.name}»")
        self.cat.reload_projects()
        # создатель-админ участником не становится: добавляем себя с ролями из плана или
        # из родителя — без участия нельзя назначить на себя задачи
        if spec.roles:
            roles = [{"href": self.cat.role(r)["_links"]["self"]["href"]} for r in spec.roles]
        else:
            roles = self.my_roles(spec.parent)
        for who in ["me", *spec.members]:
            user = self.cat.user(who)
            self.c.post("/memberships", {"_links": {
                "principal": {"href": user["_links"]["self"]["href"]},
                "project": {"href": project["_links"]["self"]["href"]},
                "roles": roles,
            }})

    # -------------------------------------------------------------- sprints
    def check_sprints(self) -> None:
        """Спринты API не создаёт и не правит — сверяем и говорим, что сделать руками."""
        for spec in self.plan.sprints:
            if spec.project in self.pending_projects:
                where = f"проект {spec.project}"
                self.report.problem(where, _sprint_todo(spec))
                continue
            project = self._lookup(lambda: self.cat.project(spec.project), spec.name, "проект")
            if not project:
                continue
            where = f"проект «{project['name']}»"
            sp = self.cat.sprints(project["id"]).get(spec.name)
            if not sp:
                self.report.problem(where, _sprint_todo(spec))
            elif (sp.get("startDate"), sp.get("finishDate")) != (spec.start.isoformat(),
                                                                spec.finish.isoformat()):
                self.report.problem(where, f"поменять даты спринта «{spec.name}»: сейчас "
                                           f"{sp.get('startDate')}…{sp.get('finishDate')}, "
                                           f"по плану {spec.start}…{spec.finish}")

    # ------------------------------------------------------------- calendar
    def ensure_calendar(self) -> None:
        """Нерабочие дни из плана → календарь экземпляра (общий для всех проектов)."""
        wanted = self.plan.calendar.non_working
        if not wanted:
            return
        span = [min(wanted).isoformat(), max(wanted).isoformat()]
        flt = [{"date": {"operator": "<>d", "values": span}}]
        existing = {d["date"] for d in self.c.collection("/days/non_working", filters=flt)}
        for day, name in sorted(wanted.items()):
            if day.isoformat() in existing:
                continue
            label = f"нерабочий день {day:%d.%m.%Y}" + (f" ({name})" if name else "")
            if self.dry:
                self.report.created.append(label)
                continue
            try:
                self.c.post("/days/non_working", {"_type": "NonWorkingDay",
                                                  "date": day.isoformat(),
                                                  "name": name or "Нерабочий день"})
            except ApiError as e:
                if e.status != 403:
                    raise
                self.report.problem("экземпляр", f"{label}: нужны права администратора")
                continue
            self.report.created.append(label)

    # ------------------------------------------------------------- schedule
    def apply_schedule(self) -> None:
        """Задачам без дат — даты из планировщика; в плане они остаются без дат."""
        result = plan_schedule(self.c, self.cat, self.plan)
        self.report.warnings += result.warnings
        for key, (start, due) in result.planned().items():
            self.plan.nodes[key].fields.update(start=start, due=due)

    def my_roles(self, project_ref: str) -> list[dict]:
        pid = self.cat.project(project_ref)["id"]
        flt = [{"project": {"operator": "=", "values": [str(pid)]}},
               {"principal": {"operator": "=", "values": [str(self.cat.me["id"])]}}]
        for m in self.c.collection("/memberships", filters=flt):
            return [{"href": r["href"]} for r in m["_links"]["roles"]]
        raise LookupError(f"вы не участник проекта {project_ref}")

    # -------------------------------------------------------------- resolve
    def resolve(self, n: Node) -> dict:
        """Имена из плана → тело запроса. Нехватку настроек копит в отчёт."""
        f = n.fields
        managed = n.explicit if n.adopt_id else set(f)
        body: dict[str, Any] = {"_links": {}}
        links = body["_links"]
        project_ref = f.get("project")
        if not project_ref and not n.adopt_id:
            self.report.problem(n.key, "не указан project")
            return body
        pending = project_ref in self.pending_projects
        project = None if pending or not project_ref else self._lookup(
            lambda: self.cat.project(project_ref), n.key, "проект")
        pid = project["id"] if project else None
        where = f"проект «{project['name']}»" if project else f"проект {project_ref}"

        if "subject" in managed:
            body["subject"] = f["subject"]
        if "description" in managed:
            body["description"] = {"format": "markdown", "raw": (f.get("description") or "").strip()}
        if project and "project" in managed:
            links["project"] = {"href": project["_links"]["self"]["href"]}

        type_name = f.get("type")
        if type_name and "type" in managed:
            t = self.cat.types.get(type_name)
            if not t:
                self.report.problem("экземпляр", f"нет типа «{type_name}»")
            else:
                links["type"] = {"href": t["_links"]["self"]["href"]}
                if pid and not pending and type_name not in self._project_types(pid):
                    self.report.problem(where, f"включить тип «{type_name}»")
        if not n.adopt_id and f.get("status"):
            s = self.cat.statuses.get(f["status"])
            if s:
                links["status"] = {"href": s["_links"]["self"]["href"]}
            else:
                self.report.problem("экземпляр", f"нет статуса «{f['status']}»")
        if f.get("priority") and "priority" in managed:
            p = self.cat.priorities.get(f["priority"])
            if p:
                links["priority"] = {"href": p["_links"]["self"]["href"]}
            else:
                self.report.problem("экземпляр", f"нет приоритета «{f['priority']}»")
        for role in ("assignee", "responsible"):
            if role in managed and f.get(role):
                user = self._lookup(lambda: self.cat.user(f[role]), n.key, "пользователь")
                if user:
                    links[role] = {"href": user["_links"]["self"]["href"]}
                    if pid and user["name"].lower() not in self.cat.members(pid):
                        self.report.problem(where, f"добавить участника «{user['name']}»")
        if "category" in managed and f.get("category"):
            if pending:
                self.report.problem(where, f"создать категорию «{f['category']}»")
            elif pid:
                cat = self.cat.categories(pid).get(f["category"])
                if cat:
                    links["category"] = {"href": cat["_links"]["self"]["href"]}
                else:
                    self.report.problem(where, f"создать категорию «{f['category']}»")
        if "sprint" in managed and f.get("sprint") and not n.has_children:
            if pending:
                self.report.problem(where, f"открыть доступ к спринту «{f['sprint']}»")
            elif pid:
                sp = self.cat.sprints(pid).get(f["sprint"])
                spec = self.plan.sprint(f["sprint"])
                if sp:
                    links["sprint"] = {"href": sp["_links"]["self"]["href"]}
                elif spec and spec.project != project_ref and spec.project not in (
                        project["identifier"], project["name"]):
                    self.report.problem(where, f"открыть доступ к спринту «{f['sprint']}» "
                                               "(Настройки проекта → Бэклоги)")
                elif not spec:
                    self.report.problem(where, f"спринт «{f['sprint']}» не виден")
                # спринт описан в плане, но не создан — об этом уже сказал check_sprints
        if "version" in managed and f.get("version") and pid:
            v = self.cat.versions(pid).get(f["version"])
            if v:
                links["targetVersions"] = [{"href": v["_links"]["self"]["href"]}]
            else:
                self.report.problem(where, f"создать версию «{f['version']}»")

        self._dates(n, f, managed, body)
        if "estimate" in managed and f.get("estimate") is not None and not n.has_children:
            per_day = self.plan.calendar.hours_per_day
            body["estimatedTime"] = iso_hours(hours(f["estimate"], per_day))
        return body

    def _dates(self, n: Node, f: dict, managed: set[str], body: dict) -> None:
        if f.get("date") is not None and "date" in managed:
            body["date"] = f["date"].isoformat()          # веха
            body["scheduleManually"] = True
            return
        if n.has_children:
            return                                        # даты сводки — от детей
        if "start" in managed and f.get("start"):
            body["startDate"] = f["start"].isoformat()
            body["scheduleManually"] = True
        if "due" in managed and f.get("due"):
            body["dueDate"] = f["due"].isoformat()
            body["scheduleManually"] = True
        if "days" in managed and f.get("days") and not f.get("due"):
            body["duration"] = f"P{int(f['days'])}D"

    def _lookup(self, fn, key: str, kind: str):
        try:
            return fn()
        except LookupError as e:
            self.report.problem(key, f"{kind}: {e}")
            return None

    def _project_types(self, pid: int) -> set[str]:
        if pid not in self._types_cache:
            data = self.c.get(f"/projects/{pid}/types")
            self._types_cache[pid] = {t["name"] for t in data["_embedded"]["elements"]}
        return self._types_cache[pid]

    # ---------------------------------------------------------------- match
    def match_existing(self) -> None:
        """Ключ → id: явный id, файл состояния, затем совпадение темы в проекте."""
        by_subject: dict[tuple[int, str], int] = {}
        for n in self.plan.ordered():
            if n.adopt_id:
                self.ids[n.key] = n.adopt_id
            elif n.key in self.state:
                self.ids[n.key] = self.state[n.key]
        projects = {f for n in self.plan.ordered() if (f := n.fields.get("project"))
                    and f not in self.pending_projects}
        for ref in projects:
            pid = self.cat.project(ref)["id"]
            flt = [{"status": {"operator": "*", "values": []}},
                   {"project": {"operator": "=", "values": [str(pid)]}}]
            for wp in self.c.collection(f"/projects/{pid}/work_packages", filters=flt):
                self.wps[wp["id"]] = wp
                by_subject.setdefault((pid, wp["subject"]), wp["id"])
        claimed = set(self.ids.values())
        for n in self.plan.ordered():
            if n.key in self.ids or not n.fields.get("project") \
                    or n.fields["project"] in self.pending_projects:
                continue
            pid = self.cat.project(n.fields["project"])["id"]
            wid = by_subject.get((pid, n.fields["subject"]))
            if wid and wid not in claimed:
                self.ids[n.key] = wid
                claimed.add(wid)
        for key, wid in list(self.ids.items()):
            if wid not in self.wps:
                try:
                    self.wps[wid] = self.c.get(f"/work_packages/{wid}")
                except ApiError as e:
                    if e.status != 404:
                        raise
                    del self.ids[key]          # удалили руками — создадим заново

    # --------------------------------------------------------------- upsert
    def upsert(self, n: Node, body: dict) -> None:
        body = json.loads(json.dumps(body))
        if n.parent_key:
            pid = self.ids.get(n.parent_key)
            if pid:
                body["_links"]["parent"] = {"href": f"{API}/work_packages/{pid}"}
            elif not self.dry:
                raise RuntimeError(f"{n.key}: родитель {n.parent_key} не создан")
        wid = self.ids.get(n.key)
        if wid is None:
            if n.adopt_id:
                self.report.problem(n.key, f"задача #{n.adopt_id} не найдена")
                return
            self.create(n, body)
        else:
            self.update(n, wid, body)

    def create(self, n: Node, body: dict) -> None:
        project = body["_links"].get("project", {}).get("href")
        self.report.created.append(f"{n.fields.get('type', '')}: {n.label}")
        if self.dry:
            self.ids[n.key] = -len(self.report.created)  # фиктивный id для потомков
            if project:
                self.validate(n, project, body)
            return
        wp = self.c.post(f"/projects/{href_id(project)}/work_packages", body)
        self.ids[n.key] = wp["id"]
        self.wps[wp["id"]] = wp
        self.state[n.key] = wp["id"]
        self.save_state()

    def validate(self, n: Node, project_href: str, body: dict) -> None:
        """Пробный прогон: сервер проверяет тело через форму, ничего не создавая."""
        probe = json.loads(json.dumps(body))
        probe["_links"].pop("parent", None)        # родителя ещё нет
        form = self.c.post(f"/projects/{href_id(project_href)}/work_packages/form", probe)
        for field_name, err in (form["_embedded"].get("validationErrors") or {}).items():
            self.report.problem(n.key, f"{field_name}: {err.get('message')}")

    def update(self, n: Node, wid: int, body: dict) -> None:
        current = self.wps.get(wid) or self.c.get(f"/work_packages/{wid}")
        if not n.adopt_id:
            self.state[n.key] = wid
        # Статус из плана — только начальный. Дальше его двигают люди и `opsync start/
        # review/done`; без этого повторный apply откатывал бы задачи обратно в «Новое».
        body["_links"].pop("status", None)
        diff = _diff(current, body, skip_dates=_wants_auto(n))
        if not diff:
            self.report.unchanged += 1
            return
        label = n.fields.get("subject") or current["subject"]
        self.report.updated.append(f"#{wid} {label}: {', '.join(sorted(_names(diff)))}")
        if {"startDate", "dueDate", "date"} & set(diff):
            self._dated.add(wid)
        if self.dry:
            return
        # правка соседей пересчитывает даты и lockVersion — берём свежую версию
        diff["lockVersion"] = self.c.get(f"/work_packages/{wid}")["lockVersion"]
        self.wps[wid] = self.c.patch(f"/work_packages/{wid}", diff)
        self.save_state()

    # ------------------------------------------------------------ relations
    def ensure_relations(self) -> None:
        pairs: list[tuple[str, str]] = []  # (позже, раньше)
        for n in self.plan.ordered():
            pairs += [(n.key, p) for p in n.follows]
            pairs += [(s, n.key) for s in n.precedes]
        existing: dict[int, set[int]] = {}
        for later, earlier in pairs:
            a, b = self.ids.get(later), self.ids.get(earlier)
            label = f"{self.plan.nodes[earlier].label} → {self.plan.nodes[later].label}"
            if a is None or b is None or a < 0 or b < 0:
                self.report.relations.append(label)
                continue
            if a not in existing:
                existing[a] = self._predecessors(a)
            if b in existing[a]:
                continue
            self.report.relations.append(label)
            if not self.dry:
                self.c.post(f"/work_packages/{a}/relations", {
                    "type": "follows", "_links": {"to": {"href": f"{API}/work_packages/{b}"}}})
                existing[a].add(b)

    def _predecessors(self, wid: int) -> set[int]:
        out = set()
        data = self.c.get(f"/work_packages/{wid}/relations")
        for r in data.get("_embedded", {}).get("elements", []):
            frm, to = href_id(r["_links"]["from"]["href"]), href_id(r["_links"]["to"]["href"])
            if r["type"] == "follows" and frm == wid:
                out.add(to)
            elif r["type"] == "precedes" and to == wid:
                out.add(frm)
        return out

    # ----------------------------------------------------------- scheduling
    def finalize_scheduling(self) -> None:
        """Режим планирования — по плану, а не по умолчаниям OpenProject.

        Сводки и задачи с `auto: true` переводятся в автоматический режим: он требует детей
        или предшественников, которых при создании задачи ещё нет. Остальные возвращаются
        в ручной режим с датами из плана: OpenProject сам переключает задачу в
        автоматический режим, как только у неё появляется предшественник, и веха с жёсткой
        датой уезжает на день после предшественника.
        """
        for n in self.plan.ordered():
            wid = self.ids.get(n.key)
            if n.adopt_id or not wid or wid < 0:
                continue
            current = self.c.get(f"/work_packages/{wid}")
            if _wants_auto(n):
                if current.get("scheduleManually") is False:
                    continue
                patch = {"scheduleManually": False}
            else:
                body = self.payloads[n.key]
                patch = {k: body[k] for k in ("startDate", "dueDate", "date") if k in body}
                if not patch:
                    continue                   # без дат в плане — режим не важен
                patch = {k: v for k, v in patch.items() if current.get(k) != v}
                if current.get("scheduleManually") is False:
                    patch["scheduleManually"] = True
                if not patch:
                    continue
            if self.dry and wid in self._dated:
                continue                       # в пробном прогоне эти даты уже показаны
            self.report.updated.append(f"#{wid} {n.label}: "
                                       + ("автопланирование" if _wants_auto(n)
                                          else "ручное планирование, даты из плана"))
            if not self.dry:
                patch["lockVersion"] = current["lockVersion"]
                self.c.patch(f"/work_packages/{wid}", patch)

    def save_state(self) -> None:
        self.state_path.parent.mkdir(exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))


def server_calendar(client: Client, plan: Plan) -> Calendar:
    """Календарь экземпляра на период плана + нерабочие дни плана."""
    dates = [d for n in plan.ordered() for k in ("start", "due", "date")
             if (d := n.fields.get(k))] + [d for s in plan.sprints for d in (s.start, s.finish)]
    first = min(dates, default=dt.date.today())
    last = max(dates, default=first) + dt.timedelta(days=366)
    return instance_calendar(client, first, last, plan.calendar.hours_per_day,
                             set(plan.calendar.non_working))


def plan_schedule(client: Client, cat: Catalog, plan: Plan) -> Schedule:
    """Планировщик с календарём экземпляра; окно спринта — из плана, иначе с сервера."""
    def window(n: Node) -> Window | None:
        name = n.fields.get("sprint")
        if not name:
            return None
        if spec := plan.sprint(name):
            return Window(name, spec.start, spec.finish)
        project = n.fields.get("project")
        if not project or project not in cat.projects:
            return None
        sp = cat.sprints(cat.project(project)["id"]).get(name)
        if not sp or not sp.get("startDate") or not sp.get("finishDate"):
            return None
        return Window(name, dt.date.fromisoformat(sp["startDate"]),
                      dt.date.fromisoformat(sp["finishDate"]))

    return schedule(plan, server_calendar(client, plan), window, closed_tasks(client, plan))


def closed_tasks(client: Client, plan: Plan) -> dict[str, tuple[dt.date | None, dt.date | None]]:
    """key → даты задач плана, закрытых на сервере (по файлу состояния)."""
    path = state_path(plan.path, plan.name)
    ids = json.loads(path.read_text()) if path.exists() else {}
    ids |= {n.key: n.adopt_id for n in plan.ordered() if n.adopt_id}
    if not ids:
        return {}
    by_id = {wid: key for key, wid in ids.items()}
    flt = [{"id": {"operator": "=", "values": [str(i) for i in by_id]}},
           {"status": {"operator": "c", "values": []}}]
    out = {}
    for wp in client.collection("/work_packages", filters=flt):
        day = wp.get("date")
        start, due = wp.get("startDate") or day, wp.get("dueDate") or day
        out[by_id[wp["id"]]] = tuple(dt.date.fromisoformat(d) if d else None
                                     for d in (start, due))
    return out


def _sprint_todo(spec) -> str:
    return (f"создать спринт «{spec.name}» {spec.start:%d.%m.%Y}–{spec.finish:%d.%m.%Y} "
            "(Бэклоги → + Спринт)")


def _diff(current: dict, body: dict, skip_dates: bool) -> dict:
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k == "_links":
            continue
        if skip_dates and k in ("startDate", "dueDate", "date", "duration", "scheduleManually"):
            continue
        cur = current.get(k)
        if k == "estimatedTime":
            if cur is None or abs(iso_to_hours(cur) - iso_to_hours(v)) > 0.01:
                out[k] = v
        elif k == "description":
            if (cur or {}).get("raw", "").strip() != v["raw"]:
                out[k] = v
        elif k == "duration":
            continue                                  # длительность выводится из дат
        elif cur != v:
            out[k] = v
    links: dict[str, Any] = {}
    for k, v in body.get("_links", {}).items():
        cur = current["_links"].get(k)
        if isinstance(v, list):
            if sorted(x["href"] for x in v) != sorted(x.get("href") for x in (cur or [])):
                links[k] = v
        elif (cur or {}).get("href") != v["href"]:
            links[k] = v
    if links:
        out["_links"] = links
    return out


def _names(diff: dict) -> list[str]:
    return [k for k in diff if k != "_links"] + list(diff.get("_links", {}))


def _wants_auto(n: Node) -> bool:
    return n.has_children or bool(n.fields.get("auto"))
