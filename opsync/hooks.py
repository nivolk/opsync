"""Хуки Claude Code: агент видит задачи с начала сессии и не забывает о них после коммита.

    python -m opsync.hooks session   SessionStart: сводка `opsync context` в контекст агента
    python -m opsync.hooks commit    PostToolUse(Bash): после `git commit` — подсказка, что
                                     сделать с задачей; сам хук в OpenProject не пишет

Хук срабатывает на каждый вызов Bash, поэтому до проверки «это git commit» модуль ничего
тяжёлого не импортирует. Любая ошибка — молчание: хук не должен мешать работе.
"""

from __future__ import annotations

import json
import os
import re
import sys

GIT_COMMIT = re.compile(r"\bgit\b(?:\s+-[cC]\s+\S+)*\s+commit\b")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    try:
        if data.get("cwd"):
            os.chdir(data["cwd"])
        if argv[:1] == ["session"]:
            from .cli import cmd_context_hook
            return cmd_context_hook()
        if argv[:1] == ["commit"]:
            command = (data.get("tool_input") or {}).get("command") or ""
            if not GIT_COMMIT.search(command):
                return 0
            hint = commit_hint()
            if hint:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PostToolUse", "additionalContext": hint}},
                    ensure_ascii=False))
    except (Exception, SystemExit):                    # noqa: BLE001 — хук не должен мешать
        pass
    return 0


def commit_hint() -> str | None:
    """Что агенту сделать с задачей после коммита; None — подсказывать нечего."""
    from .client import Client
    from .config import find_project_config, load_credentials
    from .gitinfo import head
    from .tasks import Tasks

    config = find_project_config()
    commit = head()
    if not config.project or not commit:
        return None
    creds = load_credentials()
    tasks = Tasks(Client(creds.url, creds.token, timeout=5), config)
    start = config.statuses["start"].lower()
    title = f"Коммит {commit.short} «{commit.subject}»"
    if not commit.refs:
        active = [r for r in tasks.list(mine=True) if (r["status"] or "").lower() == start]
        if not active:
            return (f"{title} не связан с задачей OpenProject, и задач в работе нет. Если это "
                    "работа по задаче — `opsync start <номер>`; если новая — предложите "
                    "пользователю `opsync new`. В сообщениях коммитов пишите OP#<номер>.")
        listing = ", ".join(f"#{r['id']} {r['subject']}" for r in active[:5])
        return (f"{title} без ссылки на задачу OpenProject. В работе: {listing}. В следующих "
                "коммитах пишите OP#<номер>; если работа новая — предложите пользователю "
                "`opsync new`.")
    stages = {k: v.lower() for k, v in config.statuses.items()}
    hints = []
    for wid in commit.refs[:3]:
        row = tasks.summary(tasks.get(wid))
        status = (row["status"] or "").lower()
        name = f"#{wid} «{row['subject']}»"
        if status == start:
            hints.append(f"{name} в работе: если готово и проверки прошли — `opsync review "
                         f"{wid} -m \"…\"` (коммит подставится сам); если нет — короткая "
                         f"заметка `opsync note {wid} -m \"…\"`: что сделано и что дальше.")
        elif status == stages.get("review"):
            hints.append(f"{name} уже на проверке: если это доработка по замечаниям — "
                         f"`opsync note {wid} -m \"…\"`.")
        elif status == stages.get("reopen") or status not in stages.values():
            hints.append(f"{name} ещё не в работе («{row['status']}»): `opsync start {wid}`.")
    return f"{title}. " + " ".join(hints) if hints else None


if __name__ == "__main__":
    sys.exit(main())
