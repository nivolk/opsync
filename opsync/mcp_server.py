"""MCP-сервер для агентов без терминала (Claude Desktop и т.п.): тонкая обёртка над `Tasks`.

Правила процесса — те же, что в скилле: разделы SKILL.md о жизненном цикле, комментариях и
подтверждениях отдаются клиенту как instructions сервера. У клиента нет рабочего каталога,
поэтому проект задаётся параметром: `project_dir` (каталог с .openproject.yaml) или
`project` (identifier). Значение по умолчанию — переменная окружения OPSYNC_PROJECT_DIR.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import re
from pathlib import Path
from typing import Any

import requests
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .apply import Applier, plan_schedule
from .catalog import Catalog
from .client import ApiError, Client
from .config import ProjectConfig, find_project_config
from .plan import PlanError, hours, load
from .pull import pull
from .setup import skill_source
from .tasks import Tasks

SECTIONS = ("Жизненный цикл задачи", "Комментарии", "Что требует подтверждения пользователя")
STAGES = ("start", "review", "done", "reopen")


def instructions() -> str:
    """Правила процесса из SKILL.md: команды `opsync X` здесь — инструменты `op_X`."""
    text = (skill_source() / "SKILL.md").read_text(encoding="utf-8")
    parts = []
    for title in SECTIONS:
        m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        if m:
            parts.append(f"## {title}\n{m.group(1).strip()}")
    head = ("Инструменты op_* ведут задачи OpenProject. В правилах ниже команда "
            "`opsync start|review|done|reopen` — это op_stage, `opsync X` — op_X. Проект: "
            "параметр project_dir (каталог репозитория с .openproject.yaml) или project "
            "(identifier); не знаешь — op_projects. В начале работы — op_context: что в "
            "работе, что дальше, какие риски.\n\n")
    return head + "\n\n".join(parts)


def _config(project_dir: str | None, project: str | None) -> ProjectConfig:
    folder = project_dir or os.environ.get("OPSYNC_PROJECT_DIR")
    config = find_project_config(Path(folder).expanduser()) if folder else ProjectConfig()
    if project:
        config.project = project
    return config


def _tasks(project_dir: str | None = None, project: str | None = None) -> Tasks:
    return Tasks(Client.from_env(), _config(project_dir, project))


def _plan_path(project_dir: str | None) -> Path:
    config = _config(project_dir, None)
    if not config.plan:
        raise LookupError("у проекта нет плана: укажите project_dir репозитория, где в "
                          ".openproject.yaml задан plan")
    return config.plan


def friendly(fn):
    """Ожидаемые ошибки — клиенту текстом, как в CLI: агент должен понять, что делать."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            hint = {401: "токен не подошёл — пользователю: opsync setup",
                    403: "недостаточно прав у учётной записи в OpenProject"}.get(e.status)
            raise ToolError(hint or str(e)) from None
        except requests.RequestException as e:
            raise ToolError(f"OpenProject недоступен ({type(e).__name__})") from None
        except (LookupError, PlanError, SystemExit) as e:
            raise ToolError(str(e)) from None
    return wrapper


