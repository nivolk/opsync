"""Мастер первого запуска: доступ к OpenProject и подключение агентов.

Повторный запуск безопасен: каждый шаг показывает, что уже сделано, и спрашивает, прежде
чем что-то поменять. Файлы агентов перед правкой копируются в `.bak`.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .client import ApiError, Client
from .config import user_config_dir

SKILL = "openproject-tasks"


# ------------------------------------------------------------------ доступ
def save_credentials(url: str, token: str) -> Path:
    folder = user_config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / ".env"
    path.write_text(f"OP_URL={url}\nOP_TOKEN={token}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def check_access(url: str, token: str) -> str:
    """Имя пользователя токена — или LookupError с понятной причиной."""
    try:
        return Client(url, token).get("/users/me")["name"]
    except ApiError as e:
        if e.status == 401:
            raise LookupError("токен не подошёл — создайте новый и повторите") from None
        raise LookupError(f"сервер ответил {e.status}: {e}") from None
    except Exception as e:                       # сеть, DNS, не тот адрес
        raise LookupError(f"не удалось подключиться к {url}: {e}") from None


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    for tail in ("/api/v3", "/my/access_token"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url


# ----------------------------------------------------------------- агенты
def skill_source() -> Path:
    """Скилл для агентов лежит в пакете: `opsync/skill`."""
    return Path(__file__).resolve().parent / "skill"


@dataclass
class Agent:
    key: str
    title: str
    present: Callable[[Path], bool]
    target: Callable[[Path], Path]               # куда ставить (скилл или конфиг MCP)
    kind: str                                    # skill | mcp


def desktop_config(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", home)) / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


AGENTS = [
    Agent("claude", "Claude Code", lambda h: (h / ".claude").is_dir(),
          lambda h: h / ".claude" / "skills" / SKILL, "skill"),
    Agent("agents", "Codex и Cursor", lambda h: (h / ".codex").is_dir() or (h / ".cursor").is_dir()
          or (h / ".agents").is_dir(), lambda h: h / ".agents" / "skills" / SKILL, "skill"),
    Agent("desktop", "Claude Desktop", lambda h: desktop_config(h).parent.is_dir(),
          desktop_config, "mcp"),
]


def claude_plugin_installed(home: Path) -> bool:
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    try:
        return "openproject@" in registry.read_text(encoding="utf-8")
    except OSError:
        return False


def _files(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def skill_state(target: Path) -> str:
    """нет | актуален | устарел."""
    if not (target / "SKILL.md").is_file():
        return "нет"
    return "актуален" if _files(target) == _files(skill_source()) else "устарел"


def agents_state(home: Path | None = None) -> list[tuple[str, str]]:
    """Найденные агенты и состояние подключения — для doctor."""
    home = home or Path.home()
    out = []
    for agent in AGENTS:
        if not agent.present(home):
            continue
        if agent.key == "claude" and claude_plugin_installed(home):
            out.append((agent.title, "плагин"))
        elif agent.kind == "skill":
            out.append((agent.title, skill_state(agent.target(home))))
        else:
            out.append((agent.title, mcp_state(agent.target(home))))
    return out


def install_skill(target: Path) -> None:
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_source(), target)


def mcp_entry() -> dict:
    # Тот же Python, что у установленного opsync: не зависит от PATH приложения.
    return {"command": sys.executable, "args": ["-m", "opsync", "mcp"]}


def mcp_state(config: Path) -> str:
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "нет"
    entry = (data.get("mcpServers") or {}).get("openproject")
    if not entry:
        return "нет"
    return "актуален" if entry == mcp_entry() else "устарел"


def install_mcp(config: Path) -> None:
    data: dict = {}
    if config.is_file():
        text = config.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {}
        config.with_suffix(".json.bak").write_text(text, encoding="utf-8")
    data.setdefault("mcpServers", {})["openproject"] = mcp_entry()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ мастер
def ask(question: str, default: bool = True) -> bool:
    hint = "[Д/н]" if default else "[д/Н]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer[0] in "дyд"


def run(url: str | None = None, token: str | None = None, yes: bool = False,
        home: Path | None = None) -> int:
    home = home or Path.home()
    interactive = sys.stdin.isatty() and not yes
    print("1. Доступ к OpenProject")
    env = user_config_dir() / ".env"
    have = env.is_file() and not (url or token)
    if have and (not interactive or not ask(f"   Уже настроен ({env}). Заменить?", False)):
        print("   Оставлен как есть.")
    else:
        if not url:
            if not interactive:
                raise LookupError("укажите --url и токен (--token-stdin) или запустите "
                                  "opsync setup в терминале")
            url = input("   Адрес OpenProject (например, https://op.example.com): ")
        url = normalize_url(url)
        if not token:
            if not interactive:
                raise LookupError("передайте токен: echo <токен> | opsync setup --url … "
                                  "--token-stdin")
            print(f"   Токен: {url}/my/access_token → «+ API» → скопируйте.")
            token = getpass.getpass("   Токен (ввод скрыт): ")
        token = token.strip()
        name = check_access(url, token)
        print(f"   Подключено: {name}. Сохранено в {save_credentials(url, token)}")

    print("\n2. Агенты")
    found = [a for a in AGENTS if a.present(home)]
    if not found:
        print("   Агентов не нашёл (Claude Code, Codex, Cursor, Claude Desktop). Поставьте "
              "агента и запустите opsync setup ещё раз.")
    for agent in found:
        target = agent.target(home)
        if agent.key == "claude" and claude_plugin_installed(home):
            print(f"   {agent.title}: подключён плагином openproject — ничего не нужно.")
            continue
        state = skill_state(target) if agent.kind == "skill" else mcp_state(target)
        if state == "актуален":
            print(f"   {agent.title}: уже подключён.")
            continue
        verb = "Обновить" if state == "устарел" else "Подключить"
        what = f"скилл → {target}" if agent.kind == "skill" else f"MCP-сервер → {target}"
        if interactive and not ask(f"   {verb} {agent.title}? ({what})"):
            print("   Пропущено.")
            continue
        (install_skill if agent.kind == "skill" else install_mcp)(target)
        note = " Перезапустите Claude Desktop." if agent.kind == "mcp" else ""
        print(f"   {agent.title}: готово.{note}")

    print("\nДальше: в каталоге проекта — opsync init, затем скажите агенту, например,\n"
          "«Что по спринту?» или «Возьми задачу 123 в работу».")
    return 0
