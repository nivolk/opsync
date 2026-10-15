# OpenProject API v3: что использует opsync

Справка по запросам, которые opsync использует или будет использовать. Это не пересказ всей
документации. Проверено на OpenProject 17.8.0 Community (октябрь 2026).
Официальная документация: <https://www.openproject.org/docs/api/>, спецификация OpenAPI —
<https://www.openproject.org/docs/api/v3/spec.json> (её же отдаёт экземпляр:
`/api/v3/spec.json`).

## Общее

- **Доступ.** HTTP Basic: логин — буквально `apikey`, пароль — токен («Моя учётная запись →
  Токены доступа → API»). Права — как у владельца токена в интерфейсе.
- **Формат.** HAL+JSON: поля — свойства, ссылки на другие ресурсы — в `_links` (`href` и
  `title`), вложенные ресурсы — в `_embedded`. Запись ссылок: `{"_links": {"status":
  {"href": "/api/v3/statuses/7"}}}`. Без `Content-Type: application/json` сервер отвечает 406.
- **`lockVersion`.** Каждый PATCH задачи передаёт текущий `lockVersion`; устаревший — 409 Conflict.
  После любой правки он растёт. Правка соседей (даты, связи) тоже его меняет, поэтому перед
  PATCH брать свежую версию (`apply.update` так и делает).
- **Постраничность.** `pageSize` (до `/configuration.maximumAPIV3PageSize`, у нас 1000) и
  `offset` — **номер страницы с 1**, а не смещение. Конец — когда элементов меньше
  `pageSize` (`Client.collection`).
- **Ошибки.** Тело `{"_type": "Error", "errorIdentifier": "urn:…", "message": …}`; у 422
  несколько ошибок лежат в `_embedded.errors` (`ApiError` их разворачивает). 404 — нет
  ресурса или нет права его видеть, это не различить.
- **`notify`.** У записи задач и комментариев есть query-параметр `notify` (по умолчанию
  `true`): при `false` письма наблюдателям, автору и исполнителю не уходят.

## Экземпляр

| Запрос | Зачем |
|---|---|
| `GET /api/v3` | `coreVersion` (видна не всем), `instanceName` |
| `GET /api/v3/configuration` | `maximumAPIV3PageSize`, `hoursPerDay`, `availableFeatures` (Enterprise-функции; пусто — Community) |
| `GET /api/v3/users/me` | кто владелец токена (`doctor`) |
| `GET /days/week` | рабочие дни недели (`day` 1–7, `working`); `PATCH` — сменить (админ) |
| `GET /days/non_working?filters=[{"date":{"operator":"<>d","values":[от,до]}}]` | нерабочие дни экземпляра за период |
| `POST /days/non_working` | `{"_type": "NonWorkingDay", "date": "2026-11-04", "name": "…"}` — добавить (админ; общий календарь) |

## Задачи (work packages)

| Запрос | Зачем | Где в opsync |
|---|---|---|
| `GET /work_packages/{id}` | задача целиком | `Tasks.get` |
| `GET /projects/{id}/work_packages?filters=…` | задачи проекта | `Tasks.list`, `apply`, `export` |
| `POST /projects/{id}/work_packages` | создать | `apply.create`, `Tasks.new` |
| `POST /projects/{id}/work_packages/form` | проверить тело без создания (`_embedded.validationErrors`) | `apply.validate`, `new --dry-run` |
| `PATCH /work_packages/{id}` | изменить: только переданные поля + `lockVersion`. Поле `comment` в теле **молча игнорируется** | `set_status`, `apply.update` |
| `POST /work_packages/{id}/form` | схема правки: допустимые значения полей | `Tasks.allowed_statuses` |
| `POST /work_packages/{id}/activities[?notify=false]` | комментарий `{"comment": {"raw": "…"}}` (markdown) | `Tasks.comment` |
| `GET /work_packages/{id}/activities` | лента от старых к новым: `comment.raw`, `details[].raw` («Статус изменено с … на …»), `createdAt`, `version` (1 — создание), `_links.user` **без `title`** — имя берём `GET /users/{id}` | `Tasks.history` |
| `POST /time_entries` | списать время: `{"hours": "PT1H30M", "spentOn": "2026-10-08", "comment": {"raw": …}, "_links": {"entity": {"href": "/api/v3/work_packages/166"}, "activity": …}}`; `activity` необязателен (у нас по умолчанию «Управление») | `Tasks.log_time` |
| `POST /time_entries/form` | виды работ для задачи: `_embedded.schema.activity._embedded.allowedValues` | `Tasks._activity` |
| `GET/POST /work_packages/{id}/relations` | связи, `{"type": "follows", "_links": {"to": …}}`; читать с точки зрения задачи: если она в `to` — тип `reverseType` | `apply.ensure_relations`, `Tasks.relations` |
| `_links.children` задачи | подзадачи (`href`, `title`) | `show` |

**Смена статуса и workflow.** Допустимые переходы зависят от типа задачи и ролей
пользователя (workflow настраивается только в интерфейсе). Недопустимый переход даёт 422.
Список допустимых статусов отдаёт форма:

```http
POST /api/v3/work_packages/100/form
{"lockVersion": 5}
```

→ `_embedded.schema.status._embedded.allowedValues[].name` (у нас: Новое, В процессе,
В тестировании, закрыто, В ожидании, отклонено). Форма ничего не записывает. Пример отказа:
«В процессе» → «Протестирован» на типе «Задание» — 422 «нет правильного перехода».

