"""HTTP-сервер поверх gigacode CLI: веб-чат + API для других программ.

Запуск:  python server.py --port 8765 --workdir ./workspace

Эндпоинты:
  GET  /                      веб-чат
  GET  /health                статус
  POST /api/chat              {"prompt": "..."} или {"messages": [...]}, опц. "stream", "cwd"
  POST /v1/chat/completions   OpenAI-совместимый (stream и обычный режим)
  GET  /v1/models             список моделей (для OpenAI-клиентов)
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import select
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from gigacode_runner import GigaCode, GigaCodeError, build_prompt

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MODEL_NAME = "gigacode"
MAX_BODY = 10 * 1024 * 1024
HEARTBEAT = 3.0  # с; как часто проверять клиента, пока CLI молчит


class App:
    def __init__(self, runner: GigaCode, token: Optional[str], max_parallel: int):
        self.runner = runner
        self.token = token
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.max_parallel = max_parallel
        self.active = 0
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "gigacode-serve/1.0"
    protocol_version = "HTTP/1.1"
    app: App  # выставляется в main()

    # --- утилиты ----------------------------------------------------------
    def log_message(self, fmt, *args):
        print("[{}] {} {}".format(time.strftime("%H:%M:%S"), self.address_string(), fmt % args), flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, openai_style: bool = False):
        if openai_style:
            self._json(status, {"error": {"message": message, "type": "gigacode_error"}})
        else:
            self._json(status, {"error": message})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("Слишком большой запрос")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("Ожидался JSON-объект")
        return data

    def _authorized(self) -> bool:
        if not self.app.token:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + self.app.token

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True

    def _sse(self, payload):
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write("data: {}\n\n".format(data).encode("utf-8"))
        self.wfile.flush()

    def _client_gone(self) -> bool:
        """True, если клиент закрыл соединение (сокет читаем и отдаёт EOF)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _ping(self):
        # SSE-комментарий: клиенты его игнорируют, а прокси не рвут «молчащее» соединение
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    def _run(self, prompt: str, cwd: Optional[str], on_chunk, on_idle=None):
        """Запускает CLI с ограничением параллельности. on_chunk(text) вызывается на каждый кусок.

        Пока CLI молчит, раз в HEARTBEAT секунд проверяем, жив ли клиент (и зовём on_idle,
        например для SSE-пинга). Если клиент отвалился — процесс CLI убивается.
        """
        if not self.app.slots.acquire(timeout=self.app.runner.timeout):
            raise GigaCodeError("Сервер занят: все {} слотов заняты".format(self.app.max_parallel))
        with self.app.lock:
            self.app.active += 1
        cancel = threading.Event()
        q: "queue.Queue" = queue.Queue()

        def pump():
            try:
                for chunk in self.app.runner.stream(prompt, cwd=cwd, cancel=cancel):
                    q.put(("chunk", chunk))
                q.put(("end", None))
            except GigaCodeError as e:
                q.put(("error", e))
            except Exception as e:  # noqa: BLE001 — не теряем неожиданные ошибки потока
                q.put(("error", GigaCodeError("Внутренняя ошибка: {!r}".format(e))))

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                try:
                    kind, value = q.get(timeout=HEARTBEAT)
                except queue.Empty:
                    if self._client_gone():
                        raise BrokenPipeError("client disconnected")
                    if on_idle:
                        on_idle()
                    continue
                if kind == "chunk":
                    on_chunk(value)
                elif kind == "error":
                    raise value
                else:
                    return
        except (BrokenPipeError, ConnectionResetError):
            cancel.set()
            raise
        finally:
            with self.app.lock:
                self.app.active -= 1
            self.app.slots.release()

    # --- маршруты ---------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/health":
            return self._json(200, {
                "status": "ok", "active": self.app.active, "max_parallel": self.app.max_parallel,
                "workdir": self.app.runner.workdir, "command": self.app.runner._base,
            })
        if path == "/v1/models":
            if not self._authorized():
                return self._error(401, "Unauthorized", True)
            return self._json(200, {"object": "list", "data": [
                {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "local"}]})
        self._error(404, "Not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        openai_style = path.startswith("/v1/")
        if not self._authorized():
            return self._error(401, "Unauthorized", openai_style)
        try:
            data = self._read_json()
        except (ValueError, UnicodeDecodeError) as e:
            return self._error(400, "Некорректный JSON: {}".format(e), openai_style)

        try:
            if path == "/api/chat":
                return self._api_chat(data)
            if path == "/v1/chat/completions":
                return self._openai_chat(data)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("клиент отключился, процесс остановлен")
            return
        self._error(404, "Not found", openai_style)

    def _static(self, name: str, ctype: str):
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._error(404, "Not found")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _prompt_from(self, data) -> str:
        if data.get("messages"):
            return build_prompt(data["messages"], self.app.runner.max_prompt_chars)
        prompt = data.get("prompt") or ""
        if not str(prompt).strip():
            raise GigaCodeError("Нужно поле 'prompt' или 'messages'")
        return str(prompt)

    # POST /api/chat — простой собственный формат
    def _api_chat(self, data):
        cwd = data.get("cwd") or None
        try:
            prompt = self._prompt_from(data)
        except GigaCodeError as e:
            return self._error(400, str(e))

        started = time.time()
        if not data.get("stream"):
            parts = []
            try:
                self._run(prompt, cwd, parts.append)
            except GigaCodeError as e:
                return self._error(502, str(e))
            return self._json(200, {"response": "".join(parts).strip(),
                                    "elapsed": round(time.time() - started, 2)})

        self._start_sse()
        try:
            self._run(prompt, cwd, lambda t: self._sse({"type": "delta", "text": t}), self._ping)
            self._sse({"type": "done", "elapsed": round(time.time() - started, 2)})
        except GigaCodeError as e:
            self._sse({"type": "error", "error": str(e)})

    # POST /v1/chat/completions — OpenAI-совместимый
    def _openai_chat(self, data):
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._error(400, "Поле 'messages' обязательно", True)
        try:
            prompt = build_prompt(messages, self.app.runner.max_prompt_chars)
        except GigaCodeError as e:
            return self._error(400, str(e), True)

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        model = data.get("model") or MODEL_NAME
        cwd = data.get("cwd") or None

        if not data.get("stream"):
            parts = []
            try:
                self._run(prompt, cwd, parts.append)
            except GigaCodeError as e:
                return self._error(502, str(e), True)
            text = "".join(parts).strip()
            return self._json(200, {
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        self._start_sse()
        self._sse(chunk({"role": "assistant", "content": ""}))
        try:
            self._run(prompt, cwd, lambda t: self._sse(chunk({"content": t})), self._ping)
            self._sse(chunk({}, "stop"))
        except GigaCodeError as e:
            self._sse({"error": {"message": str(e), "type": "gigacode_error"}})
        self._sse("[DONE]")


def main():
    ap = argparse.ArgumentParser(description="Веб-чат и API-сервер для gigacode CLI")
    ap.add_argument("--host", default=os.environ.get("GIGACODE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GIGACODE_PORT", "8765")))
    ap.add_argument("--workdir", default=os.environ.get("GIGACODE_WORKDIR", "./workspace"),
                    help="каталог, где gigacode работает и может править файлы")
    ap.add_argument("--cmd", default=None, help="команда запуска CLI (по умолчанию gigacode из PATH)")
    ap.add_argument("--timeout", type=float, default=float(os.environ.get("GIGACODE_TIMEOUT", "600")))
    ap.add_argument("--max-parallel", type=int, default=int(os.environ.get("GIGACODE_MAX_PARALLEL", "2")))
    ap.add_argument("--token", default=os.environ.get("GIGACODE_TOKEN"),
                    help="если задан, API требует заголовок Authorization: Bearer <token>")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    try:
        runner = GigaCode(cmd=args.cmd, workdir=workdir, timeout=args.timeout)
    except GigaCodeError as e:
        raise SystemExit(str(e))

    Handler.app = App(runner, args.token, max(1, args.max_parallel))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("gigacode serve: http://{}:{}  (workdir: {})".format(args.host, args.port, workdir))
    print("команда: {} --approval-mode auto-edit -p <prompt>".format(" ".join(runner._base)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")


if __name__ == "__main__":
    main()
