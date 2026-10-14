import datetime as dt

import pytest

from opsync.apply import Applier
from opsync.client import API, ApiError
from opsync.plan import PlanError, load

PROJECT = {"id": 3, "identifier": "demo", "name": "Демо",
           "_links": {"self": {"href": f"{API}/projects/3"}}}


def sprint(sid: int, name: str, start: str, finish: str) -> dict:
    return {"id": sid, "name": name, "startDate": start, "finishDate": finish,
            "_links": {"self": {"href": f"{API}/sprints/{sid}"}}}


def write_plan(tmp_path, text: str):
    path = tmp_path / "demo.yaml"
    path.write_text("defaults: {project: demo}\n" + text, encoding="utf-8")
    return load(path)


def created_wp(fake):
    """Ответ на создание задачи: сохраняет её, чтобы apply мог перечитать."""
    def create(body):
        wid = 50 + len(fake.writes("POST"))
        wp = {"id": wid, "lockVersion": 1, **body}
        fake.objects[f"/work_packages/{wid}"] = wp
        return wp
    return create


def make(fake, tmp_path, text: str, dry_run=True, sprints=(), non_working=()):
    fake.objects["/users/me"] = {"id": 1, "name": "Я"}
    fake.collections.update({
        "/projects": [PROJECT], "/types": [], "/priorities": [],
        "/projects/3/sprints": list(sprints), "/projects/3/work_packages": [],
        "/days/non_working": [{"date": d} for d in non_working],
    })
    return Applier(fake, write_plan(tmp_path, text), dry_run=dry_run, allow_missing=False)


# ----------------------------------------------------------------- формат
def test_plan_sprints_and_calendar(tmp_path):
    plan = write_plan(tmp_path, """
sprints:
  - {name: Спринт 1, start: 2026-10-06, finish: 2026-10-16}
calendar:
  hours_per_day: 6
  non_working: {2026-11-04: Перенос}
""")
    s = plan.sprint("Спринт 1")
    assert (s.start, s.finish, s.project) == (dt.date(2026, 10, 6), dt.date(2026, 10, 16), "demo")
    assert plan.calendar.hours_per_day == 6
    assert plan.calendar.non_working == {dt.date(2026, 11, 4): "Перенос"}


@pytest.mark.parametrize("text, error", [
    ("sprints:\n  - {name: С1, start: 2026-10-16, finish: 2026-10-06}\n", "start позже finish"),
    ("sprints:\n  - {name: С1, start: завтра, finish: 2026-10-06}\n", "даты YYYY-MM-DD"),
    ("sprints:\n  - {name: С1, start: 2026-10-01, finish: 2026-10-06}\n"
     "  - {name: С1, start: 2026-10-07, finish: 2026-10-09}\n", "повтор спринта"),
    ("calendar: {non_working: [2026-11-04]}\n", "словарь"),
    ("board: 1\n", "неизвестные разделы"),
])
def test_plan_format_errors(tmp_path, text, error):
    with pytest.raises(PlanError, match=error):
        write_plan(tmp_path, text)


# ---------------------------------------------------------------- спринты
SPRINTS = "sprints:\n  - {name: Спринт 1, start: 2026-10-06, finish: 2026-10-16}\n"


def test_missing_sprint_reported(fake, tmp_path):
    report = make(fake, tmp_path, SPRINTS).run()
    assert report.problems["проект «Демо»"] == {
        "создать спринт «Спринт 1» 06.10.2026–16.10.2026 (Бэклоги → + Спринт)"}


def test_sprint_dates_mismatch_reported(fake, tmp_path):
    report = make(fake, tmp_path, SPRINTS,
                  sprints=[sprint(4, "Спринт 1", "2026-10-06", "2026-10-17")]).run()
    assert any("поменять даты спринта «Спринт 1»" in p for p in report.problems["проект «Демо»"])


def test_matching_sprint_ok(fake, tmp_path):
    report = make(fake, tmp_path, SPRINTS,
                  sprints=[sprint(4, "Спринт 1", "2026-10-06", "2026-10-16")]).run()
    assert not report.problems


def test_item_sprint_planned_but_not_created_reported_once(fake, tmp_path):
    report = make(fake, tmp_path, SPRINTS + "items:\n  - {key: t, subject: T, sprint: Спринт 1}\n"
                  ).run()
    assert report.problems == {"проект «Демо»": {
        "создать спринт «Спринт 1» 06.10.2026–16.10.2026 (Бэклоги → + Спринт)"}}


# --------------------------------------------------------------- календарь
CALENDAR = "calendar:\n  non_working: {2026-11-04: Перенос, 2026-12-31: Канун}\n"