def build() -> MCPServer:
    mcp = MCPServer("openproject", instructions=instructions())

    def tool(fn):
        return mcp.tool()(friendly(fn))

    @tool
    def op_doctor() -> dict:
        """Проверить доступ: адрес OpenProject и пользователь токена."""
        client = Client.from_env()
        return {"url": client.base, "user": client.get("/users/me")["name"]}

    @tool
    def op_projects() -> list[dict]:
        """Проекты OpenProject, доступные пользователю (identifier — для параметра project)."""
        return [{"identifier": p["identifier"], "name": p["name"]}
                for p in Client.from_env().collection("/projects")]

    @tool
    def op_board(sprint: str | None = None, project: str | None = None,
                 project_dir: str | None = None) -> dict:
        """Сводка спринта (по умолчанию текущего): колонки-статусы, часы, просрочки, перегрузка."""
        return _tasks(project_dir, project).board(sprint, project)

    @tool
    def op_list(project: str | None = None, project_dir: str | None = None,
                sprint: str | None = None, status: str | None = None, mine: bool = False,
                include_closed: bool = False) -> list[dict]:
        """Задачи проекта; по умолчанию открытые."""
        return _tasks(project_dir, project).list(project, sprint, status, mine, include_closed)

    @tool
    def op_context(project_dir: str | None = None, project: str | None = None) -> dict:
        """С чего начать сессию: текущий спринт, мои задачи в работе / на проверке / в
        ожидании с последним комментарием, что дальше по спринту, риски."""
        return _tasks(project_dir, project).context()

    @tool
    def op_show(ref: str, history: int = 5, project_dir: str | None = None) -> dict:
        """Задача целиком: ref — номер (123, #123) или key плана. history — сколько последних
        записей ленты (комментарии и изменения) вернуть, 0 — все."""
        tasks = _tasks(project_dir)
        wid = tasks.resolve(ref)
        wp = tasks.get(wid)
        return {**tasks.summary(wp), "description": tasks.describe(wp),
                "children": tasks.children(wp), "relations": tasks.relations(wid),
                "history": tasks.history(wid, history or None)}

    @tool
    def op_search(text: str, include_closed: bool = False, project: str | None = None,
                  project_dir: str | None = None) -> list[dict]:
        """Поиск задач проекта по теме, описанию и комментариям — найти, что уже делали
        или обсуждали."""
        return _tasks(project_dir, project).list(project, include_closed=include_closed,
                                                 search=text)

    @tool
    def op_stage(ref: str, stage: str, comment: str | None = None, spent: str | None = None,
                 notify: bool = True, project_dir: str | None = None) -> dict:
        """Этап задачи: start | review | done | reopen. comment — в ленту задачи одной записью
        со сменой статуса; spent — заодно списать время (2h, 30m). Повтор безопасен."""
        if stage not in STAGES:
            raise LookupError(f"этап {stage!r}: допустимы {', '.join(STAGES)}")
        tasks = _tasks(project_dir)
        wid = tasks.resolve(ref)
        if spent:
            hours(spent)                               # формат — до любой записи
        change = tasks.stage(wid, stage, comment, notify)
        if spent:
            tasks.log_time(wid, spent)
            change.spent = spent
        return {**change.__dict__, "warnings": tasks.warnings}

    @tool
    def op_status(ref: str, status: str, comment: str | None = None,
                  project_dir: str | None = None) -> dict:
        """Произвольный статус задачи (имя статуса OpenProject)."""
        tasks = _tasks(project_dir)
        change = tasks.set_status(tasks.resolve(ref), status, comment)
        return {**change.__dict__, "warnings": tasks.warnings}

    @tool
    def op_comment(ref: str, text: str, notify: bool = True,
                   project_dir: str | None = None) -> str:
        """Комментарий в ленту задачи (markdown)."""
        tasks = _tasks(project_dir)
        wid = tasks.resolve(ref)
        tasks.comment(wid, text, notify)
        return "\n".join([f"#{wid}: комментарий добавлен", *tasks.warnings])

    @tool
    def op_new(subject: str, description: str, type: str | None = None,
               sprint: str | None = None, parent: str | None = None,
               estimate: str | None = None, key: str | None = None, mine: bool = False,
               dry_run: bool = True, project: str | None = None,
               project_dir: str | None = None) -> dict:
        """Завести задачу, найденную по ходу работы, и дописать её в план проекта (если он
        есть). description — зачем задача и когда готова, для людей. По умолчанию пробный
        прогон; dry_run=false — только после согласия пользователя."""
        t = _tasks(project_dir, project).new(subject, description, type, sprint, parent,
                                             estimate, key, mine, project, dry_run)
        return t.__dict__

    @tool
    def op_describe(ref: str, text: str, replace: bool = False,
                    project_dir: str | None = None) -> str:
        """Дополнить описание задачи (replace=true — заменить целиком)."""
        tasks = _tasks(project_dir)
        wid = tasks.resolve(ref)
        tasks.update_description(wid, text, replace)
        return f"#{wid}: описание {'заменено' if replace else 'дополнено'}"

    @tool
    def op_log_time(ref: str, time: str, comment: str | None = None, date: str | None = None,
                    activity: str | None = None, project_dir: str | None = None) -> dict:
        """Списать время: 2h, 0.5d, 30m; date — YYYY-MM-DD (по умолчанию сегодня)."""
        tasks = _tasks(project_dir)
        day = dt.date.fromisoformat(date) if date else None
        return tasks.log_time(tasks.resolve(ref), time, comment, day, activity)

    @tool
    def op_carry(sprint: str, to: str | None = None, comment: str | None = None,
                 dry_run: bool = True, project: str | None = None,
                 project_dir: str | None = None) -> dict:
        """Незакрытые задачи спринта — в следующий (или to), на сервере и в плане.
        По умолчанию пробный прогон; dry_run=false — только после согласия пользователя."""
        c = _tasks(project_dir, project).carry(sprint, to, project, comment, dry_run=dry_run)
        return c.__dict__

    @tool
    def op_schedule(project_dir: str | None = None) -> dict:
        """Расчёт дат плана: даты задач, загрузка, предупреждения. Ничего не пишет."""
        client = Client.from_env()
        result = plan_schedule(client, Catalog.load(client), load(_plan_path(project_dir)))
        return {"slots": [_jsonable(s.__dict__) for s in result.slots.values()],
                "warnings": result.warnings}

    @tool
    def op_apply(project_dir: str | None = None, dry_run: bool = True,
                 allow_missing: bool = False) -> dict:
        """Привести OpenProject к плану. По умолчанию пробный прогон; dry_run=false — только
        после согласия пользователя."""
        report = Applier(Client.from_env(), load(_plan_path(project_dir)), dry_run,
                         allow_missing).run()
        return {"problems": {k: sorted(v) for k, v in report.problems.items()},
                "created": report.created, "updated": report.updated,
                "relations": report.relations, "warnings": report.warnings,
                "unchanged": report.unchanged, "dry_run": dry_run}

    @tool
    def op_pull(project_dir: str | None = None, dry_run: bool = True) -> dict:
        """Статусы и даты из OpenProject — в план (на сервер не пишет)."""
        result = pull(Client.from_env(), load(_plan_path(project_dir)), dry_run)
        return {"changes": _jsonable(result.changes),
                "untracked": [{"id": w["id"], "subject": w["subject"]} for w in result.untracked],
                "dry_run": dry_run}

    return mcp


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def main() -> None:
    build().run()
