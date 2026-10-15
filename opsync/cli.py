"""opsync — задачи OpenProject из командной строки и YAML-плана.

Ведение задач (из любого каталога; проект и этапы — из .openproject.yaml):
    opsync context [--hook] [--json]          с чего начать сессию: спринт, мои задачи, риски
    opsync show <ref> [--history N] [--json]  задача целиком, связи, последние записи ленты
    opsync search <текст> [--all] [--json]    поиск по теме, описанию и комментариям
    opsync list [--sprint S] [--status S] [--mine] [--all] [--project P] [--json]
    opsync board [--sprint S] [--project P] [--json]   сводка спринта: колонки, часы, риски
    opsync carry <спринт> [--to S] [--dry-run] [-m текст]  незакрытые задачи → в следующий
    opsync start|review|done|reopen <ref>... [-m текст]   этап жизненного цикла
    opsync block <ref>... -m <что мешает>     ждёт чего-то извне (этап blocked); unblock — снова в работу
    opsync note <ref> -m <текст>              ход работы: комментарий без смены статуса и писем
    opsync status <ref> <статус> [-m текст]   произвольный статус
    opsync comment <ref> <текст>              комментарий в ленту задачи
    opsync report [--summary текст] [--publish] [--news]   состояние проекта для команды
    opsync new <тема> -m <описание> [--type T] [--sprint S] [--parent ref] [--estimate 4h]
               [--key k] [--mine] [--dry-run]   завести задачу (и дописать в план)
    opsync describe <ref> -m <текст> [--replace]   дополнить описание задачи
    opsync log <ref> <время> [-m текст] [--date D] [--activity A]   списать время (2h, 0.5d)
    (--no-notify у этих команд — комментарий без уведомлений)

План проекта:
    opsync apply [план.yaml] [--dry-run] [--allow-missing]
    opsync schedule [план.yaml] [--json]      даты по оценкам, связям и спринтам
    opsync pull [план.yaml] [--dry-run] [--adopt]   статусы и даты с сервера → в план
    opsync export [проект] [-o f.yaml]

Справка и настройка:
    opsync setup                              доступ и подключение агентов
    opsync hooks [on|off]                     хуки Claude Code: сводка при старте, подсказка после коммита
    opsync init [--project ID] [--plan путь] [--tracker путь] [--no-rules]
    opsync rules [--remove]                   правило ведения задач в AGENTS.md / CLAUDE.md
    opsync doctor                             откуда взяты настройки, доступ, статусы
    opsync inspect [проект]

ref — id (`100`, `#100`) или key из плана проекта.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import requests
import yaml

from .apply import Applier, plan_schedule
from .pull import pull
from .catalog import Catalog
from .client import ApiError, Client, href_id
from . import __version__, gitinfo, rules, setup
from .config import (CONFIG_NAMES, DEFAULT_STATUSES, OPTIONAL_STAGES, ProjectConfig,
                     detect_statuses, find_project_config, load_credentials)
from .plan import PlanError, hours, load
from .tasks import StatusChange, Tasks

STAGES = ("start", "review", "done", "reopen")
# План живёт в репозитории проекта; рядом с ним — .state/ с key → id (коммитится вместе с ним).
DEFAULT_PLAN = ".openproject/plan.yaml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="opsync", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"opsync {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="задача целиком: описание, связи, последние записи ленты")
    p.add_argument("ref")
    p.add_argument("--history", type=int, default=5, metavar="N",
                   help="сколько последних записей ленты показать (0 — все)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("search", help="поиск задач по теме, описанию и комментариям")
    p.add_argument("text")
    p.add_argument("--project")
    p.add_argument("--all", action="store_true", help="вместе с закрытыми")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("context", help="с чего начать сессию: спринт, мои задачи, риски")
    p.add_argument("--hook", action="store_true",
                   help="для хука старта сессии: коротко, без ошибок и без ожидания сети")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="задачи проекта (по умолчанию открытые)")
    p.add_argument("--project")
    p.add_argument("--sprint")
    p.add_argument("--status")
    p.add_argument("--mine", action="store_true", help="только назначенные на меня")
    p.add_argument("--all", action="store_true", help="вместе с закрытыми")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("board", help="сводка спринта (по умолчанию — текущего)")
    p.add_argument("--sprint")
    p.add_argument("--project")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("carry", help="перенести незакрытые задачи спринта в следующий")
    p.add_argument("sprint", help="исходный спринт")
    p.add_argument("--to", help="спринт назначения (по умолчанию — следующий по датам)")
    p.add_argument("--project")
    p.add_argument("-m", "--message", help="дополнение к комментарию о переносе")
    add_notify(p)
    p.add_argument("--dry-run", action="store_true", help="ничего не записывать")

    for stage in STAGES:
        p = sub.add_parser(stage, help=f"перевести в статус этапа «{stage}»")
        p.add_argument("refs", nargs="+")
        p.add_argument("-m", "--message", help="комментарий в ленту задачи")
        p.add_argument("--spent", help="заодно списать время на каждую задачу: 2h, 0.5d")
        add_notify(p)
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("block", help="задача ждёт чего-то извне: статус этапа blocked")
    p.add_argument("refs", nargs="+")
    p.add_argument("-m", "--message", required=True, help="что мешает и от кого ждём")
    add_notify(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("unblock", help="блокер снят — снова в работе (этап start)")
    p.add_argument("refs", nargs="+")
    p.add_argument("-m", "--message", help="что изменилось")
    add_notify(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("note", help="ход работы: комментарий без смены статуса и без писем")
    p.add_argument("ref")
    p.add_argument("-m", "--message", required=True, help="что сделано, что дальше")
    p.add_argument("--notify", action="store_true", help="разослать уведомления участникам")

    p = sub.add_parser("status", help="перевести в произвольный статус")
    p.add_argument("ref")
    p.add_argument("status")
    p.add_argument("-m", "--message")
    add_notify(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("log", help="списать время на задачу")
    p.add_argument("ref")
    p.add_argument("time", help="2h, 0.5d или часы числом")
    p.add_argument("-m", "--message", help="комментарий к записи времени")
    p.add_argument("--date", type=dt.date.fromisoformat, help="день, по умолчанию сегодня")
    p.add_argument("--activity", help="вид работ (по умолчанию — заданный в OpenProject)")

    p = sub.add_parser("comment", help="комментарий в ленту задачи")
    p.add_argument("ref")
    p.add_argument("text")
    add_notify(p)

    p = sub.add_parser("new", help="завести задачу, найденную по ходу работы")
    p.add_argument("subject", help="тема: что сделать, понятно без контекста")
    p.add_argument("-m", "--message", required=True,
                   help="описание: зачем задача и когда она готова")
    p.add_argument("--type", help="тип (по умолчанию — из плана или первый тип проекта)")
    p.add_argument("--sprint")
    p.add_argument("--parent", help="родительская задача: номер или key плана")
    p.add_argument("--estimate", help="оценка: 4h, 0.5d")
    p.add_argument("--key", help="key в плане проекта (без него в план пишется - id: N)")
    p.add_argument("--mine", action="store_true", help="назначить на себя")
    p.add_argument("--project")
    p.add_argument("--dry-run", action="store_true", help="проверить на сервере, не создавая")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("describe", help="дополнить описание задачи")
    p.add_argument("ref")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--replace", action="store_true", help="заменить описание целиком")

    p = sub.add_parser("report", help="состояние проекта для команды: сделано, в работе, "
                                      "ждёт, риски, дальше")
    p.add_argument("--days", type=int, default=7, help="«сделано» за сколько дней (7)")
    p.add_argument("--summary", help="«Главное» — 2–3 предложения своими словами")
    p.add_argument("--health", choices=("on_track", "at_risk", "off_track"),
                   help="статус проекта (по умолчанию — по рискам)")
    p.add_argument("--publish", action="store_true",
                   help="записать статус и пояснение на обзор проекта")
    p.add_argument("--news", action="store_true", help="и опубликовать новостью (итог спринта)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("setup", help="первый запуск: доступ к OpenProject и подключение агентов")
    p.add_argument("--url", help="адрес OpenProject")
    p.add_argument("--token-stdin", action="store_true", help="токен — из stdin")
    p.add_argument("--yes", action="store_true", help="не спрашивать, подключить всё найденное")
    p.add_argument("--no-new-hooks", dest="new_hooks", action="store_false",
                   help="хуки Claude Code только обновить, если уже стоят (так делает upgrade)")

    p = sub.add_parser("hooks", help="хуки Claude Code: сводка задач при старте, подсказка "
                                     "после коммита")
    p.add_argument("action", nargs="?", choices=("on", "off", "status"), default="status")

    sub.add_parser("mcp", help="MCP-сервер (stdio) для агентов без терминала")
    sub.add_parser("upgrade", help="обновить opsync и скиллы у агентов")

    p = sub.add_parser("init", help="подключить текущий репозиторий к проекту OpenProject")
    p.add_argument("--project", help="identifier проекта (без него — выбор из списка)")
    p.add_argument("--plan", help=f"YAML-план проекта (если есть {DEFAULT_PLAN} — он)")
    p.add_argument("--tracker", help="локальный трекер задач, например docs/TASKS.md")
    p.add_argument("--force", action="store_true", help="перезаписать существующий")
    p.add_argument("--no-rules", dest="rules", action="store_false",
                   help="не дописывать правило ведения задач в AGENTS.md / CLAUDE.md")

    p = sub.add_parser("rules", help="правило ведения задач в AGENTS.md / CLAUDE.md репозитория")
    p.add_argument("--remove", action="store_true", help="убрать блок")

    sub.add_parser("doctor", help="проверить настройки и доступ")

    p = sub.add_parser("inspect", help="справочники экземпляра или проекта")
    p.add_argument("project", nargs="?")

    p = sub.add_parser("export", help="задачи проекта в YAML")
    p.add_argument("project", nargs="?")
    p.add_argument("-o", "--output")

    p = sub.add_parser("apply", help="привести OpenProject к плану")
    p.add_argument("plan", type=Path, nargs="?")
    p.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    p.add_argument("--allow-missing", action="store_true",
                   help="пропускать ненастроенные категории/спринты вместо остановки")

    p = sub.add_parser("pull", help="перенести в план статусы и даты с сервера")
    p.add_argument("plan", type=Path, nargs="?")
    p.add_argument("--dry-run", action="store_true", help="только показать")
    p.add_argument("--adopt", action="store_true",
                   help="дописать в план задачи, заведённые руками (как `- id: N`)")

    p = sub.add_parser("schedule", help="рассчитать даты задач плана (ничего не пишет)")
    p.add_argument("plan", type=Path, nargs="?")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    tasks: Tasks | None = None
    try:
        if args.cmd == "mcp":
            from .mcp_server import main as serve       # тянет mcp — только по запросу
            serve()
            return 0
        if args.cmd == "upgrade":
            return cmd_upgrade()
        if args.cmd == "setup":
            token = sys.stdin.readline().strip() if args.token_stdin else None
            return setup.run(args.url, token, args.yes, new_hooks=args.new_hooks)
        if args.cmd == "hooks":
            return cmd_hooks(args.action)
        if args.cmd == "init":
            return cmd_init(args, Client.from_env())
        if args.cmd == "rules":
            return cmd_rules(find_project_config(), args.remove)
        if args.cmd == "context" and args.hook:
            return cmd_context_hook()
        config = find_project_config()
        client = Client.from_env()
        if args.cmd == "doctor":
            return cmd_doctor(client, config)
        tasks = Tasks(client, config)
        if args.cmd == "show":
            return cmd_show(tasks, args.ref, args.json, args.history)
        if args.cmd == "search":
            return print_rows(tasks.list(project=args.project, include_closed=args.all,
                                         search=args.text), args.json)
        if args.cmd == "context":
            return print_context(tasks.context(), args.json)
        if args.cmd == "list":
            return cmd_list(tasks, args)
        if args.cmd == "board":
            return cmd_board(tasks, args)
        if args.cmd == "carry":
            return cmd_carry(tasks, args)
        if args.cmd in STAGES:
            if args.spent:
                hours(args.spent)                   # ошибка формата — до любой записи
            changes = []
            for ref in args.refs:
                wid = tasks.resolve(ref)
                message = args.message
                if args.cmd == "review":
                    message = gitinfo.with_commit(message, wid)
                changes.append(tasks.stage(wid, args.cmd, message, args.notify))
                if args.spent:
                    tasks.log_time(wid, args.spent)
                    changes[-1].spent = args.spent
            return print_changes(changes, args.json)
        if args.cmd in ("block", "unblock"):
            stage = "blocked" if args.cmd == "block" else "start"
            tasks.config.status_for(stage)          # не настроен — до любой записи
            return print_changes([tasks.stage(tasks.resolve(ref), stage, args.message,
                                              args.notify) for ref in args.refs], args.json)
        if args.cmd == "note":
            wid = tasks.resolve(args.ref)
            tasks.comment(wid, args.message, args.notify)
            print(f"#{wid}: заметка о ходе работы добавлена")
            return 0
        if args.cmd == "status":
            change = tasks.set_status(tasks.resolve(args.ref), args.status, args.message,
                                      args.notify)
            return print_changes([change], args.json)
        if args.cmd == "log":
            wid = tasks.resolve(args.ref)
            entry = tasks.log_time(wid, args.time, args.message, args.date, args.activity)
            print(f"#{wid}: списано {entry['hours']:g} ч за {entry['spentOn']}")
            return 0
        if args.cmd == "comment":
            wid = tasks.resolve(args.ref)
            tasks.comment(wid, args.text, args.notify)
            print(f"#{wid}: комментарий добавлен")
            return 0
        if args.cmd == "new":
            return cmd_new(tasks, args)
        if args.cmd == "describe":
            wid = tasks.resolve(args.ref)
            tasks.update_description(wid, args.message, args.replace)
            print(f"#{wid}: описание {'заменено' if args.replace else 'дополнено'}")
            return 0
        if args.cmd == "report":
            return cmd_report(tasks, args)
        if args.cmd == "inspect":
            return cmd_inspect(client, args.project or config.project)
        if args.cmd == "export":
            project = args.project or config.project
            if not project:
                raise LookupError("не указан проект: подключите репозиторий (opsync init) "
                                  "или передайте проект аргументом")
            return cmd_export(client, project, args.output)
        plan = args.plan or config.plan
        if not plan:
            raise LookupError("не указан план: передайте путь к YAML или задайте plan в "
                              ".openproject.yaml")
        if args.cmd == "schedule":
            return cmd_schedule(client, plan, args.json)
        if args.cmd == "pull":
            return cmd_pull(client, plan, args.dry_run, args.adopt)
        return cmd_apply(client, plan, args.dry_run, args.allow_missing)
    except ApiError as e:
        hint = {401: "токен не подошёл — обновите доступ: opsync setup",
                403: "недостаточно прав у вашей учётной записи в OpenProject"}.get(e.status)
        print(f"Ошибка: {hint or e}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"Ошибка: OpenProject недоступен ({type(e).__name__}). Проверьте сеть и адрес: "
              "opsync doctor", file=sys.stderr)
        return 1
    except (PlanError, LookupError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        for w in tasks.warnings if tasks else ():
            print(f"Замечание к комментарию {w}", file=sys.stderr)


def add_notify(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-notify", dest="notify", action="store_false",
                   help="не рассылать уведомления о комментарии")


# ------------------------------------------------------------------ задачи
def cmd_show(tasks: Tasks, ref: str, as_json: bool, history: int = 5) -> int:
    wid = tasks.resolve(ref)
    wp = tasks.get(wid)
    row = tasks.summary(wp)
    row["description"] = tasks.describe(wp)
    row["children"] = tasks.children(wp)
    row["relations"] = tasks.relations(wid)
    row["history"] = tasks.history(wid, history or None)
    if as_json:
        print(json.dumps(row, ensure_ascii=False, indent=1))
        return 0
    print(f"#{row['id']} {row['subject']}")
    for label, name in (("Ключ", "key"), ("Тип", "type"), ("Статус", "status"),
                        ("Проект", "project"), ("Спринт", "sprint"), ("Родитель", "parent"),
                        ("Исполнитель", "assignee"), ("Приоритет", "priority"),
                        ("Начало", "start"), ("Срок", "due"), ("Ссылка", "url")):
        if row.get(name):
            print(f"  {label + ':':13}{row[name]}")
    if row["estimate"] is not None or row["spent"]:
        parts = [f"оценка {row['estimate']:g}" if row["estimate"] is not None else None,
                 f"осталось {row['remaining']:g}" if row["remaining"] is not None else None,
                 f"списано {row['spent'] or 0:g}"]
        print(f"  {'Время:':13}" + ", ".join(p for p in parts if p) + " ч")
    if row["children"]:
        print(f"  {'Подзадачи:':13}" + ", ".join(f"#{c['id']} {c['subject']}"
                                                 for c in row["children"]))
    for r in row["relations"]:
        print(f"  {'Связь:':13}{r['relation']} #{r['id']} {r['subject'] or ''}".rstrip())
    if row["description"]:
        print("\n" + row["description"])
    if row["history"]:
        print("\nЛента:")
        for h in row["history"]:
            print(f"  {_when(h['at'])} · {h['author'] or '—'}")
            for c in h["changes"]:
                print(f"    {c}")
            for line in h["comment"].splitlines():
                print(f"    > {line}")
    return 0


def _when(stamp: str) -> str:
    """ISO-время сервера (UTC) → местное «14.10 15:30»."""
    at = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    return f"{at:%d.%m %H:%M}"


def print_rows(rows: list[dict], as_json: bool) -> int:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("Задач нет.")
        return 0
    width = max(len(r["status"] or "") for r in rows)
    for r in rows:
        key = f" [{r['key']}]" if r["key"] else ""
        sprint = f" · {r['sprint']}" if r["sprint"] else ""
        print(f"#{r['id']:<5} {r['status']:<{width}}  {r['subject']}{key}{sprint}")
    return 0


CONTEXT_TITLES = {"start": "В работе", "blocked": "Ждёт (блокер)", "review": "На проверке"}


def print_context(c: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(c, ensure_ascii=False, indent=1))
        return 0
    if not c:
        print("Репозиторий не подключён к OpenProject (opsync init).")
        return 0
    head = f"OpenProject: {c['project']} ({c['identifier']}), вы — {c['me']}"
    if c["sprint"]:
        s = c["sprint"]
        start, finish = (dt.date.fromisoformat(s[k]) for k in ("start", "finish"))
        head += (f" · {s['name']} {start:%d.%m}–{finish:%d.%m}, "
                 f"осталось рабочих дней: {s['days_left']}")
    print(head)
    stage = None
    for r in c["active"]:
        if r["stage"] != stage:
            stage = r["stage"]
            print(f"\n{CONTEXT_TITLES[stage]}:")
        due = f" · до {dt.date.fromisoformat(r['due']):%d.%m}" if r["due"] else ""
        key = f" [{r['key']}]" if r["key"] else ""
        print(f"  #{r['id']} {r['subject']}{key}{due}")
        if r["last"]:
            text = " ".join(r["last"]["comment"].split())
            text = text if len(text) <= 160 else text[:157] + "…"
            print(f"    последнее ({_when(r['last']['at'])}, {r['last']['author'] or '—'}): {text}")
    if not c["active"]:
        print("\nУ вас нет задач в работе.")
    if c["next"]:
        print("\nДальше по спринту:")
        for r in c["next"][:5]:
            due = f" · до {dt.date.fromisoformat(r['due']):%d.%m}" if r["due"] else ""
            print(f"  #{r['id']} {r['subject']}{due}")
        if len(c["next"]) > 5:
            print(f"  … и ещё {len(c['next']) - 5} (opsync board)")
    if c["risks"]:
        print("\nРиски:")
        for r in c["risks"][:8]:
            print(f"  ! {r}")
        if len(c["risks"]) > 8:
            print(f"  … и ещё {len(c['risks']) - 8}")
    if c["tracker"]:
        print(f"\nЛокальный трекер: {c['tracker']} — номер задачи OpenProject пишите рядом (OP #N).")
    print("\nВедите задачи по ходу работы (скилл openproject-tasks): start при начале, "
          "OP#N в сообщении коммита, короткий комментарий о ходе работы, review после "
          "коммита; новую работу — opsync new с согласия пользователя.")
    return 0


def cmd_context_hook() -> int:
    """Хук старта сессии: что-то пошло не так — молчим, сессия важнее сводки."""
    import contextlib
    import io
    config = find_project_config()
    if not config.project:
        return 0
    buf = io.StringIO()
    try:
        creds = load_credentials()
        tasks = Tasks(Client(creds.url, creds.token, timeout=5), config)
        with contextlib.redirect_stdout(buf):
            print_context(tasks.context(), False)
    except (Exception, SystemExit):                    # noqa: BLE001 — хук не должен мешать
        return 0
    print(buf.getvalue(), end="")
    return 0


def cmd_list(tasks: Tasks, args: argparse.Namespace) -> int:
    rows = tasks.list(project=args.project, sprint=args.sprint, status=args.status,
                      mine=args.mine, include_closed=args.all)
    return print_rows(rows, args.json)


def cmd_board(tasks: Tasks, args: argparse.Namespace) -> int:
    b = tasks.board(args.sprint, args.project)
    if args.json:
        print(json.dumps(b, ensure_ascii=False, indent=1))
        return 0
    s, tot = b["sprint"], b["totals"]
    start, finish = (dt.date.fromisoformat(s[k]) for k in ("start", "finish"))
    print(f"{s['name']} · {start:%d.%m}–{finish:%d.%m} · осталось рабочих дней: {s['days_left']}")
    print(f"Задач {tot['tasks']}, закрыто {tot['closed']} · оценка {tot['estimate']:g} ч, "
          f"осталось {tot['remaining']:g} ч, списано {tot['spent']:g} ч")
    for col in b["columns"]:
        print(f"\n{col['status']} ({len(col['tasks'])})")
        for r in col["tasks"]:
            key = f" [{r['key']}]" if r["key"] else ""
            due = f" · до {dt.date.fromisoformat(r['due']):%d.%m}" if r["due"] else ""
            late = " · ПРОСРОЧЕНА" if r["overdue"] else ""
            left = r["estimate"] and not r["closed"]
            hours = f" · {r['remaining']:g}/{r['estimate']:g} ч" if left else ""
            print(f"  #{r['id']:<5} {r['subject']}{key}{due}{hours}{late}")
    risks = [x for x in b["load"] if x["over"]]
    if b["overdue"] or risks:
        print("\nРиски:")
        for r in b["overdue"]:
            print(f"  ! #{r['id']} {r['subject']}: срок {r['due']} прошёл, статус «{r['status']}»")
        for x in risks:
            print(f"  ! {x['assignee']}: осталось {x['remaining']:g} ч при {x['capacity']:g} ч "
                  "до конца спринта")
    return 0


def cmd_carry(tasks: Tasks, args: argparse.Namespace) -> int:
    c = tasks.carry(args.sprint, args.to, args.project, args.message, args.notify, args.dry_run)
    verb = "Будет перенесено" if args.dry_run else "Перенесено"
    if not c.moved:
        print(f"В «{c.source}» незакрытых задач нет.")
        return 0
    print(f"{verb} из «{c.source}» в «{c.target}»: {len(c.moved)}")
    for r in c.moved:
        key = f" [{r['key']}]" if r["key"] else ""
        print(f"  #{r['id']:<5} {r['status']:<14} {r['subject']}{key}")
    if c.plan_keys:
        what = "будет заменён" if args.dry_run else "заменён"
        print(f"\nВ плане спринт {what} у: {', '.join(c.plan_keys)}. "
              "Закоммитьте план; даты пересчитает opsync apply.")
    for w in c.warnings:
        print(f"  ! {w}")
    if args.dry_run:
        print("\nПробный прогон — ничего не записано.")
    print(f"Сам спринт «{c.source}» закрывается в интерфейсе (Бэклоги), API этого не умеет.")
    return 0


def cmd_report(tasks: Tasks, args: argparse.Namespace) -> int:
    from . import report
    r = report.build(tasks, args.days, args.summary, args.health)
    if args.json:
        print(json.dumps({**r.__dict__, "today": r.today.isoformat(),
                          "markdown": report.markdown(r)}, ensure_ascii=False, indent=1))
        if not args.publish:
            return 0
    else:
        print(f"Статус проекта: {HEALTH_TITLES[r.health]}\n")
        print(report.markdown(r))
    if not args.publish:
        if args.news:
            print("--news публикуется вместе с --publish.")
        print("Пробный прогон — ничего не записано. Опубликовать: opsync report --publish"
              + (" --summary …" if not r.summary else ""))
        return 0
    result = report.publish(tasks, r, args.news)
    if not args.json:
        print(f"Опубликовано на обзоре проекта: {tasks.c.base}/projects/{r.identifier}"
              + (f"; новость #{result['news']}" if result["news"] else ""))
    return 0


HEALTH_TITLES = {"on_track": "по плану", "at_risk": "под угрозой", "off_track": "отстаёт"}


def cmd_new(tasks: Tasks, args: argparse.Namespace) -> int:
    t = tasks.new(args.subject, args.message, args.type, args.sprint, args.parent,
                  args.estimate, args.key, args.mine, args.project, args.dry_run)
    if args.json:
        print(json.dumps(t.__dict__, ensure_ascii=False, indent=1))
        return 0
    if args.dry_run:
        print(f"Будет создано ({t.type}, {t.project}): {t.subject}. Сервер форму принял.")
    else:
        print(f"Создано #{t.id} ({t.type}): {t.subject}\n  {t.url}")
    if t.plan_item:
        verb = "Будет дописано" if args.dry_run else "Дописано"
        item = yaml.safe_dump([t.plan_item], allow_unicode=True, sort_keys=False,
                              width=4096).rstrip()
        print(f"\n{verb} в план {tasks.config.plan}:\n" + "\n".join(
            "  " + line for line in item.splitlines()))
        if not args.dry_run:
            print("Закоммитьте план.")
    if args.dry_run:
        print("\nПробный прогон — ничего не записано.")
    return 0


def print_changes(changes: list[StatusChange], as_json: bool) -> int:
    if as_json:
        print(json.dumps([c.__dict__ for c in changes], ensure_ascii=False, indent=1))
        return 0
    for c in changes:
        what = f"{c.old} → {c.new}" if c.changed else f"уже «{c.new}»"
        note = (", комментарий добавлен" if c.commented else "") + (
            f", списано {c.spent}" if c.spent else "")
        print(f"#{c.id} {c.subject}: {what}{note}")
    return 0


# --------------------------------------------------------------- настройка
def cmd_init(args: argparse.Namespace, client: Client) -> int:
    path = Path.cwd() / CONFIG_NAMES[0]
    if path.exists() and not args.force:
        print(f"{path} уже есть. Перезаписать: opsync init --force", file=sys.stderr)
        return 1
    project = args.project or choose_project(client)
    try:
        client.get(f"/projects/{project}")
    except ApiError as e:
        if e.status != 404:
            raise
        raise LookupError(f"проект «{project}» не найден или нет доступа. "
                          "Выбрать из списка: opsync init") from None
    data: dict = {"project": project}
    if args.plan or (Path.cwd() / DEFAULT_PLAN).is_file():
        data["plan"] = args.plan or DEFAULT_PLAN
    if args.tracker:
        data["tracker"] = args.tracker
    data["statuses"] = choose_statuses(client)
    header = ("# Привязка репозитория к OpenProject для opsync.\n"
              "# statuses: этап жизненного цикла → имя статуса в вашем экземпляре.\n")
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    print(f"Готово: {path.name}. Закоммитьте его — коллегам init не понадобится.")
    if args.rules:
        cmd_rules(find_project_config(), ask=sys.stdin.isatty())
    return 0


def cmd_rules(config: ProjectConfig, remove: bool = False, ask: bool = False) -> int:
    if not config.path:
        raise LookupError("репозиторий не подключён: opsync init")
    if remove:
        done = [n for n in rules.FILES if rules.remove(config.root / n)]
        print(f"Блок opsync убран из: {', '.join(done)}." if done else "Блока opsync нет.")
        return 0
    paths = rules.targets(config.root)
    names = ", ".join(p.name for p in paths)
    if ask and not setup.ask(f"Дописать в {names} правило ведения задач для агентов?"):
        print("Пропущено (позже: opsync rules).")
        return 0
    for p in paths:
        print(f"{p.name}: блок opsync {rules.write(p, config.tracker)}.")
    print("Закоммитьте — правило увидят агенты всех, кто работает с репозиторием.")
    return 0


def cmd_hooks(action: str) -> int:
    home = Path.home()
    if action != "status":
        setup.set_hooks(home, action == "on")
    state = setup.hooks_state(home)
    print(f"Хуки Claude Code: {state} ({setup.claude_settings(home)})")
    if state == "нет" and action == "status":
        print("Включить: opsync hooks on")
    elif action != "status":
        print("Действует с новой сессии Claude Code.")
    return 0


def cmd_upgrade() -> int:
    import shutil
    import subprocess
    uv = shutil.which("uv")
    if not uv:
        raise LookupError("нужен uv: https://docs.astral.sh/uv/getting-started/installation/")
    subprocess.run([uv, "tool", "upgrade", "opsync"], check=True)
    # Скиллы — уже новой версией: свежий процесс, доступ не трогаем.
    return subprocess.run(["opsync", "setup", "--yes", "--no-new-hooks"]).returncode


def choose_project(client: Client) -> str:
    projects = sorted(client.collection("/projects"), key=lambda p: p["name"].lower())
    if not projects:
        raise LookupError("вам не доступен ни один проект — попросите добавить вас участником")
    listing = "\n".join(f"  {i:>2}. {p['name']}  ({p['identifier']})"
                        for i, p in enumerate(projects, 1))
    if not sys.stdin.isatty():
        raise LookupError(f"укажите проект: opsync init --project <identifier>\n{listing}")
    print(f"Проекты:\n{listing}")
    while True:
        answer = input("Номер проекта: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(projects):
            return projects[int(answer) - 1]["identifier"]
        print(f"Введите число от 1 до {len(projects)}.")


def choose_statuses(client: Client) -> dict[str, str]:
    names = [s["name"] for s in client.collection("/statuses")]
    found = detect_statuses(names)
    out: dict[str, str] = {}
    for stage, status in found.items():
        if status:
            out[stage] = status
            continue
        if stage in OPTIONAL_STAGES:
            continue                                # нет подходящего — этап просто не нужен
        if not sys.stdin.isatty():
            out[stage] = DEFAULT_STATUSES[stage]
            print(f"Этап {stage}: подходящий статус не найден — поправьте statuses в "
                  f".openproject.yaml (есть: {', '.join(names)})", file=sys.stderr)
            continue
        print(f"Какой статус означает «{STAGE_TITLES[stage]}»?")
        for i, n in enumerate(names, 1):
            print(f"  {i:>2}. {n}")
        while True:
            answer = input("Номер: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(names):
                out[stage] = names[int(answer) - 1]
                break
    print("Этапы: " + ", ".join(f"{k} → {v}" for k, v in out.items()))
    return out


STAGE_TITLES = {"start": "взял в работу", "review": "готово, на проверке",
                "done": "принято, закрыто", "reopen": "вернуть в работу",
                "blocked": "ждёт чего-то извне"}


def cmd_doctor(client: Client, config: ProjectConfig) -> int:
    creds = load_credentials()
    ok = True
    print(f"Доступ:      {creds.url} (из {creds.source})")
    try:
        me = client.get("/users/me")
        print(f"Вы:          {me['name']}")
    except ApiError as e:
        print(f"Вы:          ОШИБКА {e}")
        return 1
    for title, state in setup.agents_state():
        mark = {"нет": "не подключён — opsync setup", "устарел": "устарел — opsync setup"}
        state = mark.get(state, state)
        for hooks in ("хуки: нет", "хуки: устарели"):
            state = state.replace(hooks, f"{hooks} — opsync hooks on")
        print(f"Агент:       {title}: {state}")
    print(f"Конфиг:      {config.path or 'нет .openproject.yaml — подключить: opsync init'}")
    if config.project:
        try:
            p = client.get(f"/projects/{config.project}")
            print(f"Проект:      {p['name']} ({p['identifier']})")
        except ApiError as e:
            ok = False
            print(f"Проект:      ОШИБКА {config.project}: {e}")
    if config.plan:
        # Плана может ещё не быть: ссылки по id работают и без него.
        hint = "" if config.plan.is_file() else "  — файла ещё нет (opsync export -o …)"
        print(f"План:        {config.plan}{hint}")
    if config.tracker:
        print(f"Трекер:      {config.tracker}")
    all_names = [s["name"] for s in client.collection("/statuses")]
    names = {n.lower() for n in all_names}
    print("Этапы:")
    for stage, status in config.statuses.items():
        found = status.lower() in names
        ok &= found
        print(f"  {stage:8} → {status}" + ("" if found else "  — НЕТ ТАКОГО СТАТУСА"))
    for stage in sorted(OPTIONAL_STAGES - set(config.statuses)):
        guess = detect_statuses(all_names).get(stage)
        hint = f"добавьте в statuses: «{stage}: {guess}»" if guess else "не настроен"
        print(f"  {stage:8} — {hint} (opsync {'block' if stage == 'blocked' else stage})")
    print("\nВсё в порядке." if ok else "\nЕсть проблемы — см. выше.")
    return 0 if ok else 1


# -------------------------------------------------------------------- план
def cmd_inspect(client: Client, project_ref: str | None) -> int:
    cat = Catalog.load(client)
    print(f"OpenProject {client.get('/').get('coreVersion')} · вы: {cat.me['name']}")
    if not project_ref:
        seen = set()
        print("\nПроекты:")
        for p in cat.projects.values():
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            parent = p["_links"].get("parent", {}).get("title")
            print(f"  {p['identifier']:40} {p['name']}" + (f"  ← {parent}" if parent else ""))
        print("\nТипы:      ", ", ".join(cat.types))
        print("Статусы:   ", ", ".join(cat.statuses))
        print("Приоритеты:", ", ".join(cat.priorities))
        return 0
    p = cat.project(project_ref)
    pid = p["id"]
    types = [t["name"] for t in client.get(f"/projects/{pid}/types")["_embedded"]["elements"]]
    print(f"\n{p['name']} ({p['identifier']})")
    print("  Типы:      ", ", ".join(types) or "—")
    print("  Категории: ", ", ".join(cat.categories(pid)) or "—")
    print("  Спринты:   ", ", ".join(
        f"{s['name']} ({s.get('startDate')}…{s.get('finishDate')})"
        for s in cat.sprints(pid).values()) or "—")
    print("  Версии:    ", ", ".join(cat.versions(pid)) or "—")
    print("  Участники: ", ", ".join(m["title"] for m in cat.members(pid).values()) or "—")
    return 0


def cmd_export(client: Client, project_ref: str, output: str | None) -> int:
    cat = Catalog.load(client)
    pid = cat.project(project_ref)["id"]
    flt = [{"status": {"operator": "*", "values": []}},
           {"project": {"operator": "=", "values": [str(pid)]}}]
    items = []
    for wp in client.collection(f"/projects/{pid}/work_packages", filters=flt):
        links = wp["_links"]
        item = {"id": wp["id"], "subject": wp["subject"], "type": links["type"]["title"],
                "status": links["status"]["title"]}
        for name in ("assignee", "category", "sprint", "priority"):
            if (links.get(name) or {}).get("href"):
                item[name] = links[name]["title"]
        if (links.get("parent") or {}).get("href"):
            item["parent"] = f"#{href_id(links['parent']['href'])}"
        for src, dst in (("startDate", "start"), ("dueDate", "due"), ("date", "date")):
            if wp.get(src):
                item[dst] = wp[src]
        items.append(item)
    text = yaml.safe_dump({"items": items}, allow_unicode=True, sort_keys=False)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
        print(f"{len(items)} задач → {output}")
    else:
        print(text)
    return 0


def cmd_pull(client: Client, plan_path: Path, dry: bool, adopt: bool) -> int:
    result = pull(client, load(plan_path), dry, adopt)
    if result.changes:
        print(("Будет обновлено" if dry else "Обновлено") + f" в плане: {len(result.changes)}")
        for key, change in result.changes.items():
            was = result.before[key]
            print(f"  {key}: " + ", ".join(f"{k} {was.get(k) or '—'} → {v}"
                                          for k, v in change.items()))
    else:
        print("План совпадает с сервером.")
    if result.untracked:
        verb = "Добавлены в план" if result.adopted else "Есть на сервере, нет в плане"
        print(f"\n{verb}: {len(result.untracked)}")
        for wp in result.untracked:
            links = wp["_links"]
            print(f"  #{wp['id']:<5} {links['status']['title']:<14} {wp['subject']}"
                  f"  [{links['project']['title']}]")
        if not result.adopted:
            print("Взять под план: opsync pull --adopt")
    if (result.changes or result.adopted) and not dry:
        print("\nЗакоммитьте план.")
    return 0


def cmd_schedule(client: Client, plan_path: Path, as_json: bool) -> int:
    plan = load(plan_path)
    result = plan_schedule(client, Catalog.load(client), plan)
    if as_json:
        print(json.dumps({"slots": [s.__dict__ for s in result.slots.values()],
                          "warnings": result.warnings}, ensure_ascii=False, indent=1,
                         default=str))
        return 0
    if not plan.calendar.schedule:
        print("calendar.schedule в плане не включён: apply эти даты не запишет.\n")
    groups: dict[str, list] = {}
    for slot in result.slots.values():
        groups.setdefault(slot.sprint or "Без спринта", []).append(slot)
    for sprint, slots in groups.items():
        w = result.windows.get(sprint)
        dates = f" ({w.start:%d.%m}–{w.finish:%d.%m})" if w else ""
        total = sum(s.hours or 0 for s in slots)
        print(f"{sprint}{dates} · {total:g} ч")
        for s in sorted(slots, key=lambda s: (s.start or dt.date.max, s.key)):
            span = "–".join(f"{d:%d.%m}" for d in dict.fromkeys((s.start, s.due)) if d) or "—"
            est = f"{s.hours:g}ч" if s.hours is not None else ""
            label = plan.nodes[s.key].label
            title = s.key if label == s.key else f"{s.key}: {label}"
            mark = "" if s.kind == "план" else f"  [{s.kind}]"
            print(f"  {span:11} {est:>5}  {s.assignee or '—':10} {title}{mark}")
        print()
    if result.warnings:
        print("Предупреждения:")
        for w in result.warnings:
            print(f"  ! {w}")
    return 0


def cmd_apply(client: Client, plan_path: Path, dry: bool, allow_missing: bool) -> int:
    plan = load(plan_path)
    applier = Applier(client, plan, dry_run=dry, allow_missing=allow_missing)
    report = applier.run()
    title = "Пробный прогон — ничего не записано" if dry else "Готово"
    if report.warnings:
        print("\nПланировщик предупреждает:")
        for w in report.warnings:
            print(f"  ! {w}")
    if report.problems:
        print("\nНужно настроить в OpenProject (через API невозможно):")
        for where, items in sorted(report.problems.items()):
            for what in sorted(items):
                print(f"  • {where}: {what}")
        if not allow_missing:
            print("\nЗадачи не создавались. Настройте и повторите, "
                  "или запустите с --allow-missing.")
            return 2
    for head, rows in (("Создать" if dry else "Создано", report.created),
                       ("Изменить" if dry else "Изменено", report.updated),
                       ("Связи «следует за»", report.relations)):
        if rows:
            print(f"\n{head} ({len(rows)}):")
            for r in rows:
                print(f"  + {r}")
    print(f"\n{title}. Без изменений: {report.unchanged}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
