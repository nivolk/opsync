# Команды opsync

Обычно команды вызывает агент. Здесь — для работы руками и для отладки.
`ref` — номер задачи (`104`, `#104`) или key из плана.

## Настройка

| Команда | Что делает |
|---|---|
| `opsync setup` | адрес и токен OpenProject, подключение агентов; повторный запуск безопасен |
| `opsync init [--project ID]` | подключить текущий репозиторий к проекту (без `--project` — выбор из списка) |
| `opsync doctor` | что настроено, что нет и что сделать |
| `opsync upgrade` | обновить opsync и скиллы у агентов |

## Задачи

| Команда | Что делает |
|---|---|
| `opsync board [--sprint S]` | спринт (по умолчанию текущий): колонки, часы, просрочки, перегрузка |
| `opsync list [--sprint S] [--mine] [--all]` | задачи проекта, по умолчанию открытые |
| `opsync show <ref>` | задача целиком |
| `opsync start\|review\|done\|reopen <ref>... [-m текст] [--spent 2h]` | этап задачи; комментарий — одной записью со сменой статуса |
| `opsync status <ref> <статус> [-m текст]` | любой статус |
| `opsync comment <ref> <текст>` | комментарий |
| `opsync log <ref> <время> [-m текст] [--date D]` | списать время: `2h`, `0.5d`, `30m` |
| `opsync carry <спринт> [--to S] [--dry-run]` | незакрытые задачи — в следующий спринт |

У команд с комментарием есть `--no-notify` — без писем участникам; у `list`, `board`,
`show` — `--json`.

## План проекта

Формат — [opsync/skill/references/plan.md](../opsync/skill/references/plan.md).

| Команда | Что делает |
|---|---|
| `opsync apply [--dry-run]` | привести OpenProject к плану |
| `opsync pull [--dry-run] [--adopt]` | статусы и даты из OpenProject — в план |
| `opsync schedule` | даты по оценкам, связям и спринтам; загрузка; предупреждения |
| `opsync export [-o файл]` | задачи проекта в формате плана — отправная точка |
| `opsync inspect [проект]` | типы, статусы, категории, спринты, участники |

## `.openproject.yaml`

```yaml
project: my-project          # identifier из URL /projects/<identifier>
plan: .openproject/plan.yaml # необязательно
tracker: docs/TASKS.md       # необязательно: подсказка агенту, где локальные задачи
statuses:                    # этап → статус вашего OpenProject (init подбирает сам)
  start: В процессе
  review: В тестировании
  done: закрыто
  reopen: Новое
```

## Агенты

`opsync setup` кладёт скилл в `~/.claude/skills` (Claude Code) и `~/.agents/skills`
(Codex и Cursor), а Claude Desktop подключает как MCP-сервер (`opsync mcp`). Доступ хранится
в `~/.config/opsync/.env`; переменные окружения `OP_URL` и `OP_TOKEN` его перекрывают.
