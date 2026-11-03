# gigacode-serve

Веб-чат и HTTP API поверх корпоративного CLI. Каждый запрос — это ровно один вызов

```
gigacode --approval-mode auto-edit -p "<текст запроса>"
```

Только стандартная библиотека Python 3.8+, без pip и без внешних CDN.

## Запуск

```bash
python3 server.py
```

Откройте http://127.0.0.1:8765 — там чат.

Параметры (или переменные окружения):

| Флаг | Env | По умолчанию | Что делает |
|---|---|---|---|
| `--port` | `GIGACODE_PORT` | 8765 | порт |
| `--host` | `GIGACODE_HOST` | 127.0.0.1 | `0.0.0.0` — открыть в сеть (тогда задайте `--token`) |
| `--workdir` | `GIGACODE_WORKDIR` | `./workspace` | каталог, где работает gigacode. **В режиме auto-edit он может править тут файлы** |
| `--cmd` | `GIGACODE_CMD` | `gigacode` из PATH | своя команда запуска, напр. `"node C:/.../cli.js"` |
| `--timeout` | `GIGACODE_TIMEOUT` | 600 | таймаут одного вызова, с |
| `--max-parallel` | `GIGACODE_MAX_PARALLEL` | 2 | сколько CLI запускать одновременно |
| `--token` | `GIGACODE_TOKEN` | — | требовать `Authorization: Bearer <token>` |

Каталог можно переопределить на каждый запрос полем `cwd` (в чате — поле внизу слева).

## Как устроено

- CLI не хранит состояние между вызовами, поэтому история чата склеивается в один промпт
  (инструкции → история → текущий вопрос). Если не влезает в лимит длины командной строки
  (≈30 000 символов на Windows, 200 000 на macOS/Linux), старые реплики отбрасываются.
- Промпт передаётся аргументом через `subprocess` без shell — кавычки, `&`, `$` и т.п. не ломаются.
  На Windows `gigacode.cmd` от npm подменяется прямым вызовом `node <cli.js>`, потому что `cmd.exe`
  портит спецсимволы в аргументах.
- Если клиент закрыл вкладку / оборвал запрос, процесс gigacode убивается вместе с дочерними.

## API для своих скриптов

### Собственный формат — `POST /api/chat`

```bash
curl -s localhost:8765/api/chat -d '{"prompt": "Напиши функцию сортировки на Go"}'
# {"response": "...", "elapsed": 12.3}
```

Поля: `prompt` **или** `messages` (`[{role, content}]`), опционально `cwd` и `stream: true`
(тогда SSE: `{"type":"delta","text":...}`, `{"type":"done"}`, `{"type":"error","error":...}`).

Из Python — [client.py](client.py):

```python
from client import GigaCodeClient
gc = GigaCodeClient("http://127.0.0.1:8765")
print(gc.ask("Привет"))
for piece in gc.stream("Объясни этот код", cwd="/path/to/project"):
    print(piece, end="")
```

Или вообще без сервера — [gigacode_runner.py](gigacode_runner.py):

```python
from gigacode_runner import GigaCode
print(GigaCode(workdir="/path/to/project").ask("Найди баги в main.py"))
```

### OpenAI-совместимый — `POST /v1/chat/completions`

Подходит для любых библиотек и тулов, умеющих в OpenAI API (openai SDK, LangChain, Continue и т.п.):

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="любой-или-ваш-token")
r = client.chat.completions.create(model="gigacode",
                                   messages=[{"role": "user", "content": "Привет"}])
print(r.choices[0].message.content)
```

Поддерживаются `stream: true` и `GET /v1/models`. Поле `usage` всегда нули — CLI их не отдаёт.

### Прочее

- `GET /health` — статус, сколько вызовов идёт сейчас.
- `python3 gigacode_runner.py "вопрос"` / `python3 client.py "вопрос"` — быстрые проверки из консоли.

## Проверка без gigacode

```bash
python3 server.py --cmd "python3 $PWD/tests/fake_gigacode.py"
```

Фейковый CLI отвечает markdown-шаблоном; слово `FAIL` в запросе имитирует ошибку, `SLOW` — 30 с молчания.
