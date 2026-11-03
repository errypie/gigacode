# gigacode-serve

Аналог `qwen serve` для корпоративного gigacode: живые сессии агента через `gigacode --acp`,
веб-интерфейс, в котором видно мысли агента, вызовы инструментов, план и запросы разрешений,
и HTTP API для своих программ (включая OpenAI-совместимый).

Только стандартная библиотека Python 3.8+, без pip и без внешних CDN.

## Запуск

```bash
python3 server.py --workdir /путь/к/проекту
```

Откройте http://127.0.0.1:8765.

- **Слева** — все сессии, включая запросы из API. Цветная точка показывает состояние:
  зелёная — готова, синяя — работает, оранжевая — ждёт разрешения, красная — агент упал.
- **В ленте** — ваши сообщения, размышления агента, план, вызовы инструментов
  (аргументы и результат), запросы разрешений с кнопками и итог хода.
- **«Диагностика»** — stderr процесса агента и полный журнал событий. Смотрите сюда,
  когда что-то сломалось.

Под капотом каждая сессия — отдельный процесс `gigacode --acp --approval-mode auto-edit`.

## Параметры

| Флаг | Env | По умолчанию | Что делает |
|---|---|---|---|
| `--port` | `GIGACODE_PORT` | 8765 | порт |
| `--host` | `GIGACODE_HOST` | 127.0.0.1 | `0.0.0.0` — открыть в сеть (тогда обязательно `--token`) |
| `--workdir` | `GIGACODE_WORKDIR` | `./workspace` | каталог по умолчанию. **Агент может править в нём файлы** |
| `--cmd` | `GIGACODE_CMD` | `gigacode` из PATH | своя команда запуска CLI |
| `--approval-mode` | `GIGACODE_APPROVAL_MODE` | auto-edit | plan / default / auto-edit |
| `--api-backend` | `GIGACODE_API_BACKEND` | acp | как выполнять `/api/chat` и `/v1`: `acp` — через живую сессию (видно в UI), `cli` — старый `gigacode -p` |
| `--api-permission` | `GIGACODE_API_PERMISSION` | reject | что делать с запросами разрешений в `/api/chat` и `/v1`: `reject` (как в `-p`), `allow`, `ask` (ждать кнопку в UI) |
| `--timeout` | `GIGACODE_TIMEOUT` | 1800 | предел одного хода, с |
| `--permission-timeout` | `GIGACODE_PERMISSION_TIMEOUT` | 1800 | сколько ждать ответа на запрос разрешения, потом — отказ |
| `--max-sessions` | `GIGACODE_MAX_SESSIONS` | 16 | живых сессий одновременно |
| `--max-pending-prompts` | `GIGACODE_MAX_PENDING` | 5 | очередь запросов на сессию |
| `--idle-timeout` | `GIGACODE_IDLE_TIMEOUT` | 1800 | закрывать простаивающие сессии без зрителей, с |
| `--token` | `GIGACODE_TOKEN` | — | требовать `Authorization: Bearer <token>` |

## API живых сессий (как у qwen serve)

```bash
# создать сессию (sessionScope "thread" — всегда новая; "single" — подключиться к живой в том же каталоге)
curl -s -X POST localhost:8765/session -d '{"sessionScope": "thread", "permissionPolicy": "ask"}'
# -> {"sessionId": "…", "workspaceCwd": "…", "attached": false, "modes": {…}, …}

# отправить сообщение (встаёт в очередь, ответ — 202)
curl -s -X POST localhost:8765/session/$SID/prompt -d '{"prompt": [{"type": "text", "text": "Найди баги в main.py"}]}'
# -> {"promptId": "…", "lastEventId": 12}

# смотреть всё, что делает агент (SSE). Last-Event-ID: 0 — с начала сессии
curl -N -H "Last-Event-ID: 0" localhost:8765/session/$SID/events

# состояние: работает ли, ждёт ли разрешения и какого
curl -s localhost:8765/session/$SID/status

# ответить на запрос разрешения (requestId и optionId — из события permission_request или из status)
curl -s -X POST localhost:8765/session/$SID/permission/$REQUEST_ID -d '{"outcome": "selected", "optionId": "proceed_once"}'

curl -s -X POST localhost:8765/session/$SID/cancel                              # остановить текущий ход
curl -s -X POST localhost:8765/session/$SID/approval-mode -d '{"mode": "plan"}' # сменить режим
curl -s "localhost:8765/session/$SID/export?format=md"                          # выгрузить переписку
curl -s -X DELETE localhost:8765/session/$SID                                   # закрыть
```

Все маршруты:

