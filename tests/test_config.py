from pathlib import Path

import pytest

from opsync.config import (DEFAULT_STATUSES, ProjectConfig, find_project_config,
                           load_credentials, state_path)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ доступ
def test_credentials_from_environment_first(isolated_env, monkeypatch):
    write(Path.cwd() / ".env", "OP_URL=https://local\nOP_TOKEN=local\n")
    monkeypatch.setenv("OP_URL", "https://env")
    monkeypatch.setenv("OP_TOKEN", "env")
    creds = load_credentials()
    assert (creds.url, creds.token, creds.source) == ("https://env", "env", "окружение")


def test_credentials_local_env_before_user_config(isolated_env):
    local = write(Path.cwd() / ".env", '# доступ\nOP_URL="https://local"\nOP_TOKEN=\'local\'\n')
    write(isolated_env / "xdg" / "opsync" / ".env", "OP_URL=https://user\nOP_TOKEN=user\n")
    creds = load_credentials()
    assert (creds.url, creds.token, creds.source) == ("https://local", "local", str(local))


def test_credentials_from_user_config(isolated_env):
    user = write(isolated_env / "xdg" / "opsync" / ".env", "OP_URL=https://user\nOP_TOKEN=user\n")
    write(Path.cwd() / ".env", "OP_URL=https://local\n")      # неполный — пропускается
    creds = load_credentials()
    assert (creds.url, creds.source) == ("https://user", str(user))


def test_credentials_missing(isolated_env):
    with pytest.raises(SystemExit):
        load_credentials()


# ------------------------------------------------------------ .openproject.yaml
def test_config_found_in_parent(isolated_env):
    root = isolated_env / "repo"
    cfg = write(root / ".openproject.yaml", "project: demo\nplan: plans/demo.yaml\n")
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    config = find_project_config(nested)
    assert config.path == cfg
    assert config.root == root
    assert config.project == "demo"
    assert config.plan == (root / "plans" / "demo.yaml").resolve()


def test_config_yml_extension(isolated_env):
    write(Path.cwd() / ".openproject.yml", "project: demo\n")
    assert find_project_config().project == "demo"


def test_config_absent(isolated_env):
    config = find_project_config()
    assert config == ProjectConfig()
    assert config.root == Path.cwd()
    assert config.statuses == DEFAULT_STATUSES


def test_config_plan_expands_home(isolated_env, monkeypatch):
    monkeypatch.setenv("HOME", str(isolated_env / "home"))
    write(Path.cwd() / ".openproject.yaml", "plan: ~/plans/demo.yaml\n")
    assert find_project_config().plan == isolated_env / "home" / "plans" / "demo.yaml"


def test_config_statuses_merge_with_defaults(isolated_env):
    write(Path.cwd() / ".openproject.yaml", "statuses:\n  review: На проверке\n")
    config = find_project_config()
    assert config.status_for("review") == "На проверке"
    assert config.status_for("start") == DEFAULT_STATUSES["start"]
    with pytest.raises(LookupError):
        config.status_for("deploy")


def test_config_unknown_field(isolated_env):
    write(Path.cwd() / ".openproject.yaml", "project: demo\nboard: 1\n")
    with pytest.raises(SystemExit):
        find_project_config()


# ------------------------------------------------------------ состояние плана
def test_state_path_next_to_plan(isolated_env, monkeypatch):
    plan = write(isolated_env / "plans" / "demo.yaml", "items: []\n")
    expected = isolated_env / "plans" / ".state" / "demo.json"
    assert state_path(plan, "demo") == expected
    monkeypatch.chdir(isolated_env / "plans")
    assert state_path(Path("demo.yaml"), "demo") == expected
