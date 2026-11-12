import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from struct import pack, unpack
from urllib.parse import urlsplit

from .config import PROFILES, TIME_LIMIT
from .httpio import HTTPError, Handler, Server
from .images import restore
from .metrics import sample


class Worker:
    def __init__(self, name, token, home):
        self.name = name
        self.token = token
        self.home = home
        self.lock = threading.RLock()
        self.task = None
        self.process = None
        self.temp = None
        self.cancel = threading.Event()
        self.monitor = None
        self.samples = {}
        self.metric_time = time.monotonic()

    def begin(self, job_id, profile, data):
        with self.lock:
            if self.task and self.task["id"] == job_id:
                return self.task.copy()
            if self.process is not None:
                raise HTTPError(409, "Сервер обробки зайнятий.")
            if self.temp:
                self.temp.cleanup()
            self.temp = tempfile.TemporaryDirectory(prefix="job-", dir=self.home)
            path = Path(self.temp.name)
            (path / "input").write_bytes(data)
            self.cancel.clear()
            self.task = {"id": job_id, "status": "running", "progress": 1, "stage": "Запуск процесу", "error": None}
            started = time.monotonic()
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "lumaflow.worker", "compute", str(path), profile, str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                self.task = None
                raise
            self.monitor = threading.Thread(target=self.watch, args=(self.process, path, started), daemon=True)
            self.monitor.start()
            return self.task.copy()

    def watch(self, process, path, started):
        final_status = "failed"
        final_error = "Обробку перервано через помилку процесу."
        while True:
            if self.cancel.is_set():
                final_status, final_error = "cancelled", None
                break
            if time.monotonic() - started >= TIME_LIMIT:
                final_error = f"Перевищено ліміт обчислення ({TIME_LIMIT} с)."
                break
            code = process.poll()
            try:
                progress = json.loads((path / "progress.json").read_text(encoding="utf-8"))
                with self.lock:
                    self.task.update(progress=progress["progress"], stage=progress["stage"])
            except (OSError, ValueError, KeyError):
                pass
            if code is not None:
                if code == 0 and (path / "result.jpg").is_file():
                    final_status, final_error = "completed", None
                elif code == 124:
                    final_error = f"Перевищено ліміт обчислення ({TIME_LIMIT} с)."
                elif (path / "error.txt").is_file():
                    final_error = (path / "error.txt").read_text(encoding="utf-8")
                break
            self.cancel.wait(0.12)
        if process.poll() is None:
            process.kill()
        process.wait()
        with self.lock:
            if self.cancel.is_set():
                final_status, final_error = "cancelled", None
            stage = {"cancelled": "Скасовано", "failed": "Помилка", "completed": "Готово"}[final_status]
            progress = 100 if final_status == "completed" else self.task["progress"]
            self.task.update(status=final_status, progress=progress, stage=stage, error=final_error)
            self.process = None

    def status(self):
        with self.lock:
            pids = [os.getpid()]
            if self.process:
                pids.append(self.process.pid)
            now = time.monotonic()
            measurements = {pid: value for pid in pids if (value := sample(pid)) is not None}
            cpu = sum(max(0, value[0] - self.samples.get(pid, value)[0]) for pid, value in measurements.items())
            ram = sum(value[1] for value in measurements.values())
            cpu_percent = round(100 * cpu / max(0.001, now - self.metric_time), 1)
            self.samples, self.metric_time = measurements, now
            return {"name": self.name, "pid": pids[0], "task_pid": pids[1] if len(pids) == 2 else None,
                    "busy": self.process is not None, "cpu": cpu_percent, "ram_mb": round(ram / 1048576, 1),
                    "task": self.task.copy() if self.task else None}

    def stop(self):
        self.cancel.set()
        if self.monitor:
            self.monitor.join(3)
        if self.temp:
            self.temp.cleanup()


