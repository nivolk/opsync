import datetime as dt

import pytest

from opsync.plan import PlanError, load
from opsync.schedule import Calendar, Window, schedule

D = dt.date
# пн 05.10.2026 … пт 16.10.2026; 04.11 — праздник в отдельном тесте
CAL = Calendar(hours_per_day=8)
SPRINT = Window("Спринт 1", D(2026, 10, 6), D(2026, 10, 16))


def plan_of(tmp_path, items: str):
    path = tmp_path / "p.yaml"
    path.write_text("defaults: {project: demo, assignee: me, sprint: Спринт 1}\nitems:\n" + items,
                    encoding="utf-8")
    return load(path)


def run(tmp_path, items: str, cal=CAL, window=SPRINT):
    return schedule(plan_of(tmp_path, items), cal,
                    lambda n: window if n.fields.get("sprint") == window.name else None)


def dates(result, key):
    s = result.slots[key]
    return s.start, s.due


def test_estimate_spread_over_working_days(tmp_path):
    r = run(tmp_path, "  - {key: a, subject: A, estimate: 20h}\n")
    assert dates(r, "a") == (D(2026, 10, 6), D(2026, 10, 8))     # 8 + 8 + 4
    assert r.planned() == {"a": (D(2026, 10, 6), D(2026, 10, 8))}
    assert r.warnings == []


def test_same_assignee_tasks_do_not_overlap_and_fill_gaps(tmp_path):
    r = run(tmp_path, """
  - {key: a, subject: A, estimate: 12h}
  - {key: b, subject: B, estimate: 2h}
  - {key: c, subject: C, estimate: 8h}
""")
    assert dates(r, "a") == (D(2026, 10, 6), D(2026, 10, 7))     # 8 + 4
    assert dates(r, "b") == (D(2026, 10, 7), D(2026, 10, 7))     # остаток 07.10
    assert dates(r, "c") == (D(2026, 10, 7), D(2026, 10, 8))     # 2 ч 07.10 + 6 ч 08.10


def test_follows_starts_next_working_day_after_predecessor(tmp_path):
    r = run(tmp_path, """
  - {key: b, subject: B, estimate: 4h, follows: [a], assignee: other}
  - {key: a, subject: A, estimate: 8h, start: 2026-10-09}
""")
    assert dates(r, "a") == (D(2026, 10, 9), D(2026, 10, 9))     # пятница
    assert dates(r, "b") == (D(2026, 10, 12), D(2026, 10, 12))   # понедельник


def test_follows_summary_waits_for_all_children(tmp_path):
    r = run(tmp_path, """
  - key: epic
    subject: E
    children:
      - {key: e1, subject: E1, estimate: 8h, assignee: x}
      - {key: e2, subject: E2, estimate: 16h, assignee: y}
  - {key: after, subject: After, estimate: 1h, follows: [epic]}
""")
    assert dates(r, "after") == (D(2026, 10, 8), D(2026, 10, 8))


def test_pinned_task_reserves_time(tmp_path):
    r = run(tmp_path, """
  - {key: fixed, subject: F, estimate: 16h, start: 2026-10-06, due: 2026-10-07}
  - {key: a, subject: A, estimate: 4h}
""")
    assert r.slots["fixed"].kind == "закреплено"
    assert dates(r, "a") == (D(2026, 10, 8), D(2026, 10, 8))
    assert "fixed" not in r.planned()


def test_holidays_and_weekend_skipped(tmp_path):
    cal = Calendar(holidays=frozenset({D(2026, 11, 4)}))
    window = Window("Спринт 3", D(2026, 11, 2), D(2026, 11, 13))
    r = run(tmp_path, "  - {key: a, subject: A, estimate: 24h, sprint: Спринт 3}\n",
            cal=cal, window=window)
    assert dates(r, "a") == (D(2026, 11, 2), D(2026, 11, 5))     # 02, 03, 05 — без 04.11


def test_overflow_out_of_sprint_warned(tmp_path):
    r = run(tmp_path, "  - {key: a, subject: A, estimate: 10d}\n")
    assert dates(r, "a") == (D(2026, 10, 6), D(2026, 10, 19))
    assert r.warnings == ["a: 06.10–19.10 выходит за «Спринт 1» 06.10–16.10"]


def test_overload_of_pinned_tasks_warned_once(tmp_path):
    r = run(tmp_path, """
  - {key: a, subject: A, estimate: 16h, start: 2026-10-06, due: 2026-10-07}
  - {key: b, subject: B, estimate: 16h, start: 2026-10-06, due: 2026-10-07}
""")
    assert r.warnings == ["перегрузка me: 2 дн. с 06.10 по 07.10, до 16.0 ч в день при 8 — "
                          "это закреплённые даты, планировщик их не двигает"]


