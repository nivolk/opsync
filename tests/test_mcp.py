import asyncio
import json

import pytest
from mcp.client import Client

from opsync.mcp_server import build, instructions

from conftest import STATUSES, wp

NEW, IN_PROGRESS, _, _ = STATUSES


@pytest.fixture
def server(fake, monkeypatch, isolated_env):
    monkeypatch.setattr("opsync.mcp_server.Client.from_env", classmethod(lambda cls: fake))
    fake.objects["/users/me"] = {"name": "Аня"}
    return build()


def call(server, name: str, args: dict):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args)
    result = asyncio.run(go())
    text = result.content[0].text if result.content else ""
    return result.is_error, text


def test_instructions_carry_process_rules_from_skill():
    text = instructions()
    for title in ("## Жизненный цикл задачи", "## Комментарии",
                  "## Что требует подтверждения пользователя"):
        assert title in text
    assert "op_stage" in text


def test_tools_listed(server):
    async def go():
        async with Client(server) as c:
            return [t.name for t in (await c.list_tools()).tools]
    assert set(asyncio.run(go())) >= {"op_board", "op_show", "op_stage", "op_comment",
                                      "op_log_time", "op_apply", "op_pull", "op_projects",
                                      "op_context", "op_search", "op_new", "op_describe"}


def test_stage_changes_status_with_comment(server, fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    error, text = call(server, "op_stage", {"ref": "#5", "stage": "start", "comment": "беру"})
    assert not error
    assert json.loads(text)["new"] == "В процессе"
    assert [c[1] for c in fake.writes()] == ["/work_packages/5/activities", "/work_packages/5"]


def test_errors_reach_client_as_text(server, fake):
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    fake.allowed[5] = ["В процессе"]
    error, text = call(server, "op_stage", {"ref": "5", "stage": "done"})
    assert error and "запрещён workflow" in text
    error, text = call(server, "op_stage", {"ref": "5", "stage": "deploy"})
    assert error and "допустимы start, review, done, reopen" in text
    error, text = call(server, "op_apply", {})
    assert error and "у проекта нет плана" in text
    assert fake.calls == []


def test_doctor(server):
    error, text = call(server, "op_doctor", {})
    assert not error and json.loads(text)["user"] == "Аня"
