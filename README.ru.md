# MCP Socket for Nuke

Локальный MCP-мост для Foundry Nuke — нуковая ветка семейства [mcp-socket](https://github.com/abyrvalg379/mcp-socket). TCP-листенер живёт внутри Nuke и говорит на том же wire-протоколе, что и Blender-мост (blender-mcp 1.6.x совместимый) — любой MCP-клиент управляет Nuke через типизированные тулы.

![Smoke 5/5 passing](https://img.shields.io/badge/smoke-5%2F5%20passing-brightgreen)

**Автор:** Maksim Kovalev · **Версия:** 0.1.0 · **Лицензия:** GPL-3.0

*English documentation: [README.md](README.md)* · Архитектура и грабли Nuke 17.1: [NUKE.md](NUKE.md)

## Как это работает

```
MCP-клиент → nuke_mcp.py (stdio) → TCP 127.0.0.1:9897 → mcp_socket_nuke/server.py (внутри Nuke)
```

`nuke.executeInMainThread` по докстрингу «return immediately» (аналога `WithResult` в Nuke нет), поэтому каждая команда маршаллится замыканием, которое шлёт ответ прямо из главного потока, пока клиентский поток ждёт на `threading.Event` (кап 180 с). Протокол — один JSON-документ на запрос, без фрейминга:

```
→ {"type": "ping", "params": {}}
← {"status": "success", "result": {...}}
```

Порт при занятом 9897 автоидёт на +1…+10 (второй Nuke); реестр инстансов в `%TEMP%` с heartbeat 10 с хранит живые pid.

## Тулы (11)

| Тул | Назначение |
|-----|-----------|
| `ping_nuke` | версия/вариант Nuke, pid, порт, сцена, диапазон кадров, счётчик нод |
| `execute_nuke_code` | Python внутри Nuke — `nuke`/`nukescripts` прединжектированы, stdout/stderr перехвачен, опциональная переменная `result` возвращается JSON-безопасно; сниппет исполняется внутри одной именованной undo-группы |
| `undo_agent_session` | откатывает undo-стек по группам агента «MCP Socket» — останавливается на первой чужой записи, работа юзера не затрагивается |
| `get_scene_info` | путь скрипта, диапазон кадров, fps, формат, флаг прокси, счётчики нод по классам, выделение |
| `get_hierarchy` | граф нод (класс, позиция, флаги disabled/selected, инпуты), кап `max_nodes`; `recurse_groups` спускается в Group-ноды |
| `get_screenshot` | **физический** снимок экрана через `QScreen.grabWindow` (честный на масштабированных мониторах, в отличие от `QWidget.grab`); `mode="window"` кропает окно Nuke, `"screen"` — весь экран; `focus=true` поднимает Nuke |
| `get_console_log` | кольцевой буфер stdout/stderr всей сессии Nuke — глобальный tee + пер-исполнение (`last_n` / `filter` / `stream`) |
| `clear_console_log` | очистить кольцо |
| `list_instances` | живые инстансы Nuke из реестра в `%TEMP%` (pid, порт, версия, сцена) |
| `replay_last_session` | переигрывает модифицирующие команды последней записанной сессии из JSONL-лога (read-only пропускаются, replayed помечаются) |
| `get_session_log_path` | путь к свежайшему JSONL-логу сессии |

## Эргономика для агента

- **Undo ходьбой по меткам** — каждый `execute_nuke_code` открывает одну именованную undo-группу («MCP Socket: …»); пустые (read-only) группы Nuke молча выбрасывает, стек остаётся чистым. `undo_agent_session` читает метку верхнего шага (`nuke.Undo.undoDescribe(0)`) и откатывает, только пока метка принадлежит агенту — чужая метка даёт честную остановку. В engine-режиме undo-стека нет — инструмент отказывается.
- **Лог сессий + реплей** — каждая записанная команда дописывает JSON-строку в `%TEMP%\mcp_socket_nuke\sessions\`; пауза >10 с начинает новый файл, хранятся 30 свежайших. `replay_last_session` переигрывает модифицирующие команды и помечает шаги `replay: true`.
- **Меню + окно** — пункт **MCP Socket → Open Window** (идемпотентный маркер-блок в `menu.py`) открывает окно статуса: шапка с версией, start/stop, путь к логу сессий, просмотр консоли.
- **Headless-тесты** — `import nuke` из бандленного `python.exe` стартует движок без места GUI-лицензии, поэтому `test_headless.py` (20/20) можно гонять при открытом Nuke.

## Установка

1. Возьмите `mcp_socket_nuke_v*.zip` из [последнего релиза](https://github.com/abyrvalg379/mcp-socket-nuke/releases/latest) и распакуйте (или склонируйте репо).
2. Запустите `python install_nuke_mcp.py` **при закрытом Nuke** — инсталлер копирует `mcp_socket_nuke/` и `nuke_mcp.py` в `%USERPROFILE%\.nuke\` и дописывает идемпотентные маркер-блоки в `init.py` (sys.path) и `menu.py` (автостарт + пункт меню).
3. Стартуйте Nuke — листенер поднимается сам на `127.0.0.1:9897` (порт занят → автоидёт +1…+10 для второго Nuke).

### Подключение любого MCP-клиента

Ноль зависимостей сверх стандартной библиотеки Python. Пример JSON-конфига:

```json
{
  "mcpServers": {
    "nuke": {
      "command": "python",
      "args": ["path/to/nuke_mcp.py"]
    }
  }
}
```

`--port 9898` или переменная окружения `NUKE_MCP_SOCKET_PORT` выбирают второй инстанс Nuke.

## Смоук-тест

`tests/socket_smoke.py` — read-only смоук живого моста: сырой TCP по wire-протоколу, только read-only команды (`ping`, `get_scene_info`, `get_hierarchy`, `list_instances`, `get_session_log_path`). Сценарий не мутирует, безопасно запускать посреди сессии:

```
python tests/socket_smoke.py nuke
```

Код выхода 0, когда все достижимые мосты прошли. Тот же скрипт без аргументов смоукит и ветки семейства Blender (`:9876`), Maya (`:7777`), Houdini (`:9877`). `test_headless.py` гоняется движком (без GUI) на одноразовом графе и удаляет его за собой.

## Безопасность

Только localhost, без аутентификации, и `execute_nuke_code` исполняет произвольный Python в вашем Nuke — это однорабочий инструмент для связки художник+агент, а не сервис. Порт наружу не выставлять.

## Смежные инструменты

- [mcp-socket](https://github.com/abyrvalg379/mcp-socket) — ветка семейства для Blender (undo-чекпоинты, кольцо консоли, pipeline FBX-пресеты, оффскрин-рендер)
- [mcp-socket-maya](https://github.com/abyrvalg379/mcp-socket-maya) — майская ветка
- [mcp-socket-houdini](https://github.com/abyrvalg379/mcp-socket-houdini) — хоудиневская ветка
- [PROKLADKA](https://github.com/abyrvalg379/prokladka) — FBX-мост Blender ↔ Maya ↔ Houdini ↔ UE
