import datetime as dt

import pytest

from opsync.client import API
from opsync.config import ProjectConfig
from opsync.tasks import Tasks

from conftest import wp

D = dt.date
WEEK = {"_embedded": {"elements": [{"day": d, "working": d < 6} for d in range(1, 8)]}}


def st(sid, name, position, closed=False):
    return {"id": sid, "name": name, "position": position, "isClosed": closed,
            "_links": {"self": {"href": f"{API}/statuses/{sid}"}}}


NEW, WORK, DONE = st(1, "Новое", 1), st(7, "В процессе", 7), st(12, "закрыто", 12, closed=True)
SPRINTS = [
    {"id": 4, "name": "Спринт 1", "startDate": "2026-10-05", "finishDate": "2026-10-16"},
    {"id": 5, "name": "Спринт 2", "startDate": "2026-10-19", "finishDate": "2026-10-30"},
]


@pytest.fixture
def board_fake(fake):
    fake.collections["/statuses"] = [DONE, NEW, WORK]
    fake.objects["/projects/demo"] = {"id": 3}
    fake.objects["/configuration"] = {"hoursPerDay": 8}
    fake.objects["/days/week"] = WEEK
    fake.collections["/projects/3/sprints"] = SPRINTS
    fake.collections["/days/non_working"] = [{"date": "2026-10-09"}]
    fake.collections["/work_packages"] = [
        wp(1, "Сделана", DONE, estimatedTime="PT8H", spentTime="PT6H", dueDate="2026-10-06"),
        wp(2, "Идёт", WORK, estimatedTime="PT16H", remainingTime="PT10H", dueDate="2026-10-07",
           links={"assignee": {"title": "Аня"}}),
        wp(3, "Ждёт", NEW, estimatedTime="PT30H", dueDate="2026-10-15",
           links={"assignee": {"title": "Аня"}}),
    ]
    return fake


def board(fake, **kw):
    return Tasks(fake, ProjectConfig(project="demo")).board(today=D(2026, 10, 8), **kw)


def test_current_sprint_by_default(board_fake):
    b = board(board_fake)
    # 08.10–16.10 без 09.10 (праздник) и выходных: 08, 12–16
    assert b["sprint"] == {"id": 4, "name": "Спринт 1", "start": "2026-10-05",
                           "finish": "2026-10-16", "days_left": 6}


def test_columns_in_status_order_and_totals(board_fake):
    b = board(board_fake)
    assert [c["status"] for c in b["columns"]] == ["Новое", "В процессе", "закрыто"]
    assert b["totals"] == {"tasks": 3, "closed": 1, "estimate": 54, "remaining": 40, "spent": 6}


def test_overdue_and_overload(board_fake):
    b = board(board_fake)
    assert [r["id"] for r in b["overdue"]] == [2]              # закрытая #1 не просрочена
    assert b["load"] == [{"assignee": "Аня", "remaining": 40, "capacity": 48, "over": False}]
    board_fake.collections["/days/non_working"] += [{"date": d} for d in
                                                     ("2026-10-12", "2026-10-13")]
    assert board(board_fake)["load"][0]["over"] is True       # 40 ч при 32


def test_sprint_by_name_and_upcoming(board_fake):
    assert board(board_fake, sprint="спринт 2")["sprint"]["name"] == "Спринт 2"
    with pytest.raises(LookupError, match="Есть: Спринт 1, Спринт 2"):
        board(board_fake, sprint="Спринт 9")
    tasks = Tasks(board_fake, ProjectConfig(project="demo"))
    assert tasks.sprint(None, today=D(2026, 10, 17))["name"] == "Спринт 2"  # между спринтами
    assert tasks.sprint(None, today=D(2026, 12, 1))["name"] == "Спринт 2"   # все прошли


def test_board_filters_by_sprint_id(board_fake):
    """Фильтр по спринту уходит в запрос; фейк его не применяет — проверяем сам запрос."""
    tasks = Tasks(board_fake, ProjectConfig(project="demo"))
    calls = []
    original = board_fake.collection
    board_fake.collection = lambda path, **kw: calls.append((path, kw)) or original(path, **kw)
    tasks.board(today=D(2026, 10, 8))
    path, kw = next(c for c in calls if c[0] == "/work_packages")
    assert {"sprint": {"operator": "=", "values": ["4"]}} in kw["filters"]


def test_list_sprint_filter_on_server(board_fake):
    calls = []
    original = board_fake.collection
    board_fake.collection = lambda path, **kw: calls.append((path, kw)) or original(path, **kw)
    rows = Tasks(board_fake, ProjectConfig(project="demo")).list(sprint="Спринт 2")
    path, kw = calls[-1]
    assert path == "/work_packages"
    assert {"sprint": {"operator": "=", "values": ["5"]}} in kw["filters"]
    assert len(rows) == 3                       # фейк фильтр не применяет — всё, что вернул


def test_list_unknown_sprint(board_fake):
    with pytest.raises(LookupError, match="спринт 'Спринт 7' не найден"):
        Tasks(board_fake, ProjectConfig(project="demo")).list(sprint="Спринт 7")