| Метод | Путь | |
|---|---|---|
| POST | `/session` | создать / подключиться: `cwd`, `sessionScope`, `permissionPolicy` (`ask`/`allow`/`reject`), `displayName` |
| GET | `/session` | список сессий (своё расширение) |
| GET | `/session/{id}/status` | состояние, ожидающие разрешения, режимы, использование контекста |
| GET | `/session/{id}/events` | SSE: `id:` / `event:` / `data:`, дочитка по `Last-Event-ID` |
| POST | `/session/{id}/prompt` | `{prompt: [{type, text}]}` → 202 `{promptId}` |
| POST | `/session/{id}/cancel` | отменить текущий ход (очередь остаётся) |
| GET | `/session/{id}/pending-prompts` | очередь |
| POST | `/session/{id}/permission/{requestId}` | `{outcome: "selected", optionId}` или `{outcome: "cancelled"}` |
| POST | `/permission/{requestId}` | то же без id сессии |
| POST | `/session/{id}/approval-mode` | `{mode}` |
| POST | `/session/{id}/model` | `{modelId}` |
| PATCH | `/session/{id}/metadata` | `{displayName}` |
| POST | `/session/{id}/heartbeat` | |
| GET | `/session/{id}/transcript` | `?cursor=&limit=` — события страницами |
| GET | `/session/{id}/export` | `?format=md|json` |
| DELETE | `/session/{id}` | закрыть |
| GET | `/health`, `/health?deep=1`, `/capabilities` | |

Основные события в потоке:

| Событие | Что значит |
|---|---|
| `session_update` | всё от агента; `data.update.sessionUpdate`: `agent_thought_chunk` (мысли), `agent_message_chunk` (ответ), `tool_call` / `tool_call_update`, `plan`, `user_message_chunk`, `usage_update`… |
| `permission_request` / `permission_resolved` | агент просит разрешение / решение принято (`by`: client, policy, timeout) |
| `pending_prompt_added` / `_started` / `_completed` | очередь запросов |
| `turn_complete` / `turn_error` | ход закончен (`stopReason`) / ошибка (`message`, `code`) |
| `agent_stderr` | stderr процесса агента |
| `session_died` / `session_closed` | агент упал (`returncode`, `stderrTail`) / сессия закрыта |

## Разовые запросы

Если история не нужна, а нужен просто ответ — их тоже видно в интерфейсе (с меткой `api`/`openai`),
ответ содержит `sessionId` для поиска.

```bash
curl -s localhost:8765/api/chat -d '{"prompt": "Что такое REST?"}'
# {"response": "...", "sessionId": "...", "stopReason": "end_turn", "elapsed": 12.4}
```

Поля: `prompt` **или** `messages` (`[{role, content}]`), опционально `cwd` и `stream: true`
(SSE: `{"type":"delta","text":…}`, `{"type":"done",…}`, `{"type":"error",…}`).

OpenAI-совместимый `POST /v1/chat/completions` (+ `GET /v1/models`) — для openai SDK, LangChain, плагинов IDE.
Токен не нужен, если сервер запущен без `--token` (SDK требует строку — передайте любую):

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="x")
r = client.chat.completions.create(model="gigacode", messages=[{"role": "user", "content": "Привет"}])
print(r.choices[0].message.content)
```

Из Python без зависимостей — [client.py](client.py):

```python
from client import GigaCodeClient
gc = GigaCodeClient("http://127.0.0.1:8765")
print(gc.ask("Привет"))

# живая сессия: события по мере появления
sid = gc.create_session(permission_policy="allow")
gc.prompt(sid, "Объясни структуру проекта")
for ev in gc.events(sid, last_event_id=0, until_turn_end=True):
    print(ev["type"], ev["data"].get("update", {}).get("sessionUpdate", ""))
```

## Отличия от qwen serve

- Каждая сессия — отдельный процесс `gigacode --acp`; одна рабочая папка на сервер по умолчанию
  (другая — через `cwd`).
- Не сделано: `/session/{id}/load` и `/resume` (восстановление после перезапуска), файловые
  маршруты (`/file`, `/list`, `/glob`…), политики голосования за разрешения (у нас — «кто первый»),
  rate limit. CORS открыт для всех — не выставляйте сервер в сеть без `--token`.
- Журнал событий хранится в памяти (8000 последних на сессию), после перезапуска сервера пропадает.

## Проверка без gigacode

```bash
python3 server.py --cmd "python3 $PWD/tests/fake_acp_agent.py"
```

Фейковый агент: слово `TOOL` в запросе — вызов инструмента с запросом разрешения, `SLOW` — 10 с
размышлений (можно проверить «Стоп»), `CRASH` — падение процесса. Для старого режима:
`--api-backend cli --cmd "python3 $PWD/tests/fake_gigacode.py"`.
