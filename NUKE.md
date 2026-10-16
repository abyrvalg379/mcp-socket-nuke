# MCP Socket для Nuke — архитектура и грабли

> Четвёртая ветка семейства mcp-socket (Blender :9876, Maya :7777, Houdini :9877, **Nuke :9897**).
> Протокол — общий mcp-socket / blender-mcp 1.6.x: JSON без framing-байта, обе стороны
> копят байты и дергают `json.loads`, пока документ не распарсится целиком.

## Статус

- **v0.1.0** (16.10.2026) — Nuke 17.1v2 (Python 3.11.11, PySide6 6.5.3).
- Headless-смоук движком: **20/20 PASS**. Живой GUI-тест: **смоук 5/5 + проба
  12/12** (маршалинг, undo-ходьба, скриншот проверен глазами, recurse_groups);
  полная тул-сеть проверена живьём 16.10 (все тула, кроме replay — тот из пробы).
- Установлено в `%USERPROFILE%\.nuke\` (инсталлер); ZCode-конфиг — запись `nuke`.

## Архитектура (как у всей семьи, два процесса)

```
ZCode ──stdio JSON-RPC──▶ nuke_mcp.py (системный Python 3.14, stdlib only)
              ──TCP 127.0.0.1:9897──▶ mcp_socket_nuke/server.py (внутри Nuke)
```

Порт 9897, занят → автоофсет +1..+10 (второй Nuke). Реестр инстансов
`%TEMP%\mcp_socket_nuke_instances\` (heartbeat — daemon-поток, 10 с, НЕ QTimer).
JSONL-сессии `%TEMP%\mcp_socket_nuke\sessions\` (гэп 10 с, хранить 30).

## Установка

```bash
python.exe install_nuke_mcp.py    # при ЗАКРЫТОМ Nuke
```

Копирует: `mcp_socket_nuke/` (пакет: `__init__/server/ui.py`), `nuke_mcp.py`,
маркер-блоки в `~/.nuke/init.py` (sys.path) и `~/.nuke/menu.py` (автостарт +
пункт меню «MCP Socket → Open Window»). Маркер-блоки переживают повторный запуск
и не трогают чужой контент. Смоук: `python tests\socket_smoke.py nuke` при живом Nuke.

## Тулы (11)

`ping_nuke`, `execute_nuke_code`, `undo_agent_session`, `get_scene_info`,
`get_hierarchy` (max_nodes, recurse_groups), `get_screenshot`, `get_console_log`,
`clear_console_log`, `list_instances`, `replay_last_session`, `get_session_log_path`.

FBX/Alembic-тулов НЕТ (v0.1) — композ не таскает геометрию мостом; если понадобится,
делать по образцу Maya-ветки.

## Undo — ходьба по меткам (паттерн Houdini, НЕ майя-чанки)

- Каждый `execute_nuke_code` → `nuke.Undo.begin("MCP Socket: execute_nuke_code")` … `end()`.
- Пустые группы (read-only сниппеты) Nuke **молча выбрасывает** — стек остаётся чистым.
- `undo_agent_session` крутит `nuke.Undo.undoDescribe(0)` (метка верхнего шага,
  «0 = most recent») и `nuke.Undo.undo()` пока метка начинается с «MCP Socket»,
  кап 200 шагов. Чужая метка → честная остановка, работа юзера не ест.
- В engine-режиме (`not nuke.GUI`) undo-стека нет — честный отказ `{undone: 0}`.
- **Foreign-записи на стеке в живой GUI-сессии — может быть работа юзера
  параллельно с агентом** («move node», «change X.knob»): ходок честно
  останавливается и ничего не трогает; вручную чужие записи не мотать. Если
  намотал — восстановление `nuke.redo()` пошагово (работает, пока после уно
  не было новых правок: новая правка чистит redo-стек). Ручные undo/redo
  сниппетами — только с `undo_chunk=false`: undo внутри открытой группы
  мутит стек артефактами.

## Грабли (проверено пробами на 17.1v2)

- **`nuke.executeInMainThread(fn, args=...)` существует, но «return immediately»**
  по докстрингу — результат НЕ ждёт. Маршалинг: closure шлёт ответ сама из главного
  потока, клиентский поток ждёт на `threading.Event` (кап 180 с) — сериализует
  команды независимо от семантики возврата. Maya-аналога
  `executeInMainThreadWithResult` в Nuke НЕТ.
- `executeInMainThread` в engine-режиме кидает `RuntimeError('Invalid function call
  in non-gui mode')` — поэтому dispatch ветка `not nuke.GUI → прямой вызов`.
- **`nuke.Undo("имя")` — TypeError**: конструктор без аргументов. Имя группы —
  `nuke.Undo.begin("имя")` или `nuke.Undo.name("имя")` внутри группы (setter,
  геттера НЕТ). Метки стека читаются только через `undoDescribe(n)`/`undoDescribeFully(n)`.
- **`nuke.mainWindow` НЕ существует** в 17.1 → главное окно = самый большой видимый
  top-level widget (`QApplication.topLevelWidgets()`).
- PySide2 в Nuke 17 нет, только **PySide6 6.5.3** (в коде fallback сохранён).
- `node.knobs()` возвращает **dict** (имя→knob), не список.
- `root.knob("format").value()` — объект `_nuke.Format` (`.name()/.width()/.height()/
  .pixelAspect()`); имя дефолтного формата может быть пустой строкой.
- Позиции нод свежесозданных без ручного драга = сентинел 1073741824 (2^30) — не баг.
- `import nuke` из бандленного `python.exe` стартует движок **без GUI-места лицензии**
  — headless-тесты гонять можно при открытом Nuke (`test_headless.py`).
- tasklist из Python читается только с `encoding="utf-8", errors="replace"`
  (кириллический вывод cmd).
- **Создание нод (17.1):** `nuke.createNode(type)` имени не принимает вообще
  (2-й аргумент = TCL-список кнобов; kwarg `name=` → TypeError) — только
  `n.setName('X')` после создания. Шорткат `nuke.nodes.NoOp(name='X')` kwarg
  `name=` принимает. `Group.add()` и `nuke.toGroup` НЕ существуют.
- **eval/exec канон execute-хендлера:** успешный eval = немедленный return
  (даже если вернулся None — print валидно возвращает None!); exec только по
  SyntaxError. Дозапуск exec при None двоит побочные эффекты (живой баг 16.10).
- **importlib.reload сервера** (хот-релоад без рестарта Nuke): stateful
  глобали гвардятся `globals().get(...)`; если состояние потеряно — живой
  сервер достаётся из `threading.enumerate()` (`_target.__self__` треда
  accept_loop).

## Смоук

- `test_headless.py` — движком, 20/20, порт 9991, мусорный граф Constant→Blur
  создаётся и удаляется, ничего не сохраняется.
- `tests/socket_smoke.py` (копия общего `work\smoke\socket_smoke.py`) — живой Nuke,
  read-only, 5 проверок.

## Бэклог v0.2 (не делать впрок)

Пуш-ивенты (addOnCreate/KnobChanged → клиент), doc-search, version-gating NukeX/17+
(splat/bigcat), FBX/Alembic — по реальной задаче. Чужие репо-референсы:
kleer001/nuke-mcp (57 тулов, Nuke 17.0v1 — источник подтверждения
`executeInMainThread`), linchpinhq/nuke-mcp (Node.js, сырой).
