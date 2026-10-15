import json

import pytest
import yaml

from opsync.client import API
from opsync.config import ProjectConfig, state_path
from opsync.plan import load
from opsync.tasks import Tasks

from conftest import STATUSES, wp

NEW = STATUSES[0]
TYPES = [{"name": "Задача", "_links": {"self": {"href": f"{API}/types/1"}}},
         {"name": "Ошибка", "_links": {"self": {"href": f"{API}/types/7"}}}]
PLAN = """\
name: demo
defaults:
  project: demo
  type: Задача
items:
  # эпик
  - key: epic
    subject: Эпик
"""


@pytest.fixture
def server(fake):
    fake.objects["/projects/demo"] = {"id": 3}
    fake.collections["/projects/3/types"] = TYPES
    fake.on_post["/projects/3/work_packages"] = lambda body: {"id": 50}
    return fake


@pytest.fixture
def planned(tmp_path) -> ProjectConfig:
    plan = tmp_path / "plan.yaml"
    plan.write_text(PLAN, encoding="utf-8")
    state = state_path(plan, "demo")
    state.parent.mkdir()
    state.write_text(json.dumps({"epic": 40}), encoding="utf-8")
    return ProjectConfig(project="demo", plan=plan)


def test_description_required(server):
    with pytest.raises(LookupError, match="нужно описание"):
        Tasks(server, ProjectConfig(project="demo")).new("Тема", "  ")
    assert server.calls == []


def test_new_without_plan(server):
    t = Tasks(server, ProjectConfig(project="demo")).new(
        "Падает выгрузка", "Готово, когда выгрузка проходит.", type_name="ошибка",
        estimate="2h")
    assert (t.id, t.type, t.plan_item) == (50, "Ошибка", None)
    (_, path, body), = server.writes()
    assert path == "/projects/3/work_packages"
    assert body["_links"]["type"]["href"] == f"{API}/types/7"
    assert body["estimatedTime"] == "PT2H"
    assert body["description"]["raw"] == "Готово, когда выгрузка проходит."


def test_new_with_key_goes_to_plan_and_state(server, planned):
    tasks = Tasks(server, planned)
    t = tasks.new("Кнопка удаления", "Зачем: …", parent="epic", key="fe-delete",
                  sprint=None, estimate="4h")
    assert server.writes()[0][2]["_links"]["parent"] == {"href": f"{API}/work_packages/40"}
    assert t.plan_item == {"key": "fe-delete", "subject": "Кнопка удаления", "parent": "epic",
                           "estimate": "4h", "description": "Зачем: …"}
    text = planned.plan.read_text(encoding="utf-8")
    assert "# эпик" in text                                   # комментарии плана на месте
    assert load(planned.plan).nodes["fe-delete"].parent_key == "epic"
    assert json.loads(state_path(planned.plan, "demo").read_text()) == {"epic": 40,
                                                                       "fe-delete": 50}
    assert Tasks(server, planned).resolve("fe-delete") == 50


def test_new_without_key_adopts_by_id(server, planned):
    t = Tasks(server, planned).new("Тема", "Описание")
    assert t.plan_item == {"id": 50}
    assert "#50" in load(planned.plan).nodes


def test_dry_run_checks_form_and_writes_nothing(server, planned):
    before = planned.plan.read_text(encoding="utf-8")
    t = Tasks(server, planned).new("Тема", "Описание", key="k", dry_run=True)
    assert t.id is None and t.plan_item["key"] == "k"
    assert [c[1] for c in server.writes()] == ["/projects/3/work_packages/form"]
    assert planned.plan.read_text(encoding="utf-8") == before


def test_dry_run_reports_validation_errors(server):
    server.on_post["/projects/3/work_packages/form"] = lambda body: {
        "_embedded": {"validationErrors": {"subject": {"message": "слишком длинная"}}}}
    with pytest.raises(LookupError, match="subject: слишком длинная"):
        Tasks(server, ProjectConfig(project="demo")).new("Тема", "Описание", dry_run=True)


@pytest.mark.parametrize("kw, error", [
    ({"key": "epic"}, "уже есть"),
    ({"type_name": "Веха"}, "не включён в проекте. Есть: Задача, Ошибка"),
])
def test_new_refuses(server, planned, kw, error):
    with pytest.raises(LookupError, match=error):
        Tasks(server, planned).new("Тема", "Описание", **kw)
    assert server.calls == []


def test_describe_appends_and_syncs_plan(server, planned):
    planned.plan.write_text(PLAN + "    description: Старое\n", encoding="utf-8")
    server.objects["/work_packages/40"] = wp(40, "Эпик", NEW, description={"raw": "Старое"})
    new = Tasks(server, planned).update_description(40, "Готово, когда …")
    assert new == "Старое\n\nГотово, когда …"
    assert server.writes()[0][2]["description"]["raw"] == new
    assert yaml.safe_load(planned.plan.read_text())["items"][0]["description"] == new
