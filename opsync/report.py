"""Отчёт о состоянии проекта для всей команды: что сделано, что в работе, что мешает, что дальше.

Лента задачи показывает одну задачу; отчёт — картину целиком. Публикуется туда, куда
команда и так смотрит: статус проекта и его пояснение на странице обзора проекта, итог
спринта — новостью. Факты собирает opsync, «Главное» в двух-трёх предложениях пишет агент
или человек (`summary`): что важного произошло и что из этого следует.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .client import API, ApiError
from .tasks import Tasks

HEALTH = ("on_track", "at_risk", "off_track")
LIMIT = 10                                  # строк в разделе: отчёт читают, а не листают


@dataclass
class Report:
    project: str
    identifier: str
    today: dt.date
    days: int
    summary: str | None = None
    sprint: dict | None = None
    done: list[dict] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    active: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    next: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    health: str = "on_track"


def build(tasks: Tasks, days: int = 7, summary: str | None = None, health: str | None = None,
          today: dt.date | None = None) -> Report:
    project = tasks.config.project
    if not project:
        raise LookupError("не указан проект: подключите репозиторий (opsync init)")
    if health and health not in HEALTH:
        raise LookupError(f"статус проекта {health!r}: допустимы {', '.join(HEALTH)}")
    today = today or dt.date.today()
    r = Report(project=tasks.c.get(f"/projects/{project}")["name"], identifier=project,
               today=today, days=days, summary=(summary or "").strip() or None)
    stages = {k: v.lower() for k, v in tasks.config.statuses.items()}
    closed = {s["name"].lower() for s in tasks.statuses().values() if s.get("isClosed")}
    closed.add(stages.get("done", ""))

    pid = tasks.project_id(project)
    recent = [{"status": {"operator": "c", "values": []}},
              {"updatedAt": {"operator": ">t-", "values": [str(days)]}}]
    r.done = [tasks.summary(wp) for wp in tasks.c.collection(
        f"/projects/{pid}/work_packages", filters=recent, sortBy=[["updatedAt", "desc"]])]
    open_rows = tasks.list(project)
    for row in open_rows:
        status = (row["status"] or "").lower()
        if status == stages.get("review"):
            r.review.append(row)
        elif status == stages.get("start"):
            r.active.append(row)
        elif status == stages.get("blocked"):
            last = tasks.last_comment(row["id"])
            r.blocked.append({**row, "reason": last["comment"] if last else None})
    overdue = [x for x in open_rows if x["due"] and x["due"] < today.isoformat()
               and (x["status"] or "").lower() not in closed]
    r.risks = [f"#{x['id']} {x['subject']}: срок {_ddmm(x['due'])} прошёл, «{x['status']}»"
               + (f", {x['assignee']}" if x["assignee"] else "") for x in overdue]
    over = []
    try:
        b = tasks.board(project=project, today=today)
    except LookupError:                               # спринтов нет — отчёт без них
        b = None
    if b:
        r.sprint = b["sprint"]
        busy = {x["id"] for x in r.review + r.active + r.blocked}
        r.next = [x for col in b["columns"] if not col["closed"] for x in col["tasks"]
                  if x["id"] not in busy]
        over = [x for x in b["load"] if x["over"]]
        r.risks += [f"{x['assignee']}: осталось {x['remaining']:g} ч работы при "
                    f"{x['capacity']:g} ч до конца спринта" for x in over]
    # off_track сам не ставим: в русском интерфейсе он подписан «Приостановлен» — для команды
    # это звучит как остановка проекта. «Отстаёт» — решение человека (--health off_track).
    if health:
        r.health = health
    elif overdue or over or r.blocked:
        r.health = "at_risk"
    return r


def markdown(r: Report) -> str:
    """Пояснение к статусу проекта: коротко, разделами, номера задач — ссылками (#123)."""
    out = []
    if r.summary:
        out += [f"**Главное.** {r.summary}", ""]
    line = f"_Обновлено {r.today:%d.%m.%Y}"
    if r.sprint:
        s = r.sprint
        start, finish = (dt.date.fromisoformat(s[k]) for k in ("start", "finish"))
        line += (f" · {s['name']} ({start:%d.%m}–{finish:%d.%m}), осталось рабочих дней: "
                 f"{s['days_left']}")
    out += [line + "_", ""]

    def section(title: str, rows: list[Any], fmt) -> None:
        if not rows:
            return
        out.append(f"**{title}** ({len(rows)})")
        out.extend(f"- {fmt(x)}" for x in rows[:LIMIT])
        if len(rows) > LIMIT:
            out.append(f"- … и ещё {len(rows) - LIMIT}")
        out.append("")

    def who(x: dict) -> str:
        parts = [x["assignee"]] if x.get("assignee") else []
        if x.get("due"):
            parts.append(f"до {_ddmm(x['due'])}")
        return f" — {', '.join(parts)}" if parts else ""

    period = "неделю" if r.days == 7 else f"{r.days} дн."
    section(f"Сделано за {period}", r.done, lambda x: f"#{x['id']} {x['subject']}")
    section("На проверке", r.review, lambda x: f"#{x['id']} {x['subject']}{who(x)}")
    section("В работе", r.active, lambda x: f"#{x['id']} {x['subject']}{who(x)}")
    section("Ждёт", r.blocked, lambda x: f"#{x['id']} {x['subject']}{who(x)}"
            + (f": {_short(x['reason'])}" if x.get("reason") else ""))
    section("Риски", r.risks, str)
    section("Дальше по спринту", r.next, lambda x: f"#{x['id']} {x['subject']}{who(x)}")
    if len(out) <= 2 + (2 if r.summary else 0):
        out.append("За период изменений нет.")
    return "\n".join(out).rstrip() + "\n"


def publish(tasks: Tasks, r: Report, news: bool = False) -> dict[str, Any]:
    """Статус и пояснение — на обзор проекта; с `news` — ещё и новостью."""
    pid = tasks.project_id(r.identifier)
    text = markdown(r)
    try:
        tasks.c.patch(f"/projects/{pid}", {
            "statusExplanation": {"format": "markdown", "raw": text},
            "_links": {"status": {"href": f"{API}/project_statuses/{r.health}"}}})
    except ApiError as e:
        if e.status == 403:
            raise LookupError("нет права менять проект: нужна роль с правом «Изменить "
                              "проект» (Edit project)") from None
        raise
    out: dict[str, Any] = {"project": r.identifier, "health": r.health, "news": None}
    if news:
        title = f"Итоги: {r.sprint['name']}" if r.sprint else f"Состояние на {r.today:%d.%m.%Y}"
        try:
            item = tasks.c.post("/news", {
                "title": title, "summary": _short(r.summary or "", 250),
                "description": {"format": "markdown", "raw": text},
                "_links": {"project": {"href": f"{API}/projects/{pid}"}}})
        except ApiError as e:
            if e.status in (403, 404, 422):
                raise LookupError("новость не опубликована: включите модуль «Новости» в "
                                  "проекте и дайте право «Управлять новостями». Статус "
                                  f"проекта уже обновлён. ({e})") from None
            raise
        out["news"] = item.get("id")
    return out


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ddmm(day: str) -> str:
    return f"{dt.date.fromisoformat(day):%d.%m}"
