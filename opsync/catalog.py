"""Справочники экземпляра: имена → ссылки API. Всё, что план называет словами."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .client import ApiError, Client
from .schedule import Calendar


@dataclass
class Catalog:
    client: Client
    me: dict = field(default_factory=dict)
    projects: dict[str, dict] = field(default_factory=dict)  # identifier и name → project
    types: dict[str, dict] = field(default_factory=dict)
    statuses: dict[str, dict] = field(default_factory=dict)
    priorities: dict[str, dict] = field(default_factory=dict)
    _users: dict[str, dict] | None = None
    _roles: dict[str, dict] | None = None
    _categories: dict[int, dict[str, dict]] = field(default_factory=dict)
    _sprints: dict[int, dict[str, dict]] = field(default_factory=dict)
    _versions: dict[int, dict[str, dict]] = field(default_factory=dict)
    _members: dict[int, dict[str, dict]] = field(default_factory=dict)

    @classmethod
    def load(cls, client: Client) -> "Catalog":
        cat = cls(client)
        cat.me = client.get("/users/me")
        cat.reload_projects()
        cat.types = {t["name"]: t for t in client.collection("/types")}
        cat.statuses = {s["name"]: s for s in client.collection("/statuses")}
        cat.priorities = {p["name"]: p for p in client.collection("/priorities")}
        return cat

    def reload_projects(self) -> None:
        self.projects = {}
        for p in self.client.collection("/projects"):
            self.projects[p["identifier"]] = p
            self.projects[p["name"]] = p

    # --- проекты ---------------------------------------------------------
    def project(self, ref: str) -> dict:
        try:
            return self.projects[ref]
        except KeyError:
            raise LookupError(f"проект не найден: {ref!r}") from None

    # --- пользователи ----------------------------------------------------
    def users(self) -> dict[str, dict]:
        if self._users is None:
            self._users = {}
            try:
                for u in self.client.collection("/users"):
                    self._index_user(u)
            except ApiError:  # не админ — видим только участников проектов
                pass
        return self._users

    def _index_user(self, u: dict) -> None:
        assert self._users is not None
        for key in (u.get("name"), u.get("login"), u.get("email")):
            if key:
                self._users[key.lower()] = u

    def user(self, ref: str) -> dict:
        if ref == "me":
            return self.me
        u = self.users().get(ref.lower())
        if not u:
            raise LookupError(f"пользователь не найден: {ref!r}")
        return u

    def roles(self) -> dict[str, dict]:
        if self._roles is None:
            self._roles = {r["name"]: r for r in self.client.collection("/roles")}
        return self._roles

    def role(self, name: str) -> dict:
        try:
            return self.roles()[name]
        except KeyError:
            raise LookupError(f"роль не найдена: {name!r}") from None

    # --- справочники проекта ---------------------------------------------
    def categories(self, project_id: int) -> dict[str, dict]:
        if project_id not in self._categories:
            self._categories[project_id] = {
                c["name"]: c for c in self.client.collection(f"/projects/{project_id}/categories")
            }
        return self._categories[project_id]

    def sprints(self, project_id: int) -> dict[str, dict]:
        if project_id not in self._sprints:
            try:
                items = list(self.client.collection(f"/projects/{project_id}/sprints"))
            except ApiError:
                items = []
            self._sprints[project_id] = {s["name"]: s for s in items}
        return self._sprints[project_id]

    def versions(self, project_id: int) -> dict[str, dict]:
        if project_id not in self._versions:
            self._versions[project_id] = {
                v["name"]: v for v in self.client.collection(f"/projects/{project_id}/versions")
            }
        return self._versions[project_id]

    def members(self, project_id: int) -> dict[str, dict]:
        if project_id not in self._members:
            out: dict[str, dict] = {}
            flt = [{"project": {"operator": "=", "values": [str(project_id)]}}]
            for m in self.client.collection("/memberships", filters=flt):
                principal = m["_links"]["principal"]
                out[principal["title"].lower()] = principal
            self._members[project_id] = out
        return self._members[project_id]

    def forget(self, project_id: int) -> None:
        for cache in (self._categories, self._sprints, self._versions, self._members):
            cache.pop(project_id, None)


def instance_calendar(client: Client, start: dt.date, end: dt.date, hours_per_day: float = 8,
                      extra: set[dt.date] | frozenset[dt.date] = frozenset()) -> Calendar:
    """Рабочая неделя и нерабочие дни экземпляра за период (+ дополнительные дни)."""
    weekend = frozenset(d["day"] for d in client.get("/days/week")["_embedded"]["elements"]
                        if not d["working"])
    flt = [{"date": {"operator": "<>d", "values": [start.isoformat(), end.isoformat()]}}]
    holidays = {dt.date.fromisoformat(d["date"])
                for d in client.collection("/days/non_working", filters=flt)}
    return Calendar(hours_per_day, weekend or frozenset({6, 7}), frozenset(holidays | set(extra)))
