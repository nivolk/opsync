"""Где opsync берёт настройки, когда запущен из любого каталога.

Доступ к экземпляру (OP_URL, OP_TOKEN) — первое найденное:
  1. переменные окружения;
  2. `.env` в текущем каталоге;
  3. `~/.config/opsync/.env` (или `$XDG_CONFIG_HOME/opsync/.env`).

Привязка репозитория к OpenProject — файл `.openproject.yaml`, ищется от текущего каталога
вверх до корня. Без него работают только команды, которым проект не нужен (show/status по id).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAMES = (".openproject.yaml", ".openproject.yml")

# Этапы жизненного цикла задачи → статус OpenProject. Имена статусов у каждого экземпляра
# свои, поэтому переопределяются в `.openproject.yaml` (раздел `statuses`).
DEFAULT_STATUSES = {
    "start": "В процессе",
    "review": "В тестировании",
    "done": "закрыто",
    "reopen": "Новое",
}

# Как этапы обычно называются в экземплярах OpenProject (русский и английский интерфейс).
STAGE_SYNONYMS = {
    "start": ("в процессе", "в работе", "in progress", "in work"),
    "review": ("в тестировании", "на проверке", "на тестировании", "in testing", "in review",
               "testing", "review", "разработан", "developed"),
    "done": ("закрыто", "закрыта", "закрыт", "closed", "done", "выполнено", "resolved"),
    "reopen": ("новое", "новая", "новый", "new", "open", "открыто"),
    "blocked": ("в ожидании", "ожидание", "заблокировано", "заблокирована", "приостановлено",
                "on hold", "blocked", "waiting"),
}
# Необязательные этапы: в экземпляре может не быть подходящего статуса — тогда этап не
# настраивается, а команда объясняет, как его добавить.
OPTIONAL_STAGES = {"blocked"}


def detect_statuses(names: list[str]) -> dict[str, str | None]:
    """Этап → статус экземпляра по синонимам; None — не нашлось."""
    by_lower = {n.lower(): n for n in names}
    return {stage: next((by_lower[s] for s in options if s in by_lower), None)
            for stage, options in STAGE_SYNONYMS.items()}


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "opsync"


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@dataclass
class Credentials:
    url: str
    token: str
    source: str


def load_credentials() -> Credentials:
    url, token = os.environ.get("OP_URL"), os.environ.get("OP_TOKEN")
    if url and token:
        return Credentials(url, token, "окружение")
    for path in (Path.cwd() / ".env", user_config_dir() / ".env"):
        env = _read_env(path)
        if env.get("OP_URL") and env.get("OP_TOKEN"):
            return Credentials(env["OP_URL"], env["OP_TOKEN"], str(path))
    raise SystemExit("Нет доступа к OpenProject. Запустите: opsync setup")


@dataclass
class ProjectConfig:
    """Содержимое `.openproject.yaml`. Пустой конфиг — когда файла нет."""

    path: Path | None = None
    project: str | None = None          # identifier проекта OpenProject по умолчанию
    plan: Path | None = None            # YAML-план проекта, если задачи ведутся планом
    statuses: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUSES))
    tracker: str | None = None          # локальный трекер задач (docs/TASKS.md) — для агента

    @property
    def root(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def status_for(self, stage: str) -> str:
        try:
            return self.statuses[stage]
        except KeyError:
            where = self.path or ".openproject.yaml"
            raise LookupError(f"этап {stage!r} не настроен: добавьте в {where} раздел "
                              f"statuses строку «{stage}: <имя статуса>» (список статусов: "
                              "opsync inspect)") from None


def find_project_config(start: Path | None = None) -> ProjectConfig:
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        for name in CONFIG_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return _parse(candidate)
    return ProjectConfig()


def _parse(path: Path) -> ProjectConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = set(raw) - {"project", "plan", "statuses", "tracker"}
    if unknown:
        raise SystemExit(f"{path}: неизвестные поля {sorted(unknown)}. Допустимы: project, "
                         "plan, statuses, tracker")
    plan = raw.get("plan")
    plan_path = None
    if plan:
        plan_path = Path(os.path.expanduser(str(plan)))
        if not plan_path.is_absolute():
            plan_path = (path.parent / plan_path).resolve()
    statuses = dict(DEFAULT_STATUSES)
    statuses.update(raw.get("statuses") or {})
    return ProjectConfig(path=path, project=raw.get("project"), plan=plan_path,
                         statuses=statuses, tracker=raw.get("tracker"))


def state_path(plan_path: Path, plan_name: str) -> Path:
    """Соответствие key → id лежит рядом с планом, а не в текущем каталоге.

    Иначе запуск из другого каталога терял бы его, и задачи сопоставлялись бы только по
    теме — с риском создать дубли после переименования.
    """
    return plan_path.resolve().parent / ".state" / f"{plan_name}.json"
