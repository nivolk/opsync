import json

from opsync.config import state_path
from opsync.plan import load
from opsync.pull import pull

from conftest import STATUSES, wp

NEW, IN_PROGRESS, IN_REVIEW, CLOSED = STATUSES
PLAN = """# комментарий сохраняется
defaults: {project: demo, status: Новое}
items:
  - key: a
    subject: A
    start: 2026-10-06
    due: 2026-10-07
  - {key: b, subject: B, estimate: 4h}
  - {key: c, subject: C}
"""


def setup(fake, tmp_path, server: list[dict]):
    path = tmp_path / "demo.yaml"
    path.write_text(PLAN, encoding="utf-8")
    state = state_path(path, "demo")
    state.parent.mkdir()
    state.write_text(json.dumps({"a": 1, "b": 2, "c": 3}), encoding="utf-8")
    fake.objects["/projects/demo"] = {"id": 9}
    fake.collections["/work_packages"] = [w for w in server if w["id"] in (1, 2, 3)]
    fake.collections["/projects/9/work_packages"] = server
    return path


def test_pull_status_and_recalculated_dates(fake, tmp_path):
    path = setup(fake, tmp_path, [
        wp(1, "A", CLOSED, startDate="2026-10-06", dueDate="2026-10-08"),  # OP сдвинул срок
        wp(2, "B", IN_REVIEW, startDate="2026-10-09", dueDate="2026-10-09"),
        wp(3, "C", NEW),
    ])
    r = pull(fake, load(path))
    assert {k: {f: str(v) for f, v in c.items()} for k, c in r.changes.items()} == {
        "a": {"status": "закрыто", "due": "2026-10-08"},
        "b": {"status": "В тестировании"},          # даты планировщика в план не пишутся
    }
    plan = load(path)
    assert plan.nodes["a"].fields["status"] == "закрыто"
    assert plan.nodes["a"].fields["due"].isoformat() == "2026-10-08"
    assert "start" not in plan.nodes["b"].fields
    assert "# комментарий сохраняется" in path.read_text(encoding="utf-8")
    assert fake.calls == []                          # на сервер не пишет


def test_pull_dry_run_and_in_sync(fake, tmp_path):
    path = setup(fake, tmp_path, [wp(1, "A", NEW, startDate="2026-10-06",
                                     dueDate="2026-10-07"), wp(2, "B", IN_REVIEW), wp(3, "C", NEW)])
    before = path.read_text(encoding="utf-8")
    assert list(pull(fake, load(path), dry_run=True).changes) == ["b"]
    assert path.read_text(encoding="utf-8") == before


def test_pull_untracked_and_adopt(fake, tmp_path):
    from opsync.client import API
    path = setup(fake, tmp_path, [
        wp(1, "A", NEW, startDate="2026-10-06", dueDate="2026-10-07"), wp(2, "B", NEW),
        wp(3, "C", NEW),
        wp(7, "Руками", NEW, links={"parent": {"href": f"{API}/work_packages/1"},
                                     "project": {"title": "Демо"}}),
    ])
    r = pull(fake, load(path), dry_run=True)
    assert [w["id"] for w in r.untracked] == [7] and r.adopted == []
    r = pull(fake, load(path), adopt=True)
    assert r.adopted == [7]
    assert load(path).nodes["#7"].parent_key == "a"
