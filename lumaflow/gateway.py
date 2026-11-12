import argparse
import hmac
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import config
from .httpio import HTTPError, Handler, Server
from .images import image_info
from .scheduler import Scheduler, prepare_docker
from .store import Store, credentials
from .tls import server_context

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"), "/sample-preview.jpg": ("sample-preview.jpg", "image/jpeg"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
JOB_PATH = re.compile(r"/api/jobs/([a-f0-9]{32})(?:/(original|result|download|cancel))?")
ADMIN_PATH = re.compile(r"/api/admin/jobs/([a-f0-9]{32})")


class Gateway:
    def __init__(self, directory, mode, port, worker_port, instance):
        self.port = port
        self.instance = instance
        self.origins = {f"https://localhost:{port}", f"https://127.0.0.1:{port}"}
        self.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        self.auth_lock = threading.Lock()
        self.attempts = defaultdict(deque)
        if mode == "docker":
            prepare_docker(directory)
        admin_file = directory / "admin.json"
        if not admin_file.exists():
            admin_file.write_text(json.dumps({"username": "admin", "password": secrets.token_urlsafe(16)}), encoding="utf-8")
            admin_file.chmod(0o600)
        self.store = Store(directory / "lumaflow.sqlite3", json.loads(admin_file.read_text(encoding="utf-8"))["password"])
        self.scheduler = Scheduler(self.store, directory, mode, worker_port)

    def check_auth_rate(self, username):
        with self.auth_lock:
            cutoff = time.monotonic() - 60
            for key, values in tuple(self.attempts.items()):
                while values and values[0] < cutoff:
                    values.popleft()
                if not values:
                    del self.attempts[key]
            if len(self.attempts["*"]) >= 70 or len(self.attempts[username]) >= 12:
                raise HTTPError(429, "Забагато спроб входу. Зачекай хвилину.")
            now = time.monotonic()
            self.attempts["*"].append(now)
            self.attempts[username].append(now)

    def health(self):
        workers = self.scheduler.view.get("workers", [])
        ready = sum(worker["state"] in ("ready", "busy") for worker in workers)
        return {"service": "LumaFlow", "instance": self.instance, "ready_workers": ready,
                "mode": self.scheduler.mode, "transport": self.scheduler.transport}

    def jobs(self, user):
        active = self.store.jobs(statuses=config.ACTIVE)
        estimates = self.scheduler.eta(active)
        jobs = self.store.jobs(owner=user)
        return [{**job, "queue": estimates.get(job["id"])} for job in reversed(jobs)]

    def admin(self):
        view = self.scheduler.view
        with self.store.connection() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
            users = dict(db.execute("SELECT id,username FROM users").fetchall())
        jobs = self.store.jobs()
        active = [job for job in jobs if job["status"] in config.ACTIVE]
        eta = self.scheduler.eta(active)
        return {
            "mode": self.scheduler.mode, "workers": view.get("workers", []), "error": view.get("error"),
            "limits": {"min_workers": config.MIN_WORKERS, "max_workers": config.MAX_WORKERS,
                       "max_active": config.MAX_ACTIVE_SYSTEM, "max_history": config.MAX_HISTORY_SYSTEM},
            "counts": counts,
            "active": [{**job, "username": users[job["owner"]], "queue": eta.get(job["id"])} for job in active],
            "recent": [{**job, "username": users[job["owner"]]} for job in reversed(jobs) if job["status"] in config.TERMINAL][:20],
        }


