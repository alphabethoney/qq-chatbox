"""stdlib HTTP API with optional token-authenticated local management endpoints."""
from __future__ import annotations

import hmac
import json
import math
import sqlite3
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def create_handler(bot):
    runtime_lock = threading.RLock()

    def update_retrieval(data):
        from .config import RetrievalConfig
        with runtime_lock:
            if set(data) - {"top_k", "vector_weight", "min_score"}:
                raise ValueError("未知检索参数")
            values = {**asdict(bot.config.retrieval), **data}
            if type(values["top_k"]) is not int or not 1 <= values["top_k"] <= 100:
                raise ValueError("top_k 必须为 1..100 整数")
            for name in ("vector_weight", "min_score"):
                value = values[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"{name} 必须为 0..1 有限数值")
            updated = RetrievalConfig(**values)
            bot.config.retrieval = updated
            bot.retriever.config = updated
            return values

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _admin(self):
            token = bot.config.observability.admin_token
            supplied = self.headers.get("Authorization", "")
            return bool(token) and hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode())

        def do_GET(self):
            if self.path in ("/", "/health"):
                self._send(200, {"status": "ok", "service": "qq-chatbot"})
            elif self.path in ("/admin/traces", "/admin/retrieval"):
                if not self._admin():
                    self._send(403, {"error": "forbidden"})
                    return
                if self.path == "/admin/traces":
                    try:
                        self._send(200, {"traces": bot.traces.recent() if bot.traces else []})
                    except (OSError, sqlite3.Error):
                        self._send(503, {"error": "追踪日志暂不可读"})
                else:
                    with runtime_lock:
                        values = asdict(bot.config.retrieval)
                    self._send(200, values)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/chat", "/admin/retrieval"):
                self._send(404, {"error": "not found"})
                return
            if self.path.startswith("/admin/") and not self._admin():
                self._send(403, {"error": "forbidden"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 65536:
                    raise ValueError("请求体长度必须为 1..65536 字节")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求必须是 JSON 对象")
                if self.path == "/admin/retrieval":
                    values = update_retrieval(data)
                    self._send(200, {"retrieval": values, "persisted": False})
                    return
                query = data.get("query") or data.get("message") or ""
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("query 必须为非空字符串")
                with runtime_lock:
                    ans = bot.answer(query, mode=data.get("mode", "qa"), person=data.get("person"))
                self._send(200, {"text": ans.text, "grounded": ans.grounded,
                                 "trace_id": ans.trace_id,
                                 "citations": [asdict(c) for c in ans.citations]})
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                self._send(500, {"error": "处理失败，请检查服务端配置"})

        def log_message(self, *args):
            pass
    return Handler


def serve(bot, host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), create_handler(bot))
    print(f"qq-chatbot 服务：http://{host}:{port}  （GET /health，POST /chat）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
