import io
import json
import subprocess

import pytest

from opsync import hooks, rules, setup
from opsync.cli import main

from conftest import STATUSES, wp

NEW, IN_PROGRESS, IN_REVIEW, _ = STATUSES


# ---------------------------------------------------------------- установка
def test_hooks_install_keeps_foreign_entries_and_is_idempotent(tmp_path):
    settings = setup.claude_settings(tmp_path)
    settings.parent.mkdir()
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint.sh"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [foreign]}}))
    assert setup.hooks_state(tmp_path) == "нет"
    setup.set_hooks(tmp_path, True)
    setup.set_hooks(tmp_path, True)
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    assert data["hooks"]["PostToolUse"][0] == foreign
    assert len(data["hooks"]["PostToolUse"]) == 2 and len(data["hooks"]["SessionStart"]) == 1
    assert setup.hooks_state(tmp_path) == "актуальны"
    assert settings.with_suffix(".json.bak").is_file()
    setup.set_hooks(tmp_path, False)
    assert json.loads(settings.read_text()) == {"model": "x",
                                                "hooks": {"PostToolUse": [foreign]}}


def test_hooks_outdated_when_python_moved(tmp_path, monkeypatch):
    setup.set_hooks(tmp_path, True)
    monkeypatch.setattr("sys.executable", "/elsewhere/python")
    assert setup.hooks_state(tmp_path) == "устарели"


def test_broken_settings_not_overwritten(tmp_path):
    settings = setup.claude_settings(tmp_path)
    settings.parent.mkdir()
    settings.write_text("{oops")
    with pytest.raises(LookupError, match="не JSON"):
        setup.set_hooks(tmp_path, True)
    assert settings.read_text() == "{oops"


# ------------------------------------------------------------- хук коммита
def run_hook(monkeypatch, capsys, payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hooks.main(["commit"]) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


@pytest.fixture
def repo(fake, isolated_env, monkeypatch):
    work = isolated_env / "work"
    (work / ".openproject.yaml").write_text("project: demo\n", encoding="utf-8")
    monkeypatch.setenv("OP_URL", "https://op.test")
    monkeypatch.setenv("OP_TOKEN", "t")
    monkeypatch.setattr("opsync.client.Client.__new__", lambda cls, *a, **kw: fake)
    monkeypatch.setattr("opsync.client.Client.__init__", lambda self, *a, **kw: None)
    fake.objects["/projects/demo"] = {"id": 3}
    env = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
           "GIT_COMMITTER_EMAIL": "a@a", "HOME": str(work), "PATH": "/usr/bin:/bin"}

    def commit(message):
        subprocess.run(["git", "init", "-q"], cwd=work, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=work,
                       check=True, env=env)
    return {"cwd": str(work), "commit": commit}


def test_not_a_commit_is_ignored(monkeypatch, capsys, repo, fake):
    payload = {"cwd": repo["cwd"], "tool_input": {"command": "git status"}}
    assert run_hook(monkeypatch, capsys, payload) == ""


def test_commit_for_task_in_progress_suggests_review(monkeypatch, capsys, repo, fake):
    repo["commit"]("feat: удаление вложений OP#5")
    fake.objects["/work_packages/5"] = wp(5, "Удаление вложений", IN_PROGRESS)
    hint = run_hook(monkeypatch, capsys, {"cwd": repo["cwd"],
                                          "tool_input": {"command": "git commit -m x"}})
    assert "#5 «Удаление вложений» в работе" in hint and "opsync review 5" in hint
    assert fake.calls == []                                   # сам ничего не пишет


def test_commit_without_ref_lists_tasks_in_progress(monkeypatch, capsys, repo, fake):
    repo["commit"]("fix: опечатка")
    fake.collections["/projects/3/work_packages"] = [wp(5, "Удаление вложений", IN_PROGRESS)]
    hint = run_hook(monkeypatch, capsys, {"cwd": repo["cwd"],
                                          "tool_input": {"command": "git -C . commit -am x"}})
    assert "без ссылки на задачу" in hint and "#5 Удаление вложений" in hint


def test_hook_silent_on_errors(monkeypatch, capsys, repo, fake):
    repo["commit"]("feat: OP#9")                              # задачи 9 нет — 404
    assert run_hook(monkeypatch, capsys, {"cwd": repo["cwd"],
                                          "tool_input": {"command": "git commit"}}) == ""


# ------------------------------------------------------------------ правила
def test_rules_block_added_updated_removed(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Проект\n\nСвои правила.\n", encoding="utf-8")
    assert rules.write(path) == "добавлен"
    assert rules.write(path) == "актуален"
    assert rules.write(path, tracker="docs/TASKS.md") == "обновлён"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Проект\n\nСвои правила.\n\n<!-- opsync:begin -->")
    assert text.count("opsync:begin") == 1 and "docs/TASKS.md" in text
    assert rules.remove(path)
    assert path.read_text(encoding="utf-8") == "# Проект\n\nСвои правила.\n"


def test_rules_targets(tmp_path):
    assert rules.targets(tmp_path) == [tmp_path / "AGENTS.md"]
    (tmp_path / "CLAUDE.md").write_text("x")
    assert rules.targets(tmp_path) == [tmp_path / "CLAUDE.md"]


def test_init_writes_rules(fake, isolated_env, monkeypatch):
    monkeypatch.setattr("opsync.cli.Client.from_env", classmethod(lambda cls: fake))
    fake.objects["/projects/demo"] = {"id": 3}
    assert main(["init", "--project", "demo", "--tracker", "docs/TASKS.md"]) == 0
    text = (isolated_env / "work" / "AGENTS.md").read_text(encoding="utf-8")
    assert "opsync context" in text and "(OP #N)" in text
    assert main(["init", "--project", "demo", "--force", "--no-rules"]) == 0


def test_setup_refreshes_but_does_not_add_hooks_on_upgrade(isolated_env, monkeypatch, capsys):
    home = isolated_env / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    (setup.user_config_dir()).mkdir(parents=True)
    (setup.user_config_dir() / ".env").write_text("OP_URL=x\nOP_TOKEN=y\n")
    setup.run(home=home, new_hooks=False)
    assert setup.hooks_state(home) == "нет"
    assert "opsync hooks on" in capsys.readouterr().out
    setup.run(home=home)
    assert setup.hooks_state(home) == "актуальны"
