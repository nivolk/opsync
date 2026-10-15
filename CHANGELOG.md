# Изменения

## 0.3.1

- `opsync rules` не дописывает блок в CLAUDE.md, который только отсылает к AGENTS.md
  (`@AGENTS.md` или ссылкой): правило остаётся в одном месте.

## 0.3.0

Всё, что делается с агентом, видно команде в OpenProject.

- `opsync context` — с чего начать сессию: мои задачи по этапам с последним комментарием,
  что дальше по спринту, риски.
- Хуки Claude Code (`opsync setup`, `opsync hooks on|off`): сводка в начале сессии,
  подсказка по задаче после `git commit`.
- `opsync init` дописывает правило ведения задач в AGENTS.md / CLAUDE.md (`opsync rules`).
- `opsync new` и `describe` — завести найденную по ходу задачу (сразу и в план) и уточнить
  описание.
- `opsync note` — ход работы без смены статуса; `block`/`unblock` — необязательный этап
  `blocked` («В ожидании»).
- `opsync report` — состояние проекта: статус и пояснение на обзоре проекта, итог новостью.
- `opsync show` — связи, подзадачи и лента; `opsync search` — по теме, описанию и
  комментариям.
- `OP#N` в коммите: `review` подставляет хеш, интеграции GitLab/GitHub привязывают коммит.
- Комментарии для людей: правила и примеры в скилле; похожее на секрет не записывается,
  о «машинном» комментарии — замечание.
- MCP: `op_context`, `op_search`, `op_new`, `op_describe`, `op_note`, `op_report`, этап
  `blocked` в `op_stage`.

Переход с 0.2: `opsync upgrade` (обновит скиллы), `opsync hooks on` — хуки Claude Code,
в репозиториях — `opsync rules`; для `opsync block` добавьте в `.openproject.yaml` `statuses: blocked: …`
(`opsync doctor` подскажет статус).

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
