import json

import pytest

from opsync.apply import Applier
from opsync.client import API
from opsync.config import state_path
from opsync.plan import PlanError, hours, iso_hours, iso_to_hours, load

from conftest import STATUSES, wp

NEW, IN_PROGRESS, _, _ = STATUSES
PROJECT = {"id": 3, "identifier": "demo", "name": "Демо",
           "_links": {"self": {"href": f"{API}/projects/3"}}}
TYPE = {"id": 2, "name": "Задача", "_links": {"self": {"href": f"{API}/types/2"}}}


def applier(fake, tmp_path, subject: str, server_subject: str) -> Applier:
    """План с одной задачей (status: Новое), уже применённой: key → #5.

    На «сервере» #5 успели перевести в «В процессе».
    """
    plan_path = tmp_path / "demo.yaml"
    plan_path.write_text(
        "items:\n"
        "  - key: be-reset-token\n"
        "    project: demo\n"
        "    type: Задача\n"
        "    status: Новое\n"
        f"    subject: {subject}\n",
        encoding="utf-8")
    plan = load(plan_path)
    state = state_path(plan.path, plan.name)
    state.parent.mkdir()
    state.write_text(json.dumps({"be-reset-token": 5}), encoding="utf-8")

    current = wp(5, server_subject, IN_PROGRESS, links={
        "project": {"href": PROJECT["_links"]["self"]["href"]},
        "type": {"href": TYPE["_links"]["self"]["href"]},
    })
    fake.objects["/users/me"] = {"id": 1, "name": "Я"}
    fake.objects["/projects/3/types"] = {"_embedded": {"elements": [TYPE]}}
    fake.objects["/work_packages/5"] = current
    fake.collections.update({
        "/projects": [PROJECT], "/types": [TYPE], "/priorities": [],
        "/projects/3/work_packages": [current],
    })
    return Applier(fake, plan, dry_run=False, allow_missing=False)


def test_apply_keeps_status_of_existing_task(fake, tmp_path):
    """Регрессия 8d10c66: повторный apply откатывал задачу в статус из плана."""
    report = applier(fake, tmp_path, "Сброс токена v2", "Сброс токена").run()
    assert not report.problems
    patches = fake.writes("PATCH")
    assert len(patches) == 1
    _, path, body = patches[0]
    assert path == "/work_packages/5"
    assert body["subject"] == "Сброс токена v2"
    assert "status" not in body.get("_links", {})
    assert fake.objects["/work_packages/5"]["_links"]["status"]["title"] == "В процессе"


def test_apply_unchanged_task_no_writes(fake, tmp_path):
    report = applier(fake, tmp_path, "Сброс токена", "Сброс токена").run()
    assert report.unchanged == 1
    assert fake.calls == []


# ------------------------------------------------------------ длительности
@pytest.mark.parametrize("value, expected", [
    ("4h", 4), ("0.5d", 4), ("1,5д", 12), (6, 6), ("3ч", 3), (" 2 ", 2),
    ("30m", 0.5), ("90мин", 1.5), ("15 min", 0.25),
])
def test_hours(value, expected):
    assert hours(value) == expected


def test_hours_rejects_garbage():
    with pytest.raises(PlanError):
        hours("неделя")


@pytest.mark.parametrize("h, iso", [(4, "PT4H"), (1.5, "PT1H30M"), (0.25, "PT0H15M")])
def test_iso_hours_roundtrip(h, iso):
    assert iso_hours(h) == iso
    assert iso_to_hours(iso) == pytest.approx(h)


def test_iso_to_hours_day_is_24h():
    assert iso_to_hours("P1D") == 24
    assert iso_to_hours("P1DT2H") == 26
    assert iso_to_hours("мусор") == -1.0


# --------------------------------------------------------- создание проекта
ADMIN_ROLE = {"id": 5, "name": "Администратор проекта",
              "_links": {"self": {"href": f"{API}/roles/5"}}}


def project_applier(fake, tmp_path, spec: str, dry_run: bool) -> Applier:
    plan_path = tmp_path / "sandbox.yaml"
    plan_path.write_text(f"projects:\n  - identifier: sandbox\n    name: Песочница\n{spec}",
                         encoding="utf-8")
    fake.objects["/users/me"] = {"id": 1, "name": "Я",
                                 "_links": {"self": {"href": f"{API}/users/1"}}}
    fake.collections.update({"/projects": [], "/types": [TYPE], "/priorities": [],
                             "/roles": [ADMIN_ROLE]})

    def create(body):
        project = {"id": 9, "identifier": body["identifier"], "name": body["name"],
                   "_links": {"self": {"href": f"{API}/projects/9"}}}
        fake.collections["/projects"].append(project)
        return project

    fake.on_post["/projects"] = create
    return Applier(fake, load(plan_path), dry_run=dry_run, allow_missing=False)


def test_top_level_project_requires_roles(fake, tmp_path):
    report = project_applier(fake, tmp_path, "", dry_run=False).run()
    assert "нет parent — укажите roles участников" in report.problems["проект sandbox"]
    assert fake.calls == []


def test_top_level_project_unknown_role(fake, tmp_path):
    report = project_applier(fake, tmp_path, "    roles: [Владелец]\n", dry_run=True).run()
    assert any("Владелец" in p for p in report.problems["проект sandbox"])
    assert report.created == []


def test_top_level_project_created_with_roles(fake, tmp_path):
    spec = "    roles: [Администратор проекта]\n"
    report = project_applier(fake, tmp_path, spec, dry_run=False).run()
    assert not report.problems
    assert report.created == ["проект «Песочница»"]
    (_, _, project), (_, path, membership) = fake.writes("POST")
    assert project["identifier"] == "sandbox" and "parent" not in project["_links"]
    assert path == "/memberships"
    assert membership["_links"]["roles"] == [{"href": f"{API}/roles/5"}]
    assert membership["_links"]["principal"] == {"href": f"{API}/users/1"}
