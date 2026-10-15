"""Что знает git текущего репозитория о задачах: ссылки `OP#123` в сообщениях коммитов.

`OP#123` — та же запись, по которой интеграции OpenProject с GitLab и GitHub привязывают
коммиты и merge request к задаче. Без git или вне репозитория — пусто, не ошибка.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REF = re.compile(r"\bOP#(\d+)\b", re.I)


@dataclass
class Commit:
    sha: str
    subject: str
    message: str

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def refs(self) -> list[int]:
        return [int(n) for n in dict.fromkeys(REF.findall(self.message))]


def head(cwd: Path | None = None) -> Commit | None:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%H%x00%s%x00%B"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or "\0" not in out.stdout:
        return None
    sha, subject, body = out.stdout.split("\0", 2)
    return Commit(sha.strip(), subject.strip(), body.strip())


def with_commit(comment: str | None, wid: int, cwd: Path | None = None) -> str | None:
    """Комментарий к review + коммит, если последний коммит помечен `OP#<wid>`."""
    commit = head(cwd)
    if not commit or wid not in commit.refs:
        return comment
    if comment and (commit.short in comment or commit.sha in comment):
        return comment
    tail = f"Коммит {commit.short}."
    return f"{comment.rstrip()} {tail}" if comment else tail
