from pathlib import Path

import pytest
import yaml

from opsync.cli import DEFAULT_PLAN, main
from opsync.config import DEFAULT_STATUSES, find_project_config


@pytest.fixture(autouse=True)
def server(fake, monkeypatch):
    fake.objects["/projects/demo"] = {"id": 3}
    fake.objects["/projects/other"] = {"id": 4}
    fake.collections["/projects"] = [{"id": 3, "name": "Демо", "identifier": "demo"},
                                     {"id": 4, "name": "Другой", "identifier": "other"}]
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    return fake


def read_config() -> dict:
    return yaml.safe_load((Path.cwd() / ".openproject.yaml").read_text(encoding="utf-8"))


def test_init_without_plan_by_default(isolated_env, capsys):
    assert main(["init", "--project", "demo"]) == 0
    assert read_config() == {"project": "demo", "statuses": DEFAULT_STATUSES}
    assert "Закоммитьте" in capsys.readouterr().out


def test_init_picks_existing_default_plan(isolated_env):
    plan = Path.cwd() / DEFAULT_PLAN
    plan.parent.mkdir()
    plan.write_text("items: []\n", encoding="utf-8")
    assert main(["init", "--project", "demo"]) == 0
    assert read_config()["plan"] == DEFAULT_PLAN
    nested = Path.cwd() / "src"
    nested.mkdir()
    assert find_project_config(nested).plan == plan.resolve()   # от корня, не от cwd


def test_init_explicit_plan_and_tracker(isolated_env):
    assert main(["init", "--project", "demo", "--plan", "plans/demo.yaml",
                 "--tracker", "docs/TASKS.md"]) == 0
    config = read_config()
    assert (config["plan"], config["tracker"]) == ("plans/demo.yaml", "docs/TASKS.md")


def test_init_does_not_overwrite(isolated_env):
    assert main(["init", "--project", "demo"]) == 0
    assert main(["init", "--project", "other"]) == 1
    assert read_config()["project"] == "demo"
    assert main(["init", "--project", "other", "--force"]) == 0
    assert read_config()["project"] == "other"


def test_init_without_project_in_pipe_lists_projects(isolated_env, capsys):
    assert main(["init"]) == 1
    err = capsys.readouterr().err
    assert "opsync init --project <identifier>" in err
    assert "1. Демо  (demo)" in err and "2. Другой  (other)" in err


def test_init_choose_project_interactively(isolated_env, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter(["9", "2", "н"])   # проект, затем отказ от блока правил
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert main(["init"]) == 0
    assert read_config()["project"] == "other"


def test_init_detects_english_statuses(isolated_env, server):
    from conftest import status
    server.collections["/statuses"] = [status(1, "New"), status(2, "In progress"),
                                       status(3, "In testing"), status(4, "Closed")]
    assert main(["init", "--project", "demo"]) == 0
    assert read_config()["statuses"] == {"start": "In progress", "review": "In testing",
                                         "done": "Closed", "reopen": "New"}


def test_init_unknown_project(isolated_env, capsys):
    assert main(["init", "--project", "nope"]) == 1
    assert not (Path.cwd() / ".openproject.yaml").exists()
