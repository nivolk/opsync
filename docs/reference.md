# Команды opsync

Обычно команды вызывает агент. Здесь — для работы руками и для отладки.
`ref` — номер задачи (`104`, `#104`) или key из плана.

## Настройка

| Команда | Что делает |
|---|---|
| `opsync setup` | адрес и токен OpenProject, подключение агентов; повторный запуск безопасен |
| `opsync init [--project ID]` | подключить текущий репозиторий к проекту (без `--project` — выбор из списка) |
| `opsync doctor` | что настроено, что нет и что сделать |
| `opsync upgrade` | обновить opsync, скиллы и хуки у агентов |
| `opsync hooks [on\|off]` | хуки Claude Code: сводка задач в начале сессии, подсказка после `git commit` |
| `opsync rules [--remove]` | блок с правилом ведения задач в AGENTS.md / CLAUDE.md (`init` добавляет его сам; `--no-rules` — нет) |

## Задачи

| Команда | Что делает |
|---|---|
| `opsync context` | с чего начать сессию: спринт, мои задачи по этапам с последним комментарием, что дальше, риски |
| `opsync board [--sprint S]` | спринт (по умолчанию текущий): колонки, часы, просрочки, перегрузка |
| `opsync list [--sprint S] [--mine] [--all]` | задачи проекта, по умолчанию открытые |
| `opsync search <текст> [--all]` | поиск по теме, описанию и комментариям |
| `opsync show <ref> [--history N]` | задача целиком: связи, подзадачи, последние записи ленты |
| `opsync start\|review\|done\|reopen <ref>... [-m текст] [--spent 2h]` | этап задачи; комментарий — одной записью со сменой статуса; `review` дописывает коммит, помеченный `OP#<номер>` |
| `opsync block <ref>... -m текст`, `opsync unblock <ref>...` | задача ждёт чего-то извне (этап `blocked`) и снова в работе |
| `opsync note <ref> -m текст` | ход работы: комментарий без смены статуса и без писем (`--notify` — с письмами) |
| `opsync new <тема> -m описание [--type T] [--sprint S] [--parent ref] [--estimate 4h] [--key k] [--mine] [--dry-run]` | завести задачу; если у проекта есть план — дописать в него |
| `opsync describe <ref> -m текст [--replace]` | дополнить описание задачи |
| `opsync status <ref> <статус> [-m текст]` | любой статус |
| `opsync comment <ref> <текст>` | комментарий |
| `opsync log <ref> <время> [-m текст] [--date D]` | списать время: `2h`, `0.5d`, `30m` |
| `opsync carry <спринт> [--to S] [--dry-run]` | незакрытые задачи — в следующий спринт |
| `opsync report [--days 7] [--summary текст] [--health H] [--publish] [--news]` | состояние проекта для команды; `--publish` — статус и пояснение на обзоре проекта, `--news` — новостью |

Комментарий, похожий на секрет (токен, пароль, ключ), не записывается; про длинный или
«машинный» комментарий opsync пишет замечание в stderr.

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
  blocked: В ожидании        # необязательно: для opsync block
```

## Коммиты

`OP#123` в сообщении коммита связывает его с задачей: `review` подставит хеш в комментарий,
хук Claude Code подскажет, что сделать с задачей. Если администратор подключил интеграцию
OpenProject с [GitLab](https://www.openproject.org/docs/system-admin-guide/integrations/gitlab-integration/)
или [GitHub](https://www.openproject.org/docs/system-admin-guide/integrations/github-integration/),
коммиты и merge/pull request появятся во вкладке задачи.

## Агенты

`opsync setup` кладёт скилл в `~/.claude/skills` (Claude Code) и `~/.agents/skills`
(Codex и Cursor), а Claude Desktop подключает как MCP-сервер (`opsync mcp`). Для Claude Code
с вашего согласия ставит хуки в `~/.claude/settings.json`: в начале сессии агент получает
сводку `opsync context`, после `git commit` — подсказку по задаче. Хуки в OpenProject не
пишут, при ошибке молчат; убрать — `opsync hooks off`. Доступ хранится
в `~/.config/opsync/.env`; переменные окружения `OP_URL` и `OP_TOKEN` его перекрывают.
