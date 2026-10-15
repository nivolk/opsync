import pytest

from opsync.cli import main
from opsync.config import ProjectConfig
from opsync.tasks import Tasks, comment_warnings

from conftest import STATUSES, wp

NEW = STATUSES[0]


def test_plain_comment_has_no_warnings():
    text = ("Администратор теперь может удалить вложение у заявки в любом статусе. "
            "Проверить: открыть закрытую заявку → вложения → удалить. Коммит 2af79e8.")
    assert comment_warnings(text) == []


@pytest.mark.parametrize("text, hint", [
    ("x" * 801, "длинный комментарий"),
    ("Обновлены src/a.py, src/b.py, UserService.cs и UserTests.cs", "перечислено 4 файлов"),
    ("Сделал с помощью Claude", "упомянут инструмент"),
    ("\n".join(f"- пункт {i}" for i in range(7)), "много заголовков"),
])
def test_machine_like_comment_warned(text, hint):
    assert any(hint in w for w in comment_warnings(text))


@pytest.mark.parametrize("text", [
    "token: abcdef123456",
    "пароль = Secr3t!pass",
    "ключ ghp_abcdefghijklmnopqrstuvwxyz0123",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
])
def test_secret_is_not_written(fake, text):
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    with pytest.raises(LookupError, match="похоже на секрет"):
        Tasks(fake, ProjectConfig()).comment(5, text)
    assert fake.calls == []


def test_commit_hash_is_not_a_secret():
    assert comment_warnings("Коммит 2af79e8c1b0d4e5f6a7b8c9d0e1f2a3b4c5d6e7f.") == []


def test_cli_prints_warning_after_write(fake, monkeypatch, isolated_env, capsys):
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    fake.objects["/work_packages/5"] = wp(5, "Задача", NEW)
    assert main(["comment", "5", "Сделано в Cursor"]) == 0
    assert len(fake.writes()) == 1
    assert "Замечание к комментарию #5: упомянут инструмент" in capsys.readouterr().err
