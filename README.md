# opsync

Агент ведёт ваши задачи в OpenProject: показывает спринт, берёт задачи в работу, отправляет
на проверку, пишет комментарии и списывает время. Работает с Claude Code, Codex, Cursor и
Claude Desktop.

## Установка

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/):
`curl -LsSf https://astral.sh/uv/install.sh | sh`

```bash
uv tool install git+https://github.com/nivolk/opsync
opsync setup
```

`setup` спросит адрес OpenProject и токен и подключит агентов, которых найдёт на компьютере.
Потом перезапустите агента.

## Подключить проект

```bash
cd ~/src/мой-проект
opsync init
```

Выберите проект из списка. Появится `.openproject.yaml` — закоммитьте его, и коллегам
этот шаг делать не придётся.

## Работа

Говорите агенту обычными словами:

- «Что по спринту?»
- «Возьми задачу 104 в работу»
- «104 готова — на проверку»
- «Принято, закрывай 104»
- «Спиши на 104 два часа»

Агент сам меняет статусы и пишет комментарии. Закрывает задачи, меняет сроки и делает
массовые правки — только с вашего согласия.

## Обновление

```bash
opsync upgrade
```

---

Если что-то не так — `opsync doctor` покажет, что именно и что сделать.
Команды для терминала — [docs/reference.md](docs/reference.md).
План проекта в YAML (спринты, сроки, планирование дат) —
[opsync/skill/references/plan.md](opsync/skill/references/plan.md).