**Лента склеивает записи.** Изменения одного автора за несколько минут сервер объединяет в
одну запись (journal aggregation): правка присоединяется к предыдущей записи, а комментарий —
только к записи без комментария. Отсюда порядок в opsync: комментарий, затем PATCH статуса —
тогда они в одной записи. Комментарий отдельно от смены статуса прилипнет к последней записи.

**Даты.** `startDate`/`dueDate` для задач, `date` для вех; `scheduleManually: true` — ручной
режим. OpenProject сам переводит задачу в автоматический режим, как только у неё появляется
предшественник (поэтому `apply.finalize_scheduling`). `estimatedTime` и `duration` — ISO 8601
(`PT6H`, `P3D`), 24 часа сервер может вернуть как `P1D`.

## Фильтры

`filters` — JSON-массив в query-строке, `sortBy` — тоже JSON:

```text
filters=[{"status":{"operator":"o","values":[]}},{"assignee":{"operator":"=","values":["me"]}}]
sortBy=[["id","asc"]]
```

| Фильтр | Операторы | Примечание |
|---|---|---|
| `status` | `o` открытые, `c` закрытые, `*` все, `=` по id | без фильтра по статусу сервер отдаёт только открытые |
| `assignee` | `=` с `me` или id | |
| `project` | `=` по id | для `/work_packages` без проекта |
| `sprint` | `=` по id спринта | есть на 17.8 (модуль Backlogs) |
| `search` | `**` со строкой | тема, описание **и комментарии** (`opsync search`) |
| `updatedAt` | `>t-` с числом дней; `<>d` — интервал дат, конец не включается | «сделано за неделю» в `report` |
| `parent`, `type`, `category`, `targetVersion`, `subject` (`~`) | | |

Полный список фильтров экземпляра: `GET /api/v3/queries/filter_instance_schemas` (у нас 48).

## Состояние проекта и новости

| Запрос | Зачем |
|---|---|
| `PATCH /projects/{id}` | `{"statusExplanation": {"format": "markdown", "raw": …}, "_links": {"status": {"href": "/api/v3/project_statuses/at_risk"}}}` — статус и пояснение на странице обзора проекта; нужно право «Изменить проект». В пояснении `#123` становится ссылкой на задачу |
| `GET /project_statuses/{id}` | `on_track`, `at_risk`, `off_track`, `not_started`, `finished`, `discontinued`; в русском интерфейсе `off_track` подписан «Приостановлен» — поэтому `report` сам его не ставит |
| `POST /news` | `{"title", "summary", "description": {"raw"}, "_links": {"project"}}` — новость; нужен модуль «Новости» в проекте и право «Управлять новостями» |

## Справочники

| Запрос | Что |
|---|---|
| `GET /statuses`, `/types`, `/priorities` | глобальные; имя → `_links.self.href` (`Catalog`) |
| `GET /projects`, `/projects/{id или identifier}` | проекты; identifier — из URL |
| `GET /projects/{id}/types` | типы, включённые в проекте |
| `GET /projects/{id}/categories`, `/versions` | категории, версии |
| `GET /projects/{id}/sprints` | спринты Backlogs: `name`, `startDate`, `finishDate`; ссылка на задаче — `_links.sprint` (`/api/v3/sprints/{id}`). **Только чтение**: с 17.3 спринты — отдельные объекты, создаются и правятся только в интерфейсе («Бэклоги → + Спринт») |
| `GET /memberships?filters=[{"project":…}]` | участники проекта |
| `GET /users` | только админу; иначе 403 (`Catalog.users` это переживает) |
| `POST /projects`, `POST /memberships` | создать подпроект и добавить участников (`apply.create_project`) |

Через API **не настраиваются**: workflow, включение типов и модулей в проекте, категории
(создание), спринты, нерабочие дни. `apply` выводит их списком для ручной настройки.

## Готовые инструменты (обзор на 08.10.2026)

| Инструмент | Что умеет | Почему не он |
|---|---|---|
| [Встроенный MCP-сервер OpenProject](https://www.openproject.org/docs/system-admin-guide/integrations/mcp-server/) (`/mcp`) | с 17.2 — чтение проектов, задач, пользователей; с [17.8](https://www.openproject.org/docs/release-notes/17-8-0/) — создание и правка задач, комментарии, связи | Enterprise add-on (Professional и выше), экспериментальный; на Community `/mcp` не работает |
| [`opf/openproject-cli`](https://github.com/opf/openproject-cli) (Go, официальный, v0.5.5) | создать, изменить, отфильтровать задачи, вложения, комментарии | нет плана, key → id, этапов и идемпотентности; процесс агента всё равно писать самим |
| Сторонние MCP для Community: [`openproject-mcp-server`](https://pypi.org/project/openproject-mcp-server/) (PyPI, 72 инструмента, разрушающие — с `confirm=true`), [`dev-in-black/openproject-mcp`](https://glama.ai/mcp/servers/@dev-in-black/openproject-mcp), [`alex13slem/openproject-codex-plugin`](https://glama.ai/mcp/servers/alex13slem/openproject-codex-plugin) | весь API как инструменты | инструменты без процесса; годятся, если понадобится клиент без shell |

Вывод: ценность opsync — план, key, этапы, идемпотентность и процесс в скилле, а не
обёртка над API.
