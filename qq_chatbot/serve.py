"""HTTP 服务（纯 stdlib，无需 FastAPI）。

- ``GET  /health`` → ``{"status": "ok"}``
- ``POST /chat``   → 请求体 ``{"query": "...", "mode": "qa|persona", "person": "..."}``
"""

from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def create_handler(bot):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/health"):
                self._send(200, {"status": "ok", "service": "qq-chatbot"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/chat":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                query = data.get("query") or data.get("message") or ""
                mode = data.get("mode", "qa")
                person = data.get("person")
                ans = bot.answer(query, mode=mode, person=person)
                self._send(
                    200,
                    {
                        "text": ans.text,
                        "grounded": ans.grounded,
                        "citations": [asdict(c) for c in ans.citations],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"error": str(exc)})

        def log_message(self, *args) -> None:  # 静默
            pass

    return Handler


def serve(bot, host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), create_handler(bot))
    print(f"qq-chatbot 服务：http://{host}:{port}  （GET /health，POST /chat）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