class WorkerHandler(Handler):
    def route(self):
        state = self.server.worker
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + state.token):
            raise HTTPError(403, "Доступ заборонено.")
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/status":
            return self.send_json(200, state.status())
        if self.command == "POST" and path == "/shutdown":
            self.send_json(200, {})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        job_id = self.headers.get("X-Job-Id", "")
        if not re.fullmatch("[a-f0-9]{32}", job_id):
            raise HTTPError(400, "Некоректний ID задачі.")
        if self.command == "POST" and path == "/run":
            profile = self.headers.get("X-Profile", "")
            if profile not in PROFILES:
                raise HTTPError(400, "Невідомий профіль.")
            return self.send_json(202, state.begin(job_id, profile, self.body()))
        if (self.command, path) not in (("POST", "/cancel"), ("GET", "/result")):
            raise HTTPError(404, "Запит не знайдено.")
        return self.send_data_for(state, path, job_id)

    def send_data_for(self, state, path, job_id):
        payload = job_action(state, path, job_id)
        if isinstance(payload, bytes):
            return self.send_data(200, payload, "image/jpeg")
        return self.send_json(200, payload)


def job_action(state, path, job_id):
    with state.lock:
        if not state.task or state.task["id"] != job_id:
            raise HTTPError(404, "Задачу не знайдено.")
        if path == "/cancel":
            if state.process is not None:
                state.cancel.set()
            return state.task.copy()
        if path == "/result" and state.task["status"] == "completed":
            return (Path(state.temp.name) / "result.jpg").read_bytes()
    raise HTTPError(404, "Запит не знайдено.")


def read_frame(stream, limit):
    header = stream.read(4)
    if len(header) != 4:
        raise EOFError
    length = unpack("!I", header)[0]
    if length > limit:
        raise ValueError("Frame too large")
    data = stream.read(length)
    if len(data) != length:
        raise EOFError
    return data


def write_frame(stream, data):
    stream.write(pack("!I", len(data)))
    stream.write(data)
    stream.flush()


def serve_stdio(state):
    from .config import MAX_BYTES

    source, target = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            request = json.loads(read_frame(source, 1024))
        except EOFError:
            return
        try:
            path = request["path"]
            if path == "/shutdown":
                write_frame(target, b"{}")
                return
            if path == "/status":
                response = state.status()
            elif path == "/run":
                response = state.begin(request["id"], request["profile"], read_frame(source, MAX_BYTES))
            else:
                response = job_action(state, path, request["id"])
        except HTTPError as error:
            response = {"_rpc_error": str(error)}
        write_frame(target, response if isinstance(response, bytes) else json.dumps(response).encode())


def compute(path, profile, parent):
    def watchdog():
        deadline = time.monotonic() + TIME_LIMIT + 1
        while time.monotonic() < deadline and sample(parent) is not None:
            time.sleep(0.25)
        os._exit(124)

    threading.Thread(target=watchdog, daemon=True).start()

    def report(progress, stage):
        temporary = path / "progress.next"
        temporary.write_text(json.dumps({"progress": progress, "stage": stage}, ensure_ascii=False), encoding="utf-8")
        for _ in range(5):
            try:
                temporary.replace(path / "progress.json")
                break
            except PermissionError:
                time.sleep(0.01)

    try:
        restore(path / "input", path / "result.jpg", profile, report)
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "Не вдалося декодувати або зберегти зображення."
        (path / "error.txt").write_text(message, encoding="utf-8")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--name", required=True)
    serve.add_argument("--port", type=int)
    serve.add_argument("--stdio", action="store_true")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--home", type=Path)
    job = commands.add_parser("compute")
    job.add_argument("path", type=Path)
    job.add_argument("profile", choices=PROFILES)
    job.add_argument("parent", type=int)
    options = parser.parse_args()
    if options.command == "compute":
        return compute(options.path, options.profile, options.parent)
    state = Worker(options.name, os.environ["LUMAFLOW_WORKER_TOKEN"], options.home)
    if options.stdio:
        try:
            serve_stdio(state)
        except KeyboardInterrupt:
            pass
        finally:
            state.stop()
        return 0
    with Server((options.host, options.port), WorkerHandler) as server:
        server.worker = state
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            state.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
