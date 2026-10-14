import datetime as dt

import pytest

from opsync.cli import main
from opsync.client import API
from opsync.config import ProjectConfig
from opsync.plan import PlanError
from opsync.tasks import Tasks

from conftest import STATUSES, wp

NEW, IN_PROGRESS, IN_REVIEW, _ = STATUSES


def test_log_time(fake):
    Tasks(fake, ProjectConfig()).log_time(5, "1.5h", "отладка", dt.date(2026, 10, 8))
    assert fake.writes() == [("POST", "/time_entries", {
        "hours": "PT1H30M", "spentOn": "2026-10-08", "comment": {"raw": "отладка"},
        "_links": {"entity": {"href": "/api/v3/work_packages/5"}}})]


def test_log_time_activity_by_name(fake):
    dev = {"name": "Разработка", "_links": {"self": {"href": f"{API}/time_entries/activities/3"}}}
    fake.on_post["/time_entries/form"] = lambda body: {"_embedded": {"schema": {"activity": {
        "_embedded": {"allowedValues": [dev]}}}}}
    tasks = Tasks(fake, ProjectConfig())
    tasks.log_time(5, "30m", activity="разработка")
    (_, _, body), = [c for c in fake.writes() if c[1] == "/time_entries"]
    assert body["_links"]["activity"] == {"href": f"{API}/time_entries/activities/3"}
    with pytest.raises(LookupError, match="Есть: Разработка"):
        tasks.log_time(5, "30m", activity="Встречи")


@pytest.mark.parametrize("amount", ["0h", "неделя"])
def test_log_time_rejects(fake, amount):
    with pytest.raises((LookupError, PlanError)):
        Tasks(fake, ProjectConfig()).log_time(5, amount)
    assert fake.calls == []


def test_stage_with_bad_spent_writes_nothing(fake, monkeypatch, isolated_env, capsys):
    """Регрессия: --spent с ошибкой формата раньше падал после смены статуса."""
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    assert main(["review", "5", "-m", "готово", "--spent", "полчаса"]) == 1
    assert "не понимаю длительность" in capsys.readouterr().err
    assert fake.calls == []


def test_stage_with_spent(fake, monkeypatch, isolated_env, capsys):
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    assert main(["review", "5", "--spent", "2h"]) == 0
    assert [c[1] for c in fake.writes()] == ["/work_packages/5", "/time_entries"]
    assert "списано 2h" in capsys.readouterr().out
