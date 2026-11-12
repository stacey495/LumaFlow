import hashlib
import heapq
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config
from .worker import read_frame, write_frame

POLL_INTERVAL = 0.45
SCALE_UP_DELAY = 1.4
SCALE_DOWN_DELAY = 16
DOCKER_IMAGE = "lumaflow-worker:local"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def local_transport():
    try:
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.settimeout(0.5)
            client.connect(listener.getsockname())
        return "http"
    except OSError:
        return "stdio"


@dataclass
class Slot:
    name: str
    port: int
    container: str
    process: subprocess.Popen
    born: float
    responses: queue.Queue | None = None
    unreachable: bool = False
    used: int = 0
    idle_since: float | None = None
    snapshot: dict | None = None


class Scheduler:
    def __init__(self, store, directory, mode, port):
        self.store = store
        self.directory = directory
        self.mode = mode
        self.transport = "http" if mode == "docker" else local_transport()
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.label = "lumaflow.project=" + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
        self.prefix = "lumaflow-" + secrets.token_hex(4)
        self.slots = {}
        self.serial = 0
        self.next_start = 0
        self.next_scale = 0
        self.last_error = None
        self.stopped = threading.Event()
        self.view = {}
        self.thread = threading.Thread(target=self.loop, daemon=True, name="scheduler")
        self.executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)
        self.log = (directory / "workers.log").open("a", encoding="utf-8")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self):
        self.thread.start()

    def rpc(self, slot, path, job=None, data=None, timeout=2):
        if slot.responses is not None:
            if slot.unreachable:
                raise OSError("Application server disconnected")
            request = {"path": path}
            if job:
                request.update(id=job["id"], profile=job["profile"])
            try:
                write_frame(slot.process.stdin, json.dumps(request).encode())
                if path == "/run":
                    write_frame(slot.process.stdin, data)
                payload = slot.responses.get(timeout=timeout)
                if payload is None:
                    raise OSError("Application server closed its response stream")
            except (OSError, queue.Empty) as error:
                slot.unreachable = True
                raise OSError("RPC stream unavailable") from error
            if path == "/result" and payload.startswith(b"\xff\xd8") and len(payload) <= config.MAX_RESULT_BYTES:
                return payload
            value = json.loads(payload)
            if "_rpc_error" in value or path == "/result":
                raise ValueError(value.get("_rpc_error", "Invalid result"))
            return value
        headers = {"Authorization": "Bearer " + self.token}
        if job:
            headers.update({"X-Job-Id": job["id"], "X-Profile": job["profile"]})
        request = urllib.request.Request(f"http://127.0.0.1:{slot.port}{path}", data, headers)
        with self.opener.open(request, timeout=timeout) as response:
            payload = response.read(config.MAX_RESULT_BYTES + 1)
            if path == "/result":
                if len(payload) > config.MAX_RESULT_BYTES or not payload.startswith(b"\xff\xd8"):
                    raise ValueError("Invalid result")
                return payload
            return json.loads(payload)

    def inspect(self, slot):
        try:
            return self.rpc(slot, "/status", timeout=0.65)
        except (OSError, ValueError):
            return None

    def spawn(self, index):
        name, port = f"app-{index}", self.port + index - 1
        env = dict(os.environ, LUMAFLOW_WORKER_TOKEN=self.token, PYTHONUNBUFFERED="1")
        container = self.prefix + "-" + name
        streams = self.transport == "stdio"
        if self.mode == "docker":
            cmd = ["docker", "run", "--rm", "--name", container, "--label", self.label,
                   "--cpus", "1", "--memory", "512m", "--pids-limit", "64", "--read-only",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--init",
                   "--tmpfs", "/tmp:rw,noexec,mode=1777,size=64m", "-p", f"127.0.0.1:{port}:8080",
                   "-e", "LUMAFLOW_WORKER_TOKEN", DOCKER_IMAGE, "--name", name,
                   "--port", "8080", "--host", "0.0.0.0"]
        else:
            cmd = [sys.executable, "-m", "lumaflow.worker", "serve", "--name", name, "--home", str(self.directory)]
            cmd.extend(("--stdio",) if streams else ("--port", str(port)))
        process = subprocess.Popen(cmd, env=env, cwd=config.ROOT,
                                   stdin=subprocess.PIPE if streams else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if streams else self.log,
                                   stderr=self.log, creationflags=NO_WINDOW)
        slot = Slot(name, port, container, process, time.monotonic(), queue.Queue() if streams else None)
        self.slots[index] = slot
        if streams:
            threading.Thread(target=self.read_worker, args=(slot,), daemon=True).start()

    @staticmethod
    def read_worker(slot):
        try:
            while True:
                slot.responses.put(read_frame(slot.process.stdout, config.MAX_RESULT_BYTES + 1))
        except (EOFError, OSError, ValueError):
            slot.responses.put(None)

    def stop_slot(self, index):
        slot = self.slots.pop(index)
        if slot.responses is not None and slot.process.poll() is None and not slot.unreachable:
            try:
                self.rpc(slot, "/shutdown")
                slot.process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if self.mode == "native" and self.transport == "http" and slot.process.poll() is None and slot.snapshot:
            try:
                self.rpc(slot, "/shutdown", data=b"")
                slot.process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if self.mode == "docker":
            try:
                subprocess.run(["docker", "stop", "--time", "2", slot.container], timeout=5, check=False,
                               stdout=self.log, stderr=self.log, creationflags=NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if slot.process.poll() is None:
            slot.process.terminate()
            try:
                slot.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                slot.process.kill()
                slot.process.wait()
        if slot.responses is not None:
            slot.process.stdin.close()
            slot.process.stdout.close()

    def update_jobs(self, active, by_name):
        for job in active:
            if job["status"] == "queued":
                continue
            slot = by_name.get(job["worker"])
            report = slot.snapshot.get("task") if slot and slot.snapshot else None
            if not report or report["id"] != job["id"]:
                if time.time() - job["started"] > 6:
                    self.store.finish(job["id"], "failed", "Зв’язок із сервером обробки втрачено.")
                continue
            try:
                status = report["status"]
                if status == "completed":
                    data = None if job["status"] == "cancelling" else self.rpc(slot, "/result", job, timeout=3)
                    self.store.finish(job["id"], "completed", image=data)
                elif status in ("failed", "cancelled"):
                    self.store.finish(job["id"], status, report["error"])
                elif job["status"] == "cancelling":
                    self.rpc(slot, "/cancel", job, b"")
                elif job["progress"] != report["progress"] or job["stage"] != report["stage"]:
                    self.store.progress(job["id"], report["progress"], report["stage"])
            except (OSError, ValueError):
                if time.time() - job["started"] > config.TIME_LIMIT + 8:
                    self.store.finish(job["id"], "failed", "Сервер не повернув результат у дозволений час.")

    def dispatch(self, active):
        assigned = {job["worker"] for job in active if job["worker"]}
        idle = sorted((slot for slot in self.slots.values()
                       if slot.snapshot and not slot.snapshot["busy"] and slot.name not in assigned),
                      key=lambda slot: (slot.used, slot.name))
        waiting = (job for job in active if job["status"] == "queued")
        for slot, job in zip(idle, waiting):
            data = self.store.assign(job["id"], slot.name)
            if data is None:
                continue
            self.serial += 1
            slot.used = self.serial
            slot.idle_since = None
            try:
                slot.snapshot["task"] = self.rpc(slot, "/run", job, data, timeout=3)
                slot.snapshot["busy"] = True
            except (OSError, ValueError):
                slot.snapshot = None

    def scale(self, active, now):
        waiting = [job for job in active if job["status"] == "queued"]
        target = config.MIN_WORKERS
        if waiting and time.time() - waiting[0]["created"] >= SCALE_UP_DELAY:
            target = max(config.MIN_WORKERS, min(config.MAX_WORKERS, len(active)))
        missing = [index for index in range(1, target + 1) if index not in self.slots]
        if missing and now >= max(self.next_start, self.next_scale):
            try:
                for index in missing:
                    self.spawn(index)
                self.next_scale = now + 0.8
            except OSError as error:
                self.last_error = str(error)
                self.next_start = now + 3
        assigned = {job["worker"] for job in active if job["worker"]}
        for index, slot in tuple(self.slots.items()):
            busy = slot.name in assigned or not slot.snapshot or slot.snapshot["busy"]
            if busy:
                slot.idle_since = None
            elif slot.idle_since is None:
                slot.idle_since = now
            if index > config.MIN_WORKERS and not waiting and slot.idle_since is not None and now - slot.idle_since >= SCALE_DOWN_DELAY:
                self.stop_slot(index)

    def cycle(self):
        now = time.monotonic()
        for index, slot in tuple(self.slots.items()):
            if slot.process.poll() is not None or slot.unreachable or (slot.snapshot is None and now - slot.born > 12):
                self.last_error = f"{slot.name} завершився або не відповідає. Перевір workers.log."
                self.next_start = now + 1.5
                self.stop_slot(index)
        active = self.store.jobs(statuses=config.ACTIVE)
        self.scale(active, now)
        slots = tuple(self.slots.values())
        for slot, snapshot in zip(slots, self.executor.map(self.inspect, slots)):
            slot.snapshot = snapshot
        self.update_jobs(active, {slot.name: slot for slot in slots})
        self.dispatch(self.store.jobs(statuses=config.ACTIVE))
        active = self.store.jobs(statuses=config.ACTIVE)
        view = []
        for index in range(1, config.MAX_WORKERS + 1):
            slot = self.slots.get(index)
            snapshot = slot.snapshot if slot else None
            entry = dict(snapshot or {"name": f"app-{index}", "pid": None, "task_pid": None,
                                      "busy": False, "cpu": None, "ram_mb": None})
            task = entry.pop("task", None)
            entry["task_id"] = task["id"] if task and entry["busy"] else None
            entry["state"] = ("busy" if entry["busy"] else "ready") if snapshot else ("starting" if slot else "off")
            entry["container"] = slot.container if slot and self.mode == "docker" else None
            entry["port"] = slot.port if slot and self.transport == "http" else None
            view.append(entry)
        self.view = {"mode": self.mode, "workers": view, "active": active, "error": self.last_error}

    def loop(self):
        while not self.stopped.is_set():
            try:
                self.cycle()
            except Exception:
                import traceback
                traceback.print_exc(file=self.log)
                self.log.flush()
            self.stopped.wait(POLL_INTERVAL)

    def close(self):
        self.stopped.set()
        self.thread.join(12)
        if self.thread.is_alive():
            self.thread.join()
        self.executor.shutdown(wait=True)
        for index, slot in tuple(self.slots.items()):
            if slot.snapshot and slot.snapshot.get("task") and slot.snapshot["busy"]:
                job = self.store.jobs(statuses=("running", "cancelling"))
                job = next((item for item in job if item["worker"] == slot.name), None)
                if job:
                    try:
                        self.rpc(slot, "/cancel", job, b"")
                    except OSError:
                        pass
        time.sleep(0.25)
        for index in tuple(self.slots):
            self.stop_slot(index)
        self.log.close()

    def eta(self, active):
        workers = [worker for worker in self.view.get("workers", []) if worker["state"] in ("ready", "busy")]
        now = time.time()
        active_workers = {job["worker"]: job for job in active if job["worker"]}

        def duration(job):
            megapixels = job["width"] * job["height"] / 1_000_000
            return min(config.TIME_LIMIT, 0.8 + megapixels * config.PROFILES[job["profile"]]["seconds_mp"])

        slots = []
        for worker in workers:
            job = active_workers.get(worker["name"])
            if not job:
                slots.append(1.0 if worker["busy"] else 0.0)
            elif job["status"] == "cancelling":
                slots.append(1.0)
            else:
                elapsed = max(0, now - job["started"])
                progress = max(0, (job["progress"] - 9) / 86)
                if progress >= 0.12:
                    remaining = elapsed * (1 - progress) / progress
                else:
                    remaining = duration(job) - elapsed
                slots.append(max(0.5, min(config.TIME_LIMIT - elapsed, remaining)))
        if not slots:
            slots = [3.0] * config.MIN_WORKERS
        heapq.heapify(slots)
        estimates = {}
        for position, job in enumerate((job for job in active if job["status"] == "queued"), 1):
            wait = heapq.heappop(slots)
            estimates[job["id"]] = {"position": position, "seconds": max(1, int(wait + 0.5))}
            heapq.heappush(slots, wait + duration(job))
        return estimates


def docker_engine():
    if not shutil.which("docker"):
        raise RuntimeError("Команду docker не знайдено. Запусти Docker Desktop з WSL 2.")
    result = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True,
                            timeout=15, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Запусти Docker Desktop і дочекайся готовності рушія.")
    if result.stdout.strip() != "linux":
        raise RuntimeError("Docker Desktop має працювати в режимі Linux-контейнерів.")


def prepare_docker(directory):
    docker_engine()
    label = "lumaflow.project=" + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    result = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={label}"],
                            check=True, capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
    stale = result.stdout.split()
    if stale:
        subprocess.run(["docker", "rm", "-f", *stale], check=True, timeout=15, creationflags=NO_WINDOW)
    image = subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, creationflags=NO_WINDOW)
    if image.returncode:
        raise RuntimeError("Спочатку збери образ командою python manage.py build.")