def test_milestone_not_after_predecessor_warned(tmp_path):
    r = run(tmp_path, """
  - {key: a, subject: A, estimate: 8h}
  - {key: ms, subject: M, date: 2026-10-06, follows: [a]}
""")
    assert "ms: веха 06.10 не позже предшественников (они заканчиваются 06.10)" in r.warnings


def test_backlog_and_missing_estimate(tmp_path):
    r = run(tmp_path, """
  - {key: backlog, subject: B, sprint: null}
  - {key: noest, subject: N}
""")
    assert r.slots["backlog"].kind == "бэклог"
    assert r.slots["noest"].kind == "без дат"
    assert r.warnings == ["noest: нет оценки (estimate) — даты не рассчитаны"]


def test_days_without_estimate(tmp_path):
    r = run(tmp_path, "  - {key: a, subject: A, days: 3, start: 2026-10-08}\n")
    assert dates(r, "a") == (D(2026, 10, 8), D(2026, 10, 12))


def test_cycle_rejected(tmp_path):
    with pytest.raises(PlanError, match="цикл"):
        run(tmp_path, """
  - {key: a, subject: A, estimate: 1h, follows: [b]}
  - {key: b, subject: B, estimate: 1h, follows: [a]}
""")


def test_deterministic(tmp_path):
    items = "  - {key: a, subject: A, estimate: 20h}\n  - {key: b, subject: B, estimate: 4h}\n"
    assert run(tmp_path, items).planned() == run(tmp_path, items).planned()


def test_closed_task_keeps_server_dates_and_frees_time(tmp_path):
    """Регрессия: планировщик двигал даты закрытой задачи, и apply переписал бы их."""
    r = schedule(plan_of(tmp_path, """
  - {key: done, subject: D, estimate: 8h}
  - {key: a, subject: A, estimate: 8h}
  - {key: b, subject: B, estimate: 2h, follows: [done]}
"""), CAL, lambda n: SPRINT, closed={"done": (D(2026, 10, 13), D(2026, 10, 13))})
    assert r.slots["done"].kind == "закрыта"
    assert dates(r, "done") == (D(2026, 10, 13), D(2026, 10, 13))
    assert "done" not in r.planned()
    assert dates(r, "a") == (D(2026, 10, 6), D(2026, 10, 6))     # время закрытой не занято
    assert dates(r, "b") == (D(2026, 10, 14), D(2026, 10, 14))   # после её фактического конца


LANES = "calendar:\n  lanes: {Бэкенд: бэкенд, Фронтенд: фронтенд}\n  capacity: {бэкенд: 12}\n"


def lanes_plan(tmp_path, items: str):
    path = tmp_path / "p.yaml"
    path.write_text("defaults: {project: demo, assignee: me, sprint: Спринт 1}\n" + LANES
                    + "items:\n" + items, encoding="utf-8")
    return schedule(load(path), CAL, lambda n: SPRINT)


def test_lanes_run_in_parallel_for_one_assignee(tmp_path):
    """Один исполнитель, два агента: фронт и бэк не делят время друг с другом."""
    r = lanes_plan(tmp_path, """
  - {key: be, subject: B, estimate: 12h, category: Бэкенд}
  - {key: fe, subject: F, estimate: 8h, category: Фронтенд}
  - {key: org, subject: O, estimate: 8h, category: Организационное}
""")
    assert dates(r, "be") == (D(2026, 10, 6), D(2026, 10, 6))   # 12 ч в день у бэкенда
    assert dates(r, "fe") == (D(2026, 10, 6), D(2026, 10, 6))   # параллельно
    assert dates(r, "org") == (D(2026, 10, 6), D(2026, 10, 6))  # общая дорожка — своя
    assert set(r.load) == {"me/бэкенд", "me/фронтенд", "me"}


def test_lane_capacity_in_overload_warning(tmp_path):
    r = lanes_plan(tmp_path, """
  - {key: a, subject: A, estimate: 10h, category: Бэкенд, start: 2026-10-06, due: 2026-10-06}
  - {key: b, subject: B, estimate: 4h, category: Бэкенд, start: 2026-10-06, due: 2026-10-06}
""")
    assert r.warnings == ["перегрузка me/бэкенд: 1 дн. с 06.10 по 06.10, до 14.0 ч в день при "
                          "12 — это закреплённые даты, планировщик их не двигает"]


@pytest.mark.parametrize("text, error", [
    ("calendar: {lanes: [Бэкенд]}\n", "lanes"),
    ("calendar: {capacity: {бэкенд: 0}}\n", "больше 0"),
])
def test_lanes_format_errors(tmp_path, text, error):
    path = tmp_path / "p.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PlanError, match=error):
        load(path)
