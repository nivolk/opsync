"""Общее для тестов: фейковый клиент OpenProject и изоляция от настроек пользователя."""

from __future__ import annotations

import copy
from typing import Any, Iterator

import pytest

from opsync.client import API, ApiError


class FakeClient:
    """Подмена `opsync.client.Client` на уровне get/post/patch/collection — без сети.

    `objects` — ответы на GET по пути, `collections` — элементы коллекций (фильтры
    игнорируются), `on_post` — ответы на POST, `allowed` — workflow для `/form`,
    `patch_error` — ошибка следующего PATCH. Запись копится в `calls` как (метод, путь, тело).
    """

    def __init__(self) -> None:
        self.base = "https://op.test"
        self.objects: dict[str, Any] = {}
        self.collections: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.on_post: dict[str, Any] = {}       # путь → fn(тело) → ответ сервера
        self.params: list[dict | None] = []     # query-параметры каждого POST из calls
        self.allowed: dict[int, list[str]] = {} # id → статусы, разрешённые workflow (иначе все)
        self.forms: list[str] = []
        self.patch_error: ApiError | None = None

    def web_url(self, wid: int) -> str:
        return f"{self.base}/work_packages/{wid}"

    def get(self, path: str, **params: Any) -> Any:
        try:
            return copy.deepcopy(self.objects[path])
        except KeyError:
            raise ApiError("GET", path, 404, {"message": "не найдено"}) from None

    def collection(self, path: str, page_size: int = 200, **params: Any) -> Iterator[dict]:
        yield from copy.deepcopy(self.collections.get(path, []))

    def post(self, path: str, body: Any, params: dict | None = None) -> Any:
        if path.startswith("/work_packages/") and path.endswith("/form"):
            self.forms.append(path)              # форма ничего не пишет — не в calls
            wid = int(path.split("/")[2])
            names = self.allowed.get(wid, [s["name"] for s in self.collections["/statuses"]])
            allowed = [s for s in self.collections["/statuses"] if s["name"] in names]
            return {"_embedded": {"schema": {"status": {"_embedded": {"allowedValues": allowed}}}}}
        self.calls.append(("POST", path, body))
        self.params.append(params)
        handler = self.on_post.get(path)
        return handler(body) if handler else {}

    def patch(self, path: str, body: Any) -> Any:
        if self.patch_error:
            error, self.patch_error = self.patch_error, None
            raise error
        self.calls.append(("PATCH", path, body))
        current = self.objects.get(path, {})
        current = {**current, **{k: v for k, v in body.items() if k != "_links"}}
        links = copy.deepcopy(body.get("_links", {}))
        if "status" in links:            # сервер отвечает ссылкой с title — как при GET
            href = links["status"]["href"]
            links["status"]["title"] = next(
                s["name"] for s in self.collections["/statuses"]
                if s["_links"]["self"]["href"] == href)
        current["_links"] = {**current.get("_links", {}), **links}
        current["lockVersion"] = current.get("lockVersion", 0) + 1
        self.objects[path] = current
        return copy.deepcopy(current)

    def writes(self, method: str | None = None) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if method is None or c[0] == method]


def status(sid: int, name: str) -> dict:
    return {"id": sid, "name": name, "_links": {"self": {"href": f"{API}/statuses/{sid}"}}}


def wp(wid: int, subject: str, st: dict, lock: int = 1, **extra: Any) -> dict:
    return {
        "id": wid, "subject": subject, "lockVersion": lock,
        "description": {"raw": ""},
        "_links": {
            "self": {"href": f"{API}/work_packages/{wid}"},
            "status": {"href": st["_links"]["self"]["href"], "title": st["name"]},
            **extra.pop("links", {}),
        },
        **extra,
    }


STATUSES = [status(1, "Новое"), status(7, "В процессе"), status(10, "В тестировании"),
            status(12, "закрыто")]


@pytest.fixture
def fake() -> FakeClient:
    client = FakeClient()
    client.collections["/statuses"] = copy.deepcopy(STATUSES)
    return client


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Ни окружение, ни ~/.config пользователя, ни cwd репозитория в тест не протекают."""
    for name in ("OP_URL", "OP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path
