import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import MAX_BYTES


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, tls=None):
        self.tls = tls
        super().__init__(address, handler)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.allow_reuse_address = False
        super().server_bind()

    def process_request_thread(self, request, address):
        request.settimeout(12)
        if self.tls:
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except OSError:
                request.close()
                return
        super().process_request_thread(request, address)


class Handler(BaseHTTPRequestHandler):
    server_version = "LumaFlow"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def dispatch(self):
        try:
            self.route()
        except HTTPError as error:
            self.send_json(error.status, {"error": str(error)})
        except (ConnectionError, TimeoutError):
            return
        except Exception:
            self.server.handle_error(self.request, self.client_address)
            self.send_json(500, {"error": "Внутрішня помилка сервера."})

    def body(self, maximum=MAX_BYTES):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as error:
            raise HTTPError(411, "Потрібен розмір запиту.") from error
        if length < 0 or self.headers.get("Transfer-Encoding"):
            raise HTTPError(400, "Некоректне тіло запиту.")
        if length > maximum:
            raise HTTPError(413, "Запит перевищує дозволений розмір.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise HTTPError(400, "Тіло запиту надійшло не повністю.")
        return data

    def json_body(self):
        try:
            data = json.loads(self.body(4096))
        except (ValueError, UnicodeError) as error:
            raise HTTPError(400, "Некоректні дані запиту.") from error
        if not isinstance(data, dict):
            raise HTTPError(400, "Потрібен JSON-об’єкт.")
        return data

    def send_json(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_data(status, data, "application/json; charset=utf-8", headers)

    def send_data(self, status, data, media, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
