"""Правило ведения задач в файле инструкций репозитория (AGENTS.md, CLAUDE.md).

Скилл срабатывает, только когда агент о нём вспомнил; хуки есть не у всех агентов. Блок в
AGENTS.md (Codex, Cursor) и CLAUDE.md (Claude Code) агент читает в начале каждой сессии.
Блок — между маркерами: повторный запуск обновляет его, текст вокруг не трогает.
"""

from __future__ import annotations

import re
from pathlib import Path

BEGIN, END = "<!-- opsync:begin -->", "<!-- opsync:end -->"
FILES = ("AGENTS.md", "CLAUDE.md")
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)


def block(tracker: str | None = None) -> str:
    lines = [
        BEGIN,
        "## Задачи в OpenProject",
        "",
        "Задачи проекта ведутся в OpenProject через `opsync` (скилл `openproject-tasks`): по",
        "ним команда видит, что сделано, что в работе и что мешает.",
        "",
        "- В начале сессии — `opsync context`: мои задачи, что дальше, риски.",
        "- Взял задачу — `opsync start N -m \"…\"`; заметный шаг или конец сессии —",
        "  `opsync note N -m \"…\"`; ждёшь кого-то — `opsync block N -m \"…\"`; готово и",
        "  закоммичено — `opsync review N -m \"…\"`.",
        "- В сообщении коммита — `OP#N`.",
        "- Новая работа, найденная по ходу, — `opsync new` с согласия пользователя.",
        "- Комментарии — коротко и простыми словами, для коллег без контекста сессии.",
    ]
    if tracker:
        lines.append(f"- Локальный трекер — `{tracker}`: рядом с задачей пиши `(OP #N)`.")
    lines += ["- Закрытие (`done`), сроки и массовые правки — только с согласия пользователя.",
              END]
    return "\n".join(lines) + "\n"


def targets(root: Path) -> list[Path]:
    """Куда писать блок: существующие файлы инструкций; если нет ни одного — AGENTS.md.

    CLAUDE.md, который лишь отсылает к AGENTS.md (`@AGENTS.md` или ссылкой), не трогаем:
    правило должно быть в одном месте, а Claude Code прочитает его через ссылку.
    """
    found = [root / name for name in FILES if (root / name).is_file()]
    agents, claude = root / "AGENTS.md", root / "CLAUDE.md"
    if agents in found and claude in found and "AGENTS.md" in claude.read_text(encoding="utf-8"):
        found.remove(claude)
    return found or [root / FILES[0]]


def write(path: Path, tracker: str | None = None) -> str:
    """Добавить или обновить блок: «добавлен» | «обновлён» | «актуален»."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    new = block(tracker)
    if BLOCK.search(text):
        current = BLOCK.search(text).group(0)
        if current.rstrip("\n") == new.rstrip("\n"):
            return "актуален"
        path.write_text(BLOCK.sub(lambda m: new, text, count=1), encoding="utf-8")
        return "обновлён"
    sep = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    path.write_text(text + sep + new, encoding="utf-8")
    return "добавлен"


def remove(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if not BLOCK.search(text):
        return False
    path.write_text(BLOCK.sub("", text, count=1).rstrip("\n") + "\n", encoding="utf-8")
    return True
