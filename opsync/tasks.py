"""Работа с задачами по ходу дела: найти, посмотреть, сменить статус, оставить комментарий.

Ссылка на задачу (`ref`) — `100`, `#100` или key из YAML-плана проекта (`be-reset-token`):
key переводится в id через файл состояния плана, указанного в `.openproject.yaml`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

from .catalog import instance_calendar
from .client import API, ApiError, Client, href_id
from .config import ProjectConfig, state_path
from .plan import hours, iso_hours, iso_to_hours, load
from .planedit import DELETE, edit_items


# Этапы, которые агент видит в начале сессии, — в этом порядке.
CONTEXT_STAGES = ("start", "blocked", "review")

RELATIONS = {
    "follows": "следует за", "precedes": "предшествует", "blocks": "блокирует",
    "blocked": "заблокирована", "relates": "связана с", "duplicates": "дублирует",
    "duplicated": "дублируется", "includes": "включает", "partof": "входит в",
    "requires": "требует", "required": "нужна для",
}


@dataclass
class Carry:
    source: str
    target: str
    moved: list[dict]                    # id, key, subject, status
    plan_keys: list[str]                 # элементы плана, где спринт заменён
    warnings: list[str]


@dataclass
class StatusChange:
    id: int
    subject: str
    old: str
    new: str
    changed: bool
    commented: bool
    spent: str | None = None


class Tasks:
    def __init__(self, client: Client, config: ProjectConfig):
        self.c = client
        self.config = config
        self._statuses: dict[str, dict] | None = None
        self._keys: dict[str, int] | None = None
        self.warnings: list[str] = []          # замечания к записанным комментариям
        self._users: dict[str, str | None] = {}

    # ---------------------------------------------------------------- ссылки
    def keys(self) -> dict[str, int]:
        """key → id из состояния плана проекта; пусто, если плана нет или он не применялся."""
        if self._keys is None:
            self._keys = {}
            if self.config.plan and self.config.plan.is_file():
                plan = load(self.config.plan)
                path = state_path(plan.path, plan.name)
                if path.is_file():
                    self._keys = json.loads(path.read_text(encoding="utf-8"))
        return self._keys

    def key_of(self, wid: int) -> str | None:
        return next((k for k, v in self.keys().items() if v == wid), None)

    def resolve(self, ref: str | int) -> int:
        text = str(ref).strip().lstrip("#")
        if text.isdigit():
            return int(text)
        if text in self.keys():
            return self.keys()[text]
        where = self.config.plan or "план не задан в .openproject.yaml"
        raise LookupError(f"задача {ref!r} не найдена: это не номер задачи и не key плана "
                          f"({where}). Номера задач: opsync list")

    # ---------------------------------------------------------------- чтение
    def get(self, wid: int) -> dict:
        return self.c.get(f"/work_packages/{wid}")

    def summary(self, wp: dict) -> dict[str, Any]:
        links = wp["_links"]

        def title(name: str) -> str | None:
            return (links.get(name) or {}).get("title")

        return {
            "id": wp["id"],
            "key": self.key_of(wp["id"]),
            "subject": wp["subject"],
            "type": title("type"),
            "status": title("status"),
            "project": title("project"),
            "sprint": title("sprint"),
            "assignee": title("assignee"),
            "priority": title("priority"),
            "start": wp.get("startDate") or wp.get("date"),
            "due": wp.get("dueDate") or wp.get("date"),
            "parent": title("parent"),
            "estimate": _hours(wp.get("estimatedTime"), default=None),
            "remaining": _hours(wp.get("remainingTime"), default=None),
            "spent": _hours(wp.get("spentTime"), default=None),
            "url": self.c.web_url(wp["id"]),
        }

    def describe(self, wp: dict) -> str:
        return ((wp.get("description") or {}).get("raw") or "").strip()

    def user_name(self, href: str | None) -> str | None:
        """Имя автора записи: в ленте у ссылки на пользователя нет title."""
        if not href:
            return None
        if href not in self._users:
            try:
                self._users[href] = self.c.get(href.removeprefix(API))["name"]
            except ApiError:
                self._users[href] = None           # удалён или скрыт — не повод падать
        return self._users[href]

    def history(self, wid: int, limit: int | None = 5) -> list[dict]:
        """Последние записи ленты, от старых к новым: кто, когда, комментарий, что изменилось."""
        rows = []
        for a in self.c.collection(f"/work_packages/{wid}/activities"):
            comment = ((a.get("comment") or {}).get("raw") or "").strip()
            changes = [d["raw"] for d in a.get("details") or [] if d.get("raw")]
            if a.get("version") == 1:
                changes = ["задача создана"]       # а не дюжина «поле присвоено значение»
            if comment or changes:
                rows.append({"at": a["createdAt"],
                             "author": self.user_name((a["_links"].get("user") or {}).get("href")),
                             "comment": comment, "changes": changes})
        return rows[-limit:] if limit else rows

    def last_comment(self, wid: int) -> dict | None:
        return next((r for r in reversed(self.history(wid, None)) if r["comment"]), None)

    def relations(self, wid: int) -> list[dict]:
        """Связи с точки зрения этой задачи: «следует за #12», «блокирует #15»."""
        out = []
        for r in self.c.collection(f"/work_packages/{wid}/relations"):
            src, dst = r["_links"]["from"], r["_links"]["to"]
            forward = href_id(src["href"]) == wid
            other = dst if forward else src
            kind = r["type"] if forward else r.get("reverseType") or r["type"]
            out.append({"relation": RELATIONS.get(kind, kind), "id": href_id(other["href"]),
                        "subject": other.get("title")})
        return out

    @staticmethod
    def children(wp: dict) -> list[dict]:
        return [{"id": href_id(c["href"]), "subject": c.get("title")}
                for c in wp["_links"].get("children") or []]

    def context(self, today: dt.date | None = None) -> dict[str, Any]:
        """Что агенту знать в начале сессии: спринт, мои задачи по этапам с последним
        комментарием, что дальше по спринту и риски. Без проекта — пусто."""
        project = self.config.project
        if not project:
            return {}
        today = today or dt.date.today()
        me = self.c.get("/users/me")["name"]
        out: dict[str, Any] = {"project": self.c.get(f"/projects/{project}")["name"],
                               "identifier": project, "me": me, "sprint": None,
                               "active": [], "next": [], "risks": [],
                               "tracker": self.config.tracker}
        mine = self.list(mine=True)
        by_status = {self.config.statuses[s].lower(): s for s in CONTEXT_STAGES
                     if s in self.config.statuses}
        for row in mine:
            stage = by_status.get((row["status"] or "").lower())
            if stage:
                out["active"].append({**row, "stage": stage, "last": self.last_comment(row["id"])})
        out["active"].sort(key=lambda r: (CONTEXT_STAGES.index(r["stage"]), r["id"]))
        late = [r for r in mine if r["due"] and r["due"] < today.isoformat()]
        out["risks"] = [f"#{r['id']} {r['subject']}: срок {_ddmm(r['due'])} прошёл, статус "
                        f"«{r['status']}»" for r in late]
        try:
            b = self.board(today=today)
        except LookupError:                           # спринтов в проекте нет — не беда
            return out
        out["sprint"] = b["sprint"]
        # по задачам доски, а не по имени спринта: у подпроектов бывают свои «Спринт 1»
        in_sprint = {r["id"] for col in b["columns"] for r in col["tasks"]}
        active = {r["id"] for r in out["active"]}
        out["next"] = sorted((r for r in mine if r["id"] in in_sprint and r["id"] not in active),
                             key=lambda r: (r["due"] or "9999", r["id"]))
        seen = {r["id"] for r in late}
        out["risks"] += [f"#{r['id']} {r['subject']}: срок {_ddmm(r['due'])} прошёл, статус "
                         f"«{r['status']}»" for r in b["overdue"] if r["id"] not in seen]
        out["risks"] += [f"{x['assignee']}: осталось {x['remaining']:g} ч при "
                         f"{x['capacity']:g} ч до конца спринта" for x in b["load"] if x["over"]]
        return out

    def list(self, project: str | None = None, sprint: str | None = None,
             status: str | None = None, mine: bool = False, include_closed: bool = False,
             search: str | None = None) -> list[dict]:
        project = project or self.config.project
        filters: list[dict] = []
        if search:
            # тема, описание и комментарии — фильтр сервера
            filters.append({"search": {"operator": "**", "values": [search]}})
        if status:
            filters.append({"status": {"operator": "=", "values": [str(self.status(status)["id"])]}})
        else:
            filters.append({"status": {"operator": "*" if include_closed else "o", "values": []}})
        if mine:
            filters.append({"assignee": {"operator": "=", "values": ["me"]}})
        if sprint and project:
            # спринт — фильтром на сервере и по всем проектам: его открывают подпроектам
            sid = self.sprint(sprint, project)["id"]
            filters.append({"sprint": {"operator": "=", "values": [str(sid)]}})
            path = "/work_packages"
        elif project:
            path = f"/projects/{self._project_id(project)}/work_packages"
        else:
            path = "/work_packages"
        rows = []
        for wp in self.c.collection(path, filters=filters, sortBy=[["id", "asc"]]):
            row = self.summary(wp)
            if sprint and not project and (row["sprint"] or "").lower() != sprint.lower():
                continue                    # без проекта id спринта не узнать — по имени
            rows.append(row)
        return rows

    # ---------------------------------------------------------------- спринт
    def sprints(self, project: str | None = None) -> list[dict]:
        project = project or self.config.project
        if not project:
            raise LookupError("не указан проект: подключите репозиторий (opsync init) "
                              "или передайте --project")
        items = self.c.collection(f"/projects/{self._project_id(project)}/sprints")
        return sorted(items, key=lambda s: (s.get("startDate") or "", s["name"]))

    def sprint(self, name: str | None, project: str | None = None,
               today: dt.date | None = None) -> dict:
        """Спринт по имени; без имени — текущий, иначе ближайший следующий, иначе последний."""
        items = self.sprints(project)
        if not items:
            raise LookupError("в проекте нет спринтов")
        if name:
            for s in items:
                if s["name"].lower() == name.lower():
                    return s
            raise LookupError(f"спринт {name!r} не найден. Есть: "
                              + ", ".join(s["name"] for s in items))
        today = (today or dt.date.today()).isoformat()
        current = [s for s in items if s.get("startDate", "") <= today <= s.get("finishDate", "")]
        upcoming = [s for s in items if s.get("startDate", "") > today]
        return (current or upcoming or items[-1:])[0]

    def board(self, sprint: str | None = None, project: str | None = None,
              today: dt.date | None = None) -> dict[str, Any]:
        """То, что видно на доске спринта: колонки-статусы, часы, просрочки, риски."""
        today = today or dt.date.today()
        project = project or self.config.project
        sp = self.sprint(sprint, project, today)
        # по спринту, а не по проекту: спринт бывает открыт подпроектам
        flt = [{"status": {"operator": "*", "values": []}},
               {"sprint": {"operator": "=", "values": [str(sp["id"])]}}]
        statuses = {s["name"]: s for s in self.statuses().values()}
        hpd = float(self.c.get("/configuration").get("hoursPerDay") or 8)
        start = dt.date.fromisoformat(sp["startDate"])
        finish = dt.date.fromisoformat(sp["finishDate"])
        cal = instance_calendar(self.c, start, finish, hpd)
        days_left = len(cal.working_days(max(today, start), finish))

        rows: list[dict] = []
        for wp in self.c.collection("/work_packages", filters=flt, sortBy=[["id", "asc"]]):
            row = self.summary(wp)
            status = statuses.get(row["status"] or "", {})
            row["closed"] = bool(status.get("isClosed"))
            row["estimate"] = _hours(wp.get("estimatedTime"))
            row["remaining"] = 0.0 if row["closed"] else _hours(
                wp.get("remainingTime"), default=row["estimate"])
            row["spent"] = _hours(wp.get("spentTime"))
            row["overdue"] = (not row["closed"] and bool(row["due"])
                              and row["due"] < today.isoformat())
            rows.append(row)

        order = {name: s.get("position", 0) for name, s in statuses.items()}
        columns: dict[str, list[dict]] = {}
        for row in sorted(rows, key=lambda r: (order.get(r["status"], 99), r["id"])):
            columns.setdefault(row["status"], []).append(row)
        load: dict[str, float] = {}
        for row in rows:
            if not row["closed"]:
                who = row["assignee"] or "не назначено"
                load[who] = load.get(who, 0) + row["remaining"]
        capacity = days_left * hpd
        return {
            "sprint": {"id": sp["id"], "name": sp["name"], "start": sp["startDate"],
                       "finish": sp["finishDate"], "days_left": days_left},
            "columns": [{"status": s, "closed": statuses.get(s, {}).get("isClosed", False),
                         "tasks": tasks} for s, tasks in columns.items()],
            "totals": {"tasks": len(rows), "closed": sum(r["closed"] for r in rows),
                       "estimate": sum(r["estimate"] for r in rows),
                       "remaining": sum(r["remaining"] for r in rows),
                       "spent": sum(r["spent"] for r in rows)},
            "overdue": [r for r in rows if r["overdue"]],
            "load": [{"assignee": who, "remaining": h, "capacity": capacity,
                      "over": h > capacity + 0.01} for who, h in sorted(load.items())],
        }

    def carry(self, source: str, target: str | None = None, project: str | None = None,
              comment: str | None = None, notify: bool = True, dry_run: bool = False) -> Carry:
        """Незакрытые задачи спринта → в следующий: на сервере и в плане проекта.

        В плане спринт задачи заменяется явным `sprint:` — иначе следующий apply вернул бы её
        назад. Закреплённые даты (start и due) при включённом планировщике снимаются: apply
        разложит задачу в новом спринте. Без планировщика даты остаются — предупреждение.
        """
        src = self.sprint(source, project)
        if target:
            dst = self.sprint(target, project)
        else:
            later = [s for s in self.sprints(project)
                     if (s.get("startDate") or "") > (src.get("startDate") or "")]
            if not later:
                raise LookupError(f"после «{src['name']}» спринтов нет — укажите --to")
            dst = later[0]
        if dst["id"] == src["id"]:
            raise LookupError("спринт назначения совпадает с исходным")
        flt = [{"status": {"operator": "o", "values": []}},
               {"sprint": {"operator": "=", "values": [str(src["id"])]}}]
        moved = [self.summary(wp) for wp in
                 self.c.collection("/work_packages", filters=flt, sortBy=[["id", "asc"]])]
        text = f"Перенесено из «{src['name']}» в «{dst['name']}»." + (
            f"\n\n{comment}" if comment else "")
        href = {"href": f"/api/v3/sprints/{dst['id']}"}
        if not dry_run:
            for row in moved:
                self.comment(row["id"], text, notify)       # до PATCH — одна запись ленты
                wp = self.get(row["id"])
                self.c.patch(f"/work_packages/{row['id']}", {
                    "lockVersion": wp["lockVersion"], "_links": {"sprint": href}})
        plan_keys, warnings = self._carry_plan(moved, dst["name"], dry_run)
        return Carry(src["name"], dst["name"], moved, plan_keys, warnings)

    def _carry_plan(self, moved: list[dict], sprint: str, dry_run: bool):
        keys = [r["key"] or f"#{r['id']}" for r in moved]
        if not self.config.plan or not self.config.plan.is_file() or not moved:
            return [], []
        plan = load(self.config.plan)
        changes: dict[str, dict] = {}
        warnings: list[str] = []
        stale: list[str] = []                       # даты остались от прошлого спринта
        for key in keys:
            node = plan.nodes.get(key)
            if not node:
                continue
            change: dict = {"sprint": sprint}
            if "date" in node.explicit:
                warnings.append(f"{key}: веха с датой {node.fields['date']} — перенесите дату "
                                "в плане сами, планировщик вехи не двигает")
            elif {"start", "due"} <= node.explicit:
                if plan.calendar.schedule:
                    change.update(start=DELETE, due=DELETE)
                else:
                    stale.append(key)
            changes[key] = change
        if stale:
            warnings.append(f"даты остались от прошлого спринта у {len(stale)} задач "
                            f"({', '.join(stale)}): поправьте их в плане или включите "
                            "calendar.schedule — тогда перенос снимет даты, а apply разложит "
                            "задачи заново")
        if changes and not dry_run:
            edit_items(plan.path, changes)
        return sorted(changes), warnings

    def _project_id(self, ref: str) -> int:
        try:
            return self.c.get(f"/projects/{ref}")["id"]
        except ApiError as e:
            if e.status == 404:
                raise LookupError(f"проект не найден: {ref!r}") from None
            raise

    # ---------------------------------------------------------------- запись
    def statuses(self) -> dict[str, dict]:
        if self._statuses is None:
            self._statuses = {s["name"].lower(): s for s in self.c.collection("/statuses")}
        return self._statuses

    def status(self, name: str) -> dict:
        try:
            return self.statuses()[name.lower()]
        except KeyError:
            known = ", ".join(s["name"] for s in self.statuses().values())
            raise LookupError(f"статус {name!r} не найден. Есть: {known}") from None

    def allowed_statuses(self, wp: dict) -> list[str]:
        """Статусы, в которые workflow пускает задачу (зависит от типа и ролей)."""
        form = self.c.post(f"/work_packages/{wp['id']}/form", {"lockVersion": wp["lockVersion"]})
        schema = form["_embedded"]["schema"]["status"]
        return [s["name"] for s in schema["_embedded"]["allowedValues"]]

    def set_status(self, wid: int, status_name: str, comment: str | None = None,
                   notify: bool = True) -> StatusChange:
        """Идемпотентно: задача уже в этом статусе — статус не трогаем, комментарий пишем.

        Комментарий пишется до смены статуса: OpenProject склеивает изменения одного автора
        за несколько минут в одну запись ленты, и смена статуса присоединяется к записи с
        комментарием. В обратном порядке комментарий уезжает к следующей смене. Поэтому
        переход проверяется заранее — чтобы не оставить комментарий без смены статуса.
        """
        target = self.status(status_name)
        wp = self.get(wid)
        old = wp["_links"]["status"]["title"]
        changed = old.lower() != target["name"].lower()
        if changed:
            allowed = self.allowed_statuses(wp)
            if target["name"].lower() not in {s.lower() for s in allowed}:
                raise self._forbidden(wid, old, target["name"], allowed)
        if comment:
            self.comment(wid, comment, notify)
        if changed:
            if comment:
                wp = self.get(wid)            # свежий lockVersion после комментария
            try:
                self.c.patch(f"/work_packages/{wid}", {
                    "lockVersion": wp["lockVersion"],
                    "_links": {"status": {"href": target["_links"]["self"]["href"]}},
                })
            except ApiError as e:
                if e.status != 422:
                    raise
                raise self._forbidden(wid, old, target["name"],
                                      self.allowed_statuses(self.get(wid)), e) from None
        return StatusChange(wid, wp["subject"], old, target["name"], changed, bool(comment))

    @staticmethod
    def _forbidden(wid: int, old: str, new: str, allowed: list[str],
                   error: ApiError | None = None) -> LookupError:
        options = ", ".join(s for s in allowed if s.lower() != old.lower()) or "никуда"
        reason = f" ({error})" if error else ""
        return LookupError(f"#{wid}: переход «{old}» → «{new}» запрещён workflow{reason}. "
                           f"Можно: {options}")

    def stage(self, wid: int, stage: str, comment: str | None = None,
              notify: bool = True) -> StatusChange:
        return self.set_status(wid, self.config.status_for(stage), comment, notify)

    def log_time(self, wid: int, amount: str | float, comment: str | None = None,
                 day: dt.date | None = None, activity: str | None = None) -> dict:
        """Списать время на задачу: `2h`, `0.5d`, `1.5` (часы)."""
        spent = hours(amount)
        if spent <= 0:
            raise LookupError("время должно быть больше нуля")
        links: dict[str, Any] = {"entity": {"href": f"/api/v3/work_packages/{wid}"}}
        if activity:
            links["activity"] = {"href": self._activity(wid, activity)}
        body: dict[str, Any] = {"hours": iso_hours(spent),
                                "spentOn": (day or dt.date.today()).isoformat(),
                                "_links": links}
        if comment:
            body["comment"] = {"raw": comment}
        self.c.post("/time_entries", body)
        return {"id": wid, "hours": spent, "spentOn": body["spentOn"], "activity": activity}

    def _activity(self, wid: int, name: str) -> str:
        """Вид работ по имени — из формы: список зависит от проекта."""
        form = self.c.post("/time_entries/form", {
            "_links": {"entity": {"href": f"/api/v3/work_packages/{wid}"}}})
        allowed = form["_embedded"]["schema"]["activity"]["_embedded"]["allowedValues"]
        for a in allowed:
            if a["name"].lower() == name.lower():
                return a["_links"]["self"]["href"]
        raise LookupError(f"вид работ {name!r} не найден. Есть: "
                          + ", ".join(a["name"] for a in allowed))

    def comment(self, wid: int, text: str, notify: bool = True) -> None:
        """Похожее на секрет не пишем вовсе; замечания по стилю копятся в `self.warnings`."""
        secret = SECRET.search(text)
        if secret:
            raise LookupError(f"#{wid}: в комментарии похоже на секрет ({secret.group(0)[:12]}…) "
                              "— уберите его, комментарий не записан")
        self.warnings += [f"#{wid}: {w}" for w in comment_warnings(text)]
        self.c.post(f"/work_packages/{wid}/activities", {"comment": {"raw": text}},
                    params=None if notify else {"notify": "false"})


