"""Состояние сервера → план: статусы и даты, которые поменялись в OpenProject.

`apply` ведёт план → сервер, `pull` — обратно, но только то, чем владеют люди и сам
OpenProject: статус (его двигают `start/review/done` и доска) и закреплённые даты, если
OpenProject пересчитал их по связям. На сервер `pull` не пишет.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from .client import ApiError, Client, href_id
from .config import state_path
from .plan import Plan
from .planedit import append_items, edit_items

DATES = (("start", "startDate"), ("due", "dueDate"), ("date", "date"))


@dataclass
class Pull:
    changes: dict[str, dict[str, object]] = field(default_factory=dict)  # key → поле → новое
    before: dict[str, dict[str, object]] = field(default_factory=dict)   # key → поле → было
    untracked: list[dict] = field(default_factory=list)                   # задачи вне плана
    adopted: list[int] = field(default_factory=list)


def pull(client: Client, plan: Plan, dry_run: bool = False, adopt: bool = False) -> Pull:
    out = Pull()
    path = state_path(plan.path, plan.name)
    ids: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    keys = {wid: key for key, wid in ids.items() if key in plan.nodes}

    if keys:
        flt = [{"id": {"operator": "=", "values": [str(i) for i in keys]}},
               {"status": {"operator": "*", "values": []}}]
        for wp in client.collection("/work_packages", filters=flt):
            node = plan.nodes[keys[wp["id"]]]
            change, before = {}, {}
            status = wp["_links"]["status"]["title"]
            if (node.fields.get("status") or "") != status:
                change["status"], before["status"] = status, node.fields.get("status")
            for name, api in DATES:
                if name not in node.explicit:        # даты планировщика в плане не хранятся
                    continue
                server = dt.date.fromisoformat(wp[api]) if wp.get(api) else None
                if server and server != node.fields.get(name):
                    change[name], before[name] = server, node.fields.get(name)
            if change:
                out.changes[node.key], out.before[node.key] = change, before

    out.untracked = _untracked(client, plan, set(ids.values())
                               | {n.adopt_id for n in plan.ordered() if n.adopt_id})
    if adopt and out.untracked and not dry_run:
        by_id = {wid: key for key, wid in ids.items()}
        items = []
        for wp in out.untracked:
            item = {"id": wp["id"]}
            parent = href_id((wp["_links"].get("parent") or {}).get("href"))
            if parent in by_id:
                item["parent"] = by_id[parent]
            items.append(item)
        append_items(plan.path, items)
        out.adopted = [wp["id"] for wp in out.untracked]
    if out.changes and not dry_run:
        edit_items(plan.path, out.changes)
    return out


def _untracked(client: Client, plan: Plan, known: set[int]) -> list[dict]:
    """Задачи проектов плана, о которых план не знает (заведены руками в OpenProject)."""
    refs = {p.identifier for p in plan.projects}
    refs |= {f for n in plan.ordered() if (f := n.fields.get("project"))}
    pids: set[int] = set()
    for ref in refs:                           # проект бывает назван и по имени, и по id
        try:
            pids.add(client.get(f"/projects/{ref}")["id"])
        except ApiError as e:
            if e.status != 404:
                raise                          # 404 — проект ещё не создан apply
    found: dict[int, dict] = {}                # родитель отдаёт и задачи подпроектов
    for pid in sorted(pids):
        flt = [{"status": {"operator": "*", "values": []}},
               {"project": {"operator": "=", "values": [str(pid)]}}]
        for wp in client.collection(f"/projects/{pid}/work_packages", filters=flt):
            if wp["id"] not in known:
                found[wp["id"]] = wp
    return [found[i] for i in sorted(found)]
