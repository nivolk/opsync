import json

import pytest

from opsync.config import ProjectConfig, state_path
from opsync.plan import load
from opsync.planedit import DELETE, edit_items
from opsync.tasks import Tasks

from conftest import STATUSES, wp

NEW, IN_PROGRESS, _, _ = STATUSES
SPRINTS = [
    {"id": 4, "name": "Спринт 1", "startDate": "2026-10-05", "finishDate": "2026-10-16"},
    {"id": 5, "name": "Спринт 2", "startDate": "2026-10-19", "finishDate": "2026-10-30"},
]
PLAN = """# план с комментарием — должен сохраниться
name: demo
calendar: {schedule: SCHEDULE}

items:

  - key: epic
    subject: Эпик
    defaults: {sprint: Спринт 1}   # спринт по умолчанию
    children:
      - key: a
        subject: Задача A
        estimate: 4h
        start: 2026-10-14   # закреплено
        due: 2026-10-15
      - {key: b, subject: Задача B, estimate: 2h}
      - key: ms
        type: Веха
        subject: Веха
        date: 2026-10-16
"""


@pytest.fixture
def setup(fake, tmp_path):
    def make(schedule: bool):
        path = tmp_path / "demo.yaml"
        path.write_text(PLAN.replace("SCHEDULE", str(schedule).lower()), encoding="utf-8")
        state = state_path(path, "demo")
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps({"epic": 9, "a": 10, "b": 11, "ms": 12}), encoding="utf-8")
        fake.objects["/projects/demo"] = {"id": 3}
        fake.collections["/projects/3/sprints"] = SPRINTS
        rows = [wp(10, "Задача A", IN_PROGRESS), wp(11, "Задача B", NEW), wp(12, "Веха", NEW)]
        for r in rows:
            fake.objects[f"/work_packages/{r['id']}"] = r
        fake.collections["/work_packages"] = rows
        return Tasks(fake, ProjectConfig(project="demo", plan=path)), path
    return make


def test_carry_moves_open_tasks_to_next_sprint(fake, setup):
    tasks, path = setup(schedule=True)
    c = tasks.carry("Спринт 1", comment="не успели")
    assert (c.source, c.target) == ("Спринт 1", "Спринт 2")
    assert [r["id"] for r in c.moved] == [10, 11, 12]
    first = fake.calls[:2]                         # комментарий, потом PATCH — для каждой
    assert first[0] == ("POST", "/work_packages/10/activities", {"comment": {
        "raw": "Перенесено из «Спринт 1» в «Спринт 2».\n\nне успели"}})
    assert first[1][0:2] == ("PATCH", "/work_packages/10")
    assert first[1][2]["_links"] == {"sprint": {"href": "/api/v3/sprints/5"}}
    assert len(fake.writes("PATCH")) == 3


def test_carry_rewrites_plan_and_unpins_dates(fake, setup):
    tasks, path = setup(schedule=True)
    c = tasks.carry("Спринт 1")
    assert c.plan_keys == ["a", "b", "ms"]
    assert c.warnings == ["ms: веха с датой 2026-10-16 — перенесите дату в плане сами, "
                          "планировщик вехи не двигает"]
    text = path.read_text(encoding="utf-8")
    assert "# план с комментарием — должен сохраниться" in text
    assert "defaults: {sprint: Спринт 1}   # спринт по умолчанию" in text
    plan = load(path)
    assert plan.nodes["a"].fields["sprint"] == "Спринт 2"
    assert "start" not in plan.nodes["a"].fields and "due" not in plan.nodes["a"].fields
    assert plan.nodes["b"].fields["sprint"] == "Спринт 2"


def test_carry_keeps_dates_without_scheduler(fake, setup):
    tasks, path = setup(schedule=False)
    c = tasks.carry("Спринт 1")
    assert load(path).nodes["a"].fields["start"].isoformat() == "2026-10-14"
    assert any("даты остались от прошлого спринта у 1 задач (a)" in w for w in c.warnings)


def test_carry_dry_run_writes_nothing(fake, setup):
    tasks, path = setup(schedule=True)
    before = path.read_text(encoding="utf-8")
    c = tasks.carry("Спринт 1", dry_run=True)
    assert len(c.moved) == 3 and c.plan_keys == ["a", "b", "ms"]
    assert fake.calls == []
    assert path.read_text(encoding="utf-8") == before


def test_carry_needs_next_sprint(fake, setup):
    tasks, _ = setup(schedule=True)
    with pytest.raises(LookupError, match="после «Спринт 2» спринтов нет"):
        tasks.carry("Спринт 2")
    with pytest.raises(LookupError, match="совпадает"):
        tasks.carry("Спринт 1", "Спринт 1")


def test_edit_items_flow_style_and_missing(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("items:\n  - {key: b, subject: B}\n  - id: 84\n    parent: b\n",
                    encoding="utf-8")
    missing = edit_items(path, {"b": {"sprint": "С2"}, "#84": {"parent": DELETE},
                                "nope": {"sprint": "С2"}})
    assert missing == ["nope"]
    assert path.read_text(encoding="utf-8") == ("items:\n  - {key: b, subject: B, sprint: С2}\n"
                                                "  - id: 84\n")
