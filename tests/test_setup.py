import json
import os

import pytest

from opsync import setup
from opsync.config import load_credentials


@pytest.fixture
def home(isolated_env, monkeypatch):
    home = isolated_env / "home"
    home.mkdir()
    monkeypatch.setattr("opsync.setup.check_access", lambda url, token: "Аня")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return home


@pytest.mark.parametrize("raw, url", [
    ("op.example.com", "https://op.example.com"),
    ("https://op.example.com/", "https://op.example.com"),
    ("https://op.example.com/my/access_token", "https://op.example.com"),
    ("http://10.0.0.5:8080/api/v3", "http://10.0.0.5:8080"),
])
def test_normalize_url(raw, url):
    assert setup.normalize_url(raw) == url


def test_setup_saves_credentials_privately(home, capsys):
    assert setup.run("op.example.com", " tok ", home=home) == 0
    creds = load_credentials()
    assert (creds.url, creds.token) == ("https://op.example.com", "tok")
    path = setup.user_config_dir() / ".env"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert "Подключено: Аня" in capsys.readouterr().out


def test_setup_without_args_outside_terminal_explains(home):
    with pytest.raises(LookupError, match="--token-stdin"):
        setup.run(home=home)


def test_setup_keeps_existing_access(home, capsys):
    setup.run("op.example.com", "tok", home=home)
    assert setup.run(home=home) == 0                  # не терминал — оставить как есть
    assert "Оставлен как есть" in capsys.readouterr().out


def test_setup_installs_skill_for_found_agents(home):
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    setup.run("op.example.com", "tok", home=home)
    source = (setup.skill_source() / "SKILL.md").read_text(encoding="utf-8")
    for folder in (home / ".claude" / "skills", home / ".agents" / "skills"):
        assert (folder / "openproject-tasks" / "SKILL.md").read_text(encoding="utf-8") == source
    assert not setup.desktop_config(home).exists()      # Desktop не найден — не трогаем


def test_setup_updates_outdated_skill_and_skips_plugin(home, capsys):
    target = home / ".agents" / "skills" / "openproject-tasks"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("старая версия", encoding="utf-8")
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        '{"plugins": {"openproject@opsync": {}}}', encoding="utf-8")
    setup.run("op.example.com", "tok", home=home)
    assert setup.skill_state(target) == "актуален"
    assert not (home / ".claude" / "skills").exists()
    assert "подключён плагином" in capsys.readouterr().out


def test_setup_adds_mcp_to_desktop_config_with_backup(home):
    config = setup.desktop_config(home)
    config.parent.mkdir(parents=True)
    config.write_text('{"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}',
                      encoding="utf-8")
    setup.run("op.example.com", "tok", home=home)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["openproject"]["args"] == ["-m", "opsync", "mcp"]
    assert config.with_suffix(".json.bak").is_file()
    assert setup.mcp_state(config) == "актуален"
