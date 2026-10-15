import datetime as dt

from opsync.client import API
from opsync.cli import main
from opsync.config import ProjectConfig
from opsync.tasks import Tasks

from conftest import STATUSES, wp
from test_board import SPRINTS, WEEK, NEW as B_NEW, WORK as B_WORK, DONE as B_DONE

NEW, IN_PROGRESS, IN_REVIEW, _ = STATUSES
USER = {"href": f"{API}/users/6"}


def activity(version, comment="", details=(), at="2026-10-14T09:30:00Z"):
    return {"version": version, "createdAt": at, "comment": {"raw": comment},
            "details": [{"raw": d} for d in details], "_links": {"user": USER}}


def test_history_newest_last_creation_collapsed(fake):
    fake.objects["/users/6"] = {"name": "Аня"}
    fake.collections["/work_packages/5/activities"] = [
        activity(1, details=["Тема присвоено значение X", "Тип присвоено значение Y"]),
        activity(2, details=["Статус изменено с Новое на В процессе"], comment="Берусь"),
        activity(3),                                      # пустая запись — пропускается
        activity(4, comment="Сервер готов, осталась кнопка"),
    ]
    tasks = Tasks(fake, ProjectConfig())
    rows = tasks.history(5)
    assert [r["changes"] for r in rows] == [["задача создана"],
                                           ["Статус изменено с Новое на В процессе"], []]
    assert rows[-1]["author"] == "Аня"
    assert tasks.history(5, limit=1)[0]["comment"] == "Сервер готов, осталась кнопка"
    assert tasks.last_comment(5)["comment"] == "Сервер готов, осталась кнопка"


def test_relations_from_this_task_point_of_view(fake):
    def rel(src, dst, kind, reverse):
        return {"type": kind, "reverseType": reverse, "_links": {
            "from": {"href": f"{API}/work_packages/{src}", "title": f"Задача {src}"},
            "to": {"href": f"{API}/work_packages/{dst}", "title": f"Задача {dst}"}}}
    fake.collections["/work_packages/5/relations"] = [rel(5, 4, "follows", "precedes"),
                                                       rel(9, 5, "blocks", "blocked")]
    assert Tasks(fake, ProjectConfig()).relations(5) == [
        {"relation": "следует за", "id": 4, "subject": "Задача 4"},
        {"relation": "заблокирована", "id": 9, "subject": "Задача 9"}]


def test_search_filter_goes_to_server(fake):
    fake.objects["/projects/demo"] = {"id": 3}
    fake.collections["/projects/3/work_packages"] = [wp(5, "Удаление вложений", NEW)]
    rows = Tasks(fake, ProjectConfig(project="demo")).list(search="вложен", include_closed=True)
    assert [r["id"] for r in rows] == [5]
    filters = fake.queries[-1][1]["filters"]
    assert {"search": {"operator": "**", "values": ["вложен"]}} in filters
    assert {"status": {"operator": "*", "values": []}} in filters


def test_show_prints_history_and_relations(fake, monkeypatch, isolated_env, capsys):
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    fake.objects["/users/6"] = {"name": "Аня"}
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS, links={
        "children": [{"href": f"{API}/work_packages/6", "title": "Подзадача"}]})
    fake.collections["/work_packages/5/activities"] = [activity(2, comment="Берусь")]
    assert main(["show", "5"]) == 0
    out = capsys.readouterr().out
    assert "Подзадачи:   #6 Подзадача" in out
    assert "Лента:" in out and "· Аня" in out and "> Берусь" in out


# ------------------------------------------------------------------ context
def context_fake(fake):
    fake.collections["/statuses"] = [B_DONE, B_NEW, B_WORK,
                                     {**IN_REVIEW, "position": 10, "isClosed": False}]
    fake.objects["/users/me"] = {"name": "Аня"}
    fake.objects["/users/6"] = {"name": "Аня"}
    fake.objects["/projects/demo"] = {"id": 3, "name": "Демо"}
    fake.objects["/configuration"] = {"hoursPerDay": 8}
    fake.objects["/days/week"] = WEEK
    fake.collections["/projects/3/sprints"] = SPRINTS
    me = {"assignee": {"title": "Аня"}}
    in_work = wp(2, "Идёт", B_WORK, dueDate="2026-10-15", links=me)
    review = wp(7, "На проверке", IN_REVIEW, links=me)
    next_up = wp(3, "Ждёт", B_NEW, dueDate="2026-10-15", links=me)
    late = wp(8, "Просрочена", B_NEW, dueDate="2026-10-01", links=me)
    fake.collections["/projects/3/work_packages"] = [in_work, next_up, review, late]
    fake.collections["/work_packages"] = [in_work, next_up]          # доска спринта
    fake.collections["/work_packages/2/activities"] = [activity(2, comment="Сервер готов")]
    return fake


def test_context_groups_my_tasks_by_stage(fake):
    c = Tasks(context_fake(fake), ProjectConfig(project="demo", tracker="docs/TASKS.md")
              ).context(today=dt.date(2026, 10, 8))
    assert [(r["id"], r["stage"]) for r in c["active"]] == [(2, "start"), (7, "review")]
    assert c["active"][0]["last"]["comment"] == "Сервер готов"
    assert c["active"][1]["last"] is None
    assert [r["id"] for r in c["next"]] == [3]                # #8 не в этом спринте
    assert c["risks"] == ["#8 Просрочена: срок 01.10 прошёл, статус «Новое»"]
    assert c["sprint"]["name"] == "Спринт 1" and c["tracker"] == "docs/TASKS.md"


def test_context_without_project_is_empty(fake):
    assert Tasks(fake, ProjectConfig()).context() == {}


def test_context_hook_is_silent_on_any_failure(isolated_env, capsys, monkeypatch):
    (isolated_env / "work" / ".openproject.yaml").write_text("project: demo\n", encoding="utf-8")
    assert main(["context", "--hook"]) == 0                  # нет доступа — молчим
    assert capsys.readouterr() == ("", "")


def test_context_hook_prints_summary(fake, isolated_env, capsys, monkeypatch):
    (isolated_env / "work" / ".openproject.yaml").write_text("project: demo\n", encoding="utf-8")
    monkeypatch.setenv("OP_URL", "https://op.test")
    monkeypatch.setenv("OP_TOKEN", "t")
    monkeypatch.setattr("opsync.cli.Client", lambda *a, **kw: context_fake(fake))
    assert main(["context", "--hook"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OpenProject: Демо (demo), вы — Аня")
    assert "В работе:\n  #2 Идёт" in out and "На проверке:" in out
