"""Тонкий клиент OpenProject API v3: авторизация, ошибки, постраничное чтение."""

from __future__ import annotations

import json
from typing import Any, Iterator

import requests

from .config import load_credentials

API = "/api/v3"


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: Any):
        self.status = status
        self.body = body
        message = body.get("message") if isinstance(body, dict) else str(body)[:300]
        details = _error_details(body)
        super().__init__(f"{method} {path} → {status}: {message}{details}")


def _error_details(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    errors = body.get("_embedded", {}).get("errors") or []
    return "".join(f"\n  - {e.get('message')}" for e in errors)


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 60):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = ("apikey", token)
        self.session.headers["Content-Type"] = "application/json"

    @classmethod
    def from_env(cls) -> "Client":
        """Доступ — из окружения, `./.env` или `~/.config/opsync/.env` (см. config.py)."""
        creds = load_credentials()
        return cls(creds.url, creds.token)

    def web_url(self, wid: int) -> str:
        return f"{self.base}/work_packages/{wid}"

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith(API):
            path = API + path
        return self.base + path

    def request(self, method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
        resp = self.session.request(
            method, self._url(path), params=params,
            data=json.dumps(body) if body is not None else None, timeout=self.timeout,
        )
        if resp.status_code == 204 or not resp.content:
            if resp.ok:
                return None
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if not resp.ok:
            raise ApiError(method, path, resp.status_code, data)
        return data

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: _param(v) for k, v in params.items()})

    def post(self, path: str, body: Any, params: dict | None = None) -> Any:
        return self.request("POST", path, body, params=params)

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body)

    def collection(self, path: str, page_size: int = 200, **params: Any) -> Iterator[dict]:
        offset = 1
        while True:
            data = self.get(path, pageSize=page_size, offset=offset, **params)
            elements = data.get("_embedded", {}).get("elements", [])
            yield from elements
            if len(elements) < page_size or data.get("count", 0) < page_size:
                return
            offset += 1


def _param(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, (list, dict)) else value


def href_id(href: str | None) -> int | None:
    if not href:
        return None
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None
