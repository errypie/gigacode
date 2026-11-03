"""Клиент к server.py для использования из других проектов (только стандартная библиотека).

    from client import GigaCodeClient
    gc = GigaCodeClient("http://127.0.0.1:8765")
    print(gc.ask("Объясни, что делает этот regex: ^\\d{3}$"))
    for piece in gc.stream("Напиши hello world на Go"):
        print(piece, end="", flush=True)

Из консоли:  python client.py "вопрос"
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Iterator, List, Optional


class GigaCodeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: Optional[str] = None,
                 timeout: float = 900):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, payload: dict):
        req = urllib.request.Request(
            self.base_url + "/api/chat", data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + self.token} if self.token else {})},
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise RuntimeError(msg) from None

    def ask(self, prompt: str, cwd: Optional[str] = None) -> str:
        with self._request({"prompt": prompt, "cwd": cwd}) as r:
            return json.loads(r.read().decode("utf-8"))["response"]

    def chat(self, messages: List[dict], cwd: Optional[str] = None) -> str:
        with self._request({"messages": messages, "cwd": cwd}) as r:
            return json.loads(r.read().decode("utf-8"))["response"]

    def stream(self, prompt: str = "", messages: Optional[List[dict]] = None,
               cwd: Optional[str] = None) -> Iterator[str]:
        payload = {"stream": True, "cwd": cwd}
        payload.update({"messages": messages} if messages else {"prompt": prompt})
        with self._request(payload) as r:
            for raw in r:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                if ev["type"] == "delta":
                    yield ev["text"]
                elif ev["type"] == "error":
                    raise RuntimeError(ev["error"])


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or sys.stdin.read()
    for part in GigaCodeClient().stream(question):
        sys.stdout.write(part)
        sys.stdout.flush()
    print()