class GatewayHandler(Handler):
    def route(self):
        app = self.server.app
        if self.headers.get("Host") not in app.hosts:
            raise HTTPError(400, "Некоректний хост.")
        path = urlsplit(self.path).path
        method = self.command
        if method == "GET" and path in STATIC:
            filename, media = STATIC[path]
            return self.send_data(200, (config.PUBLIC / filename).read_bytes(), media)
        if method == "GET" and path == "/health":
            return self.send_json(200, app.health())
        if not path.startswith("/api/"):
            raise HTTPError(404, "Сторінку не знайдено.")
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin is not None and origin not in app.origins:
                raise HTTPError(403, "Невідоме джерело запиту.")
        if method == "POST" and path in ("/api/login", "/api/register"):
            if self.headers.get_content_type() != "application/json":
                raise HTTPError(415, "Потрібен JSON-запит.")
            username, password = credentials(self.json_body())
            app.check_auth_rate(username)
            user = app.store.register(username, password) if path.endswith("/register") else app.store.authenticate(username, password)
            token, session = app.store.create_session(user)
            return self.send_json(201 if path.endswith("/register") else 200, {"user": session},
                                  {"Set-Cookie": self.cookie(token, config.SESSION_SECONDS)})
        token = self.token()
        user = app.store.session(token)
        if method == "GET" and path == "/api/session":
            return self.send_json(200, {"user": user})
        if user is None:
            raise HTTPError(401, "Увійди до акаунта.")
        if method != "GET" and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), user["csrf"]):
            raise HTTPError(403, "Онови сторінку та повтори дію.")
        if method == "POST" and path == "/api/logout":
            app.store.logout(token)
            return self.send_json(200, {}, {"Set-Cookie": self.cookie("", 0)})
        if path == "/api/jobs":
            if method == "GET":
                return self.send_json(200, {"jobs": app.jobs(user["id"])})
            if method == "POST":
                return self.create_job(app, user)
        if path == "/api/demo" and method == "GET":
            return self.send_data(200, (config.PUBLIC / "sample.jpg").read_bytes(), "image/jpeg")
        match = JOB_PATH.fullmatch(path)
        if match:
            job_id, action = match.groups()
            if method == "POST" and action == "cancel":
                return self.send_json(200, {"job": app.store.cancel(user["id"], job_id)})
            if method == "DELETE" and action is None:
                app.store.delete(user["id"], job_id)
                return self.send_json(200, {})
            if method == "GET" and action in ("original", "result", "download"):
                data, media = app.store.asset(user["id"], job_id, action != "original")
                headers = {"Content-Disposition": f'attachment; filename="lumaflow-{job_id[:8]}.jpg"'} if action == "download" else None
                return self.send_data(200, data, media, headers)
        if path == "/api/admin" or ADMIN_PATH.fullmatch(path):
            if user["role"] != "admin":
                raise HTTPError(403, "Потрібні права адміністратора.")
            if method == "GET" and path == "/api/admin":
                return self.send_json(200, app.admin())
            if method == "DELETE" and (entry := ADMIN_PATH.fullmatch(path)):
                app.store.delete(user["id"], entry[1], admin=True)
                return self.send_json(200, {})
        raise HTTPError(404, "Запит не знайдено.")

    def create_job(self, app, user):
        profile = self.headers.get("X-Profile", "")
        if profile not in config.PROFILES:
            raise HTTPError(400, "Вибери профіль обробки.")
        filename = unquote(self.headers.get("X-Image-Name", "photo"))
        filename = "".join(ch for ch in filename if ch.isprintable() and ch not in "/\\")[:120].strip() or "photo"
        data = self.body()
        try:
            width, height, media = image_info(data)
        except ValueError as error:
            raise HTTPError(422, str(error)) from error
        job = app.store.submit(user["id"], filename, width, height, media, profile, data)
        job["queue"] = app.scheduler.eta(app.store.jobs(statuses=config.ACTIVE)).get(job["id"])
        return self.send_json(202, {"job": job})

    def token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        cookie = cookies.get("lumaflow")
        return cookie.value if cookie else ""

    @staticmethod
    def cookie(token, lifetime):
        return f"lumaflow={token}; Path=/; Max-Age={lifetime}; Secure; HttpOnly; SameSite=Strict"


def serve(directory, mode, port, worker_port, instance):
    directory.mkdir(parents=True, exist_ok=True)
    stop_file = directory / ("stop-" + instance)
    ready_file = directory / ("health-" + instance + ".json")
    next_file = ready_file.with_suffix(".next")
    with Server(("127.0.0.1", port), GatewayHandler) as server:
        server.tls = server_context(directory)
        server.app = Gateway(directory, mode, port, worker_port, instance)
        scheduler = server.app.scheduler
        scheduler.start()
        server.timeout = 0.45
        print(f"LumaFlow https://localhost:{port} ({mode})", flush=True)
        previous = None
        try:
            while not stop_file.exists():
                current = server.app.health()
                if current != previous:
                    next_file.write_text(json.dumps(current), encoding="utf-8")
                    try:
                        next_file.replace(ready_file)
                        previous = current
                    except PermissionError:
                        pass
                server.handle_request()
        except KeyboardInterrupt:
            pass
        finally:
            scheduler.close()
            ready_file.unlink(missing_ok=True)
            next_file.unlink(missing_ok=True)
            stop_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--mode", choices=("native", "docker"), required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--worker-port", type=int, required=True)
    parser.add_argument("--instance", required=True)
    options = parser.parse_args()
    serve(options.data, options.mode, options.port, options.worker_port, options.instance)


if __name__ == "__main__":
    main()
