import json

import pytest

from opsync.client import ApiError
from opsync.config import ProjectConfig, state_path
from opsync.plan import load
from opsync.tasks import Tasks

from conftest import STATUSES, wp

NEW, IN_PROGRESS, IN_REVIEW, _ = STATUSES


@pytest.fixture
def planned(tmp_path) -> ProjectConfig:
    """Конфиг с планом, который уже применялся: в состоянии есть key → id."""
    plan_path = tmp_path / "plans" / "demo.yaml"
    plan_path.parent.mkdir()
    plan_path.write_text("items:\n  - key: be-reset-token\n    subject: Сброс токена\n",
                         encoding="utf-8")
    state = state_path(plan_path, load(plan_path).name)
    state.parent.mkdir()
    state.write_text(json.dumps({"be-reset-token": 101}), encoding="utf-8")
    return ProjectConfig(path=tmp_path / ".openproject.yaml", plan=plan_path)


# ------------------------------------------------------------------ ссылки
@pytest.mark.parametrize("ref", [100, "100", "#100", " #100 "])
def test_resolve_id(fake, ref):
    assert Tasks(fake, ProjectConfig()).resolve(ref) == 100


def test_resolve_key(fake, planned):
    tasks = Tasks(fake, planned)
    assert tasks.resolve("be-reset-token") == 101
    assert tasks.key_of(101) == "be-reset-token"


def test_resolve_unknown_key(fake, planned):
    with pytest.raises(LookupError, match="fe-login"):
        Tasks(fake, planned).resolve("fe-login")


def test_resolve_key_without_plan(fake):
    with pytest.raises(LookupError, match="план не задан"):
        Tasks(fake, ProjectConfig()).resolve("be-reset-token")


# ------------------------------------------------------------- смена статуса
def test_set_status_same_status_no_patch(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    change = Tasks(fake, ProjectConfig()).set_status(5, "в процессе")
    assert not change.changed and not change.commented
    assert fake.calls == []


def test_set_status_same_status_still_comments(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    change = Tasks(fake, ProjectConfig()).set_status(5, "В процессе", comment="коммит abc")
    assert not change.changed and change.commented
    assert fake.calls == [("POST", "/work_packages/5/activities",
                           {"comment": {"raw": "коммит abc"}})]


def test_set_status_changes(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW, lock=3)
    change = Tasks(fake, ProjectConfig()).set_status(5, "В процессе")
    assert (change.old, change.new, change.changed) == ("Новое", "В процессе", True)
    assert fake.writes() == [("PATCH", "/work_packages/5", {
        "lockVersion": 3,
        "_links": {"status": {"href": IN_PROGRESS["_links"]["self"]["href"]}},
    })]


def test_set_status_twice_is_idempotent(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    tasks = Tasks(fake, ProjectConfig())
    tasks.set_status(5, "В процессе")
    assert not tasks.set_status(5, "В процессе").changed
    assert len(fake.writes("PATCH")) == 1


def test_set_status_unknown(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    with pytest.raises(LookupError, match="Есть: Новое"):
        Tasks(fake, ProjectConfig()).set_status(5, "Отложено")
    assert fake.calls == []


def test_stage_uses_configured_status(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    config = ProjectConfig(statuses={"review": "В тестировании"})
    change = Tasks(fake, config).stage(5, "review")
    assert change.new == IN_REVIEW["name"] and change.changed


# ------------------------------------------------- порядок записи и workflow
def test_comment_goes_before_status_change(fake):
    """OpenProject клеит смену статуса к предыдущей записи ленты — комментарий первым."""
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW, lock=3)
    fake.on_post["/work_packages/5/activities"] = lambda body: fake.objects[
        "/work_packages/5"].update(lockVersion=4)
    Tasks(fake, ProjectConfig()).set_status(5, "В процессе", comment="беру")
    (m1, p1, _), (m2, p2, body) = fake.calls
    assert (m1, p1) == ("POST", "/work_packages/5/activities")
    assert (m2, p2) == ("PATCH", "/work_packages/5")
    assert body["lockVersion"] == 4            # свежий, после комментария


def test_forbidden_transition_checked_before_comment(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    fake.allowed[5] = ["В процессе", "В тестировании", "Новое"]
    with pytest.raises(LookupError) as e:
        Tasks(fake, ProjectConfig()).set_status(5, "закрыто", comment="готово")
    assert str(e.value) == ("#5: переход «В процессе» → «закрыто» запрещён workflow. "
                            "Можно: Новое, В тестировании")
    assert fake.calls == []                     # ни комментария, ни PATCH


def test_patch_422_explains_allowed(fake):
    """Workflow мог поменяться между формой и PATCH — отказ всё равно понятный."""
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    fake.patch_error = ApiError("PATCH", "/work_packages/5", 422, {"message": "Статус недопустимо"})
    with pytest.raises(LookupError, match=r"запрещён workflow \(PATCH .*422.*\)\. Можно: "):
        Tasks(fake, ProjectConfig()).set_status(5, "В процессе")


def test_same_status_skips_form(fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    Tasks(fake, ProjectConfig()).set_status(5, "Новое", comment="заметка")
    assert fake.forms == []


@pytest.mark.parametrize("notify, params", [(True, None), (False, {"notify": "false"})])
def test_comment_notify(fake, notify, params):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    Tasks(fake, ProjectConfig()).stage(5, "start", comment="беру", notify=notify)
    assert fake.params == [params]
