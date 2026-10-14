"""Точечная правка YAML-плана с сохранением комментариев и форматирования (ruamel.yaml).

Нужна командам, которые меняют то, чем владеет план (например, спринт задачи при переносе):
иначе следующий `apply` вернул бы всё как было.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Iterator

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

DELETE = object()                        # значение-маркер: убрать поле из элемента


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _items(nodes: list) -> Iterator[CommentedMap]:
    for item in nodes or []:
        yield item
        yield from _items(item.get("children") or [])


def _key(item: CommentedMap) -> str | None:
    return item.get("key") or (f"#{item['id']}" if item.get("id") else None)


def append_items(path: Path, items: list[dict[str, Any]]) -> None:
    """Дописать элементы в конец `items` (например, задачи, заведённые руками)."""
    y = _yaml()
    data = y.load(path.read_text(encoding="utf-8"))
    if data.get("items") is None:
        data["items"] = []
    for item in items:
        data["items"].append(CommentedMap(item))
    buf = io.StringIO()
    y.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


def edit_items(path: Path, changes: dict[str, dict[str, Any]]) -> list[str]:
    """key → {поле: значение | DELETE}. Возвращает ключи, которых в плане не нашлось."""
    y = _yaml()
    data = y.load(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for item in _items(data.get("items") or []):
        key = _key(item)
        if key not in changes:
            continue
        found.add(key)
        for name, value in changes[key].items():
            if value is DELETE:
                item.pop(name, None)
            elif name in item:
                item[name] = value
            else:
                # новое поле — сразу после subject (или key), чтобы элемент читался как раньше
                anchor = next((k for k in ("subject", "key", "id") if k in item), None)
                position = list(item).index(anchor) + 1 if anchor else len(item)
                item.insert(position, name, value)
    buf = io.StringIO()
    y.dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return sorted(set(changes) - found)