# Комментарий читают люди без контекста сессии. Проверка грубая, на явные признаки отчёта
# «для машины»; запись не останавливает — следующий комментарий агент напишет проще.
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY|"
    r"(?i:\b(?:token|password|passwd|secret|api[_-]?key|пароль|токен)\b\s*[:=]\s*\S{6,})|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9_\-]{20,}|"
    r"xox[bpas]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})")
FILE = re.compile(r"[\w.\-]*[/\\][\w./\\\-]*\.\w{1,5}\b|\b[\w\-]+\.(?:py|ts|tsx|js|jsx|cs|go|"
                  r"java|kt|rb|php|vue|sql|rs|swift|scss|css)\b")
TOOL = re.compile(r"\b(?:Claude|ChatGPT|GPT-?\d|Codex|Copilot|Cursor|LLM|AI)\b|\bИИ\b|"
                  r"нейросет", re.I)
COMMENT_LIMIT = 800


def comment_warnings(text: str) -> list[str]:
    """Чем комментарий непохож на сообщение коллеге; пусто — всё хорошо."""
    out = []
    if len(text) > COMMENT_LIMIT:
        out.append(f"длинный комментарий ({len(text)} знаков): хватает 1–4 предложений — что "
                   "сделано, что осталось, как проверить")
    files = set(FILE.findall(text))
    if len(files) > 3:
        out.append(f"перечислено {len(files)} файлов: напишите, что изменилось по смыслу, "
                   "файлы видны в коммите")
    if TOOL.search(text):
        out.append("упомянут инструмент, которым сделана работа: пишите от себя, по существу")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if sum(ln.lstrip().startswith(("#", "- ", "* ", "|")) for ln in lines) > 6:
        out.append("много заголовков и пунктов: обычным текстом в пару предложений понятнее")
    return out


def _ddmm(day: str) -> str:
    return f"{dt.date.fromisoformat(day):%d.%m}"


def _hours(value: str | None, default: float | None = 0.0) -> float | None:
    if not value:
        return default
    h = iso_to_hours(value)
    return default if h < 0 else h