def test_calendar_dry_run_lists_missing_days(fake, tmp_path):
    report = make(fake, tmp_path, CALENDAR, non_working=["2026-12-31"]).run()
    assert report.created == ["нерабочий день 04.11.2026 (Перенос)"]
    assert fake.calls == []


def test_calendar_adds_missing_days(fake, tmp_path):
    make(fake, tmp_path, CALENDAR, dry_run=False, non_working=["2026-12-31"]).run()
    assert fake.writes("POST") == [("POST", "/days/non_working", {
        "_type": "NonWorkingDay", "date": "2026-11-04", "name": "Перенос"})]


def test_calendar_without_admin_rights(fake, tmp_path):
    def forbidden(body):
        raise ApiError("POST", "/days/non_working", 403, {"message": "нет прав"})
    fake.on_post["/days/non_working"] = forbidden
    report = make(fake, tmp_path, CALENDAR, dry_run=False).run()
    assert report.problems["экземпляр"] == {
        "нерабочий день 04.11.2026 (Перенос): нужны права администратора",
        "нерабочий день 31.12.2026 (Канун): нужны права администратора"}


# ------------------------------------------------------------ планировщик
WEEK = {"_embedded": {"elements": [{"day": d, "working": d < 6} for d in range(1, 8)]}}


def test_apply_writes_scheduled_dates(fake, tmp_path):
    fake.objects["/days/week"] = WEEK
    fake.on_post["/projects/3/work_packages"] = created_wp(fake)
    applier = make(fake, tmp_path, SPRINTS + """
calendar: {schedule: true}
items:
  - {key: a, subject: A, estimate: 12h, sprint: Спринт 1}
  - {key: b, subject: B, estimate: 4h, start: 2026-10-06, due: 2026-10-06, sprint: Спринт 1}
""", dry_run=False, sprints=[sprint(4, "Спринт 1", "2026-10-06", "2026-10-16")])
    report = applier.run()
    assert not report.problems and not report.warnings
    created = {body["subject"]: body for _, path, body in fake.writes("POST")
               if path == "/projects/3/work_packages"}
    # «b» закреплена на 06.10 и занимает 4 ч → «a»: 4 ч 06.10 + 8 ч 07.10
    assert (created["A"]["startDate"], created["A"]["dueDate"]) == ("2026-10-06", "2026-10-07")
    assert created["A"]["scheduleManually"] is True
    assert (created["B"]["startDate"], created["B"]["dueDate"]) == ("2026-10-06", "2026-10-06")


def test_apply_without_schedule_flag_leaves_dates_empty(fake, tmp_path):
    fake.on_post["/projects/3/work_packages"] = created_wp(fake)
    make(fake, tmp_path, "items:\n  - {key: a, subject: A, estimate: 12h}\n", dry_run=False).run()
    (_, _, body), = fake.writes("POST")
    assert "startDate" not in body and "dueDate" not in body


def test_closed_tasks_from_server(fake, tmp_path):
    from opsync.apply import closed_tasks
    from opsync.config import state_path
    plan = write_plan(tmp_path, "items:\n  - {key: a, subject: A}\n  - {id: 84, subject: X}\n")
    state = state_path(plan.path, plan.name)
    state.parent.mkdir()
    state.write_text('{"a": 7}', encoding="utf-8")
    calls = []
    fake.collection = lambda path, **kw: calls.append(kw["filters"]) or iter([
        {"id": 7, "startDate": "2026-10-06", "dueDate": "2026-10-07"},
        {"id": 84, "date": "2026-10-09"}])
    assert closed_tasks(fake, plan) == {"a": (dt.date(2026, 10, 6), dt.date(2026, 10, 7)),
                                        "#84": (dt.date(2026, 10, 9), dt.date(2026, 10, 9))}
    assert calls == [[{"id": {"operator": "=", "values": ["7", "84"]}},
                      {"status": {"operator": "c", "values": []}}]]


def test_dry_run_reports_scheduled_dates_once(fake, tmp_path):
    from conftest import wp, STATUSES
    fake.objects["/days/week"] = WEEK
    task = wp(7, "A", STATUSES[0], startDate="2026-10-01", dueDate="2026-10-01",
              scheduleManually=True, links={"project": {"href": f"{API}/projects/3"}})
    fake.objects["/work_packages/7"] = task
    fake.collections["/work_packages"] = []
    applier = make(fake, tmp_path, SPRINTS + "calendar: {schedule: true}\n"
                   "items:\n  - {key: a, subject: A, estimate: 4h, sprint: Спринт 1}\n",
                   sprints=[sprint(4, "Спринт 1", "2026-10-06", "2026-10-16")])
    fake.collections["/projects/3/work_packages"] = [task]
    report = applier.run()
    assert [u for u in report.updated if u.startswith("#7")] == [
        "#7 A: dueDate, estimatedTime, sprint, startDate"]
