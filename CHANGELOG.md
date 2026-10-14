# Изменения

## 0.2.0

- `opsync setup` — мастер первого запуска: доступ к OpenProject и подключение агентов
  (скилл для Claude Code, Codex, Cursor; MCP-сервер `opsync mcp` для Claude Desktop).
- `opsync upgrade` — обновить opsync и скиллы у агентов.
- `opsync init` — проект выбирается из списка, этапы подбираются по статусам экземпляра.
- `opsync pull` — статусы и даты из OpenProject в план.
- Спринты и календарь в плане, планировщик дат (`opsync schedule`), дорожки исполнителя.
- `opsync board`, `opsync carry`, `opsync log`, `--spent`, `--no-notify`.
- Комментарий и смена статуса — одной записью ленты; понятный отказ workflow.
- Ошибки подсказывают следующий шаг.

## 0.1.0

- Ведение задач (`start/review/done/reopen/status/comment/show/list`) и YAML-план (`apply`).
