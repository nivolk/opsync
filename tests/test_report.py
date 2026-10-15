import datetime as dt

import pytest

from opsync import report
from opsync.client import API, ApiError
from opsync.config import ProjectConfig
from opsync.tasks import Tasks

from conftest import status, wp
from test_board import SPRINTS, WEEK, NEW, WORK, DONE

REVIEW = {**status(10, "В тестировании"), "position": 10, "isClosed": False}
WAIT = {**status(14, "В ожидании"), "position": 14, "isClosed": False}
TODAY = dt.date(2026, 10, 8)
ANYA = {"assignee": {"title": "Аня"}}


@pytest.fixture
def tasks(fake):
    fake.collections["/statuses"] = [DONE, NEW, WORK, REVIEW, WAIT]
    fake.objects["/projects/demo"] = {"id": 3, "name": "Демо"}
    fake.objects["/configuration"] = {"hoursPerDay": 8}
    fake.objects["/days/week"] = WEEK
    fake.objects["/users/6"] = {"name": "Аня"}
    fake.collections["/projects/3/sprints"] = SPRINTS
    work = wp(2, "Идёт", WORK, dueDate="2026-10-15", links=ANYA)
    review = wp(7, "Ждёт приёмки", REVIEW, links=ANYA)
    blocked = wp(9, "Нет доступа", WAIT)
    todo = wp(3, "Следующая", NEW, dueDate="2026-10-15")
    late = wp(8, "Просрочена", NEW, dueDate="2026-10-01", links=ANYA)
    done = wp(1, "Сделана", DONE)
    open_rows = [work, todo, review, late, blocked]
    fake.collections["/projects/3/work_packages"] = open_rows
    fake.collections["/work_packages"] = [work, todo, done]           # доска спринта
    fake.collections["/work_packages/9/activities"] = [{
        "version": 2, "createdAt": "2026-10-07T10:00:00Z",
        "comment": {"raw": "Жду доступ к стенду от админа"}, "details": [],
        "_links": {"user": {"href": f"{API}/users/6"}}}]
    config = ProjectConfig(project="demo", statuses={
        "start": "В процессе", "review": "В тестировании", "done": "закрыто",
        "reopen": "Новое", "blocked": "В ожидании"})
    return Tasks(fake, config)


def build(tasks, fake, **kw):
    # «сделано» — отдельный запрос с фильтром закрытых; фейк фильтры не применяет
    rows = fake.collections["/projects/3/work_packages"]
    real = list(rows)

    def collection(path, page_size=200, **params):
        fake.queries.append((path, params))
        flt = params.get("filters") or []
        if path == "/projects/3/work_packages" and {"status": {"operator": "c", "values": []}} in flt:
            yield wp(1, "Сделана", DONE)
            return
        yield from (real if path == "/projects/3/work_packages" else
                    fake.collections.get(path, []))
    fake.collection = collection
    return report.build(tasks, today=TODAY, **kw)


def test_report_sections(tasks, fake):
    r = build(tasks, fake, summary="Сервер готов, ждём доступ.")
    assert [x["id"] for x in r.done] == [1]
    assert [x["id"] for x in r.review] == [7]
    assert [x["id"] for x in r.active] == [2]
    assert [(x["id"], x["reason"]) for x in r.blocked] == [(9, "Жду доступ к стенду от админа")]
    assert [x["id"] for x in r.next] == [3]
    assert r.risks == ["#8 Просрочена: срок 01.10 прошёл, «Новое», Аня"]
    assert r.health == "at_risk"
    text = report.markdown(r)
    assert text.startswith("**Главное.** Сервер готов, ждём доступ.\n")
    assert "**Ждёт** (1)\n- #9 Нет доступа: Жду доступ к стенду от админа" in text
    assert "**В работе** (1)\n- #2 Идёт — Аня, до 15.10" in text


def test_health_never_off_track_by_itself(tasks, fake):
    fake.collections["/projects/3/work_packages"] = [
        wp(i, f"Просрочена {i}", NEW, dueDate="2026-10-01") for i in range(20, 26)]
    assert build(tasks, fake).health == "at_risk"
    assert build(tasks, fake, health="off_track").health == "off_track"


def test_publish_sets_status_and_explanation(tasks, fake):
    r = build(tasks, fake)
    fake.on_post["/news"] = lambda body: {"id": 3}
    out = report.publish(tasks, r, news=True)
    (m, path, body), (m2, path2, news) = fake.writes()
    assert (m, path) == ("PATCH", "/projects/3")
    assert body["_links"]["status"]["href"] == f"{API}/project_statuses/at_risk"
    assert body["statusExplanation"]["raw"] == report.markdown(r)
    assert (m2, path2, news["title"]) == ("POST", "/news", "Итоги: Спринт 1")
    assert out == {"project": "demo", "health": "at_risk", "news": 3}


def test_publish_without_rights_explains(tasks, fake):
    r = build(tasks, fake)
    fake.patch_error = ApiError("PATCH", "/projects/3", 403, {"message": "нет"})
    with pytest.raises(LookupError, match="Изменить проект"):
        report.publish(tasks, r)


def test_unknown_health(tasks, fake):
    with pytest.raises(LookupError, match="допустимы on_track"):
        build(tasks, fake, health="great")
