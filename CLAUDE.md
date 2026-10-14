# CLAUDE.md

opsync — CLI, скилл и MCP-сервер для ведения задач OpenProject агентами. Пользователям —
README.md; устройство и решения — docs/DESIGN.md; запросы API — docs/API.md.

- Тесты без сети: `uv run pytest` (фейковый клиент в tests/conftest.py). Новая команда — с тестом.
- Скилл агента — opsync/skill/SKILL.md: setup копирует его агентам, MCP берёт из него
  разделы «Жизненный цикл задачи», «Комментарии», «Что требует подтверждения пользователя»
  (не переименовывать без правки opsync/mcp_server.py).
- Запись в OpenProject при проверках — только в проекте-песочнице.
- Коммиты: conventional commits на русском; сообщения CLI и комментарии в коде — на русском.
- Новая зависимость → у тех, кто ставил editable: `uv tool install -e . --reinstall`.
