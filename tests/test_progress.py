import subprocess

import pytest

from opsync import gitinfo
from opsync.cli import main
from opsync.config import detect_statuses

from conftest import STATUSES, status, wp

NEW, IN_PROGRESS, IN_REVIEW, _ = STATUSES
WAITING = status(14, "В ожидании")


@pytest.fixture
def server(fake, monkeypatch, isolated_env):
    fake.collections["/statuses"].append(WAITING)
    fake.objects["/work_packages/5"] = wp(5, "Задача", IN_PROGRESS)
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    return fake


def config(text: str = "project: demo\n") -> None:
    (gitinfo.Path.cwd() / ".openproject.yaml").write_text(text, encoding="utf-8")


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a",
                        "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a",
                        "HOME": str(gitinfo.Path.cwd()), "PATH": "/usr/bin:/bin"})


def test_blocked_stage_detected():
    assert detect_statuses(["Новое", "В ожидании"])["blocked"] == "В ожидании"
    assert detect_statuses(["New", "On hold"])["blocked"] == "On hold"


def test_block_without_stage_explains_and_writes_nothing(server, capsys):
    config()
    assert main(["block", "5", "-m", "Жду доступ к стенду"]) == 1
    assert "этап 'blocked' не настроен" in capsys.readouterr().err
    assert server.calls == []


def test_block_and_unblock(server, capsys):
    config("project: demo\nstatuses:\n  blocked: В ожидании\n")
    assert main(["block", "5", "-m", "Жду доступ к стенду от админа"]) == 0
    assert server.objects["/work_packages/5"]["_links"]["status"]["title"] == "В ожидании"
    assert main(["unblock", "5", "-m", "Доступ дали"]) == 0
    assert server.objects["/work_packages/5"]["_links"]["status"]["title"] == "В процессе"
    comments = [c[2]["comment"]["raw"] for c in server.writes("POST")]
    assert comments == ["Жду доступ к стенду от админа", "Доступ дали"]


def test_note_is_silent_by_default(server, capsys):
    config()
    assert main(["note", "5", "-m", "Сервер готов, дальше кнопка"]) == 0
    assert server.params == [{"notify": "false"}]
    assert not server.writes("PATCH")


def test_review_adds_tagged_commit(server):
    config()
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "feat: удаление вложений OP#5")
    assert main(["review", "5", "-m", "Можно удалять вложения."]) == 0
    sha = gitinfo.head().short
    assert server.writes("POST")[0][2]["comment"]["raw"] == f"Можно удалять вложения. Коммит {sha}."


def test_review_ignores_commit_of_other_task(server):
    config()
    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "fix: другое OP#6")
    assert main(["review", "5", "-m", "Готово."]) == 0
    assert server.writes("POST")[0][2]["comment"]["raw"] == "Готово."


def test_with_commit_outside_git(tmp_path):
    assert gitinfo.with_commit("Готово.", 5, tmp_path) == "Готово."
