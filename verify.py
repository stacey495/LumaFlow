import http.client
import io
import json
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from lumaflow import config
from lumaflow.gateway import Gateway, GatewayHandler
from lumaflow.tls import server_context
from lumaflow.worker import Worker


class Request:
    def __init__(self, data):
        self.incoming = io.BytesIO(data)
        self.outgoing = bytearray()

    def makefile(self, *_args):
        return self.incoming

    def sendall(self, data):
        self.outgoing.extend(data)


class Incoming:
    def __init__(self, data):
        self.data = data

    def makefile(self, *_args):
        return io.BytesIO(self.data)


def call(app, method, path, payload=b"", headers=None, expected=200):
    headers = {"Host": "localhost:8443", "Content-Length": str(len(payload)), **(headers or {})}
    raw = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n").encode()
    request = Request(raw + payload)

    def errors(*_args):
        raise

    GatewayHandler(request, ("127.0.0.1", 40000), SimpleNamespace(app=app, handle_error=errors))
    response = http.client.HTTPResponse(Incoming(request.outgoing))
    response.begin()
    data = response.read()
    assert response.status == expected, (method, path, response.status, data[:350])
    if response.headers.get_content_type() == "application/json":
        data = json.loads(data)
    return data, response.headers


def signin(app, username, password, register=False):
    path = "/api/register" if register else "/api/login"
    user, headers = call(app, "POST", path, json.dumps({"username": username, "password": password}).encode(),
                         {"Content-Type": "application/json"}, 201 if register else 200)
    cookie = headers["Set-Cookie"]
    assert all(attribute in cookie for attribute in ("Secure", "HttpOnly", "SameSite=Strict"))
    return {"Cookie": cookie.split(";")[0], "X-CSRF-Token": user["user"]["csrf"]}


def verify_tls(directory):
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server = server_context(directory).wrap_bio(server_in, server_out, server_side=True)
    client_context = ssl.create_default_context(cafile=str(directory / "localhost.crt"))
    client = client_context.wrap_bio(client_in, client_out, server_hostname="localhost")
    ready = set()
    for _ in range(10):
        for stream, outgoing, incoming in ((client, client_out, server_in), (server, server_out, client_in)):
            if stream not in ready:
                try:
                    stream.do_handshake()
                    ready.add(stream)
                except ssl.SSLWantReadError:
                    pass
            if outgoing.pending:
                incoming.write(outgoing.read())
        if len(ready) == 2:
            break
    assert len(ready) == 2
    assert client.version() in ("TLSv1.2", "TLSv1.3")
    print("TLS certificate + authenticated handshake", client.version(), flush=True)


def until(check, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(.17)
    raise AssertionError("Timed out")


def run(directory):
    verify_tls(directory)
    worker = Worker("timeout-verification", "", directory)
    worker.task = {"progress": 0}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    worker.watch(child, directory, time.monotonic() - config.TIME_LIMIT)
    assert child.poll() is not None and worker.task["status"] == "failed" and "45" in worker.task["error"]
    print("Compute timeout terminates the child process", flush=True)
    app = Gateway(directory, "native", 8443, 19451, "verification")
    app.scheduler.start()
    try:
        until(lambda: app.health()["ready_workers"] == 2, 10)
        call(app, "GET", "/", expected=200)
        call(app, "GET", "/api/jobs", expected=401)
        first = signin(app, "studio_user", "test-1234-secret", register=True)
        second = signin(app, "other_user", "test-5678-secret", register=True)
        admin_password = json.loads((directory / "admin.json").read_text())["password"]
        admin = signin(app, "admin", admin_password)
        call(app, "GET", "/api/admin", headers=first, expected=403)
        call(app, "POST", "/api/jobs", b"hello", {**first, "X-Profile": "deep"}, 422)
        bad = io.BytesIO()
        Image.new("RGB", (5001, 10)).save(bad, "PNG")
        call(app, "POST", "/api/jobs", bad.getvalue(), {**first, "X-Profile": "deep"}, 422)
        call(app, "POST", "/api/jobs", headers={**first, "X-Profile": "deep", "Content-Length": str(config.MAX_BYTES + 1)}, expected=413)
        call(app, "POST", "/api/jobs", headers={"Cookie": first["Cookie"], "X-Profile": "deep"}, expected=403)
        photo, _ = call(app, "GET", "/api/demo", headers=first)
        ids = []
        for index in range(6):
            response, _ = call(app, "POST", "/api/jobs", photo,
                               {**first, "X-Profile": "deep", "X-Image-Name": f"example-{index}.jpg"}, 202)
            ids.append(response["job"]["id"])
        call(app, "POST", "/api/jobs", photo, {**first, "X-Profile": "deep"}, 429)
        call(app, "GET", "/api/jobs/" + ids[0] + "/original", headers=second, expected=404)
        call(app, "GET", "/api/jobs/" + ids[0] + "/original", headers=first)

        def monitoring():
            return call(app, "GET", "/api/admin", headers=admin)[0]

        def history():
            return call(app, "GET", "/api/jobs", headers=first)[0]["jobs"]

        observed = until(lambda: [item for item in monitoring()["active"] if item["queue"]], 5)
        assert all(item["queue"]["position"] > 0 and item["queue"]["seconds"] > 0 for item in observed)
        running = until(lambda: [job for job in history() if job["status"] == "running" and 9 <= job["progress"] < 100], 12)
        assigned = until(lambda: [worker for worker in monitoring()["workers"] if worker["state"] in ("busy", "ready")]
                         if app.health()["ready_workers"] == 4 else None, 12)
        assert len({worker["pid"] for worker in assigned}) == 4
        jobs_by_worker = {job["worker"] for job in history() if job["worker"]}
        assert {"app-1", "app-2", "app-3", "app-4"} <= jobs_by_worker, jobs_by_worker
        print("Verified dispatch to:", ", ".join(sorted(jobs_by_worker)), flush=True)
        queued = next(job for job in history() if job["status"] == "queued")
        call(app, "POST", "/api/jobs/" + queued["id"] + "/cancel", headers=first)
        call(app, "POST", "/api/jobs/" + running[0]["id"] + "/cancel", headers=first)
        until(lambda: sum(job["status"] == "cancelled" for job in history()) == 2, 8)
        completed = until(lambda: [job for job in history() if job["status"] == "completed"], 50)
        assert completed[0]["progress"] == 100
        result, headers = call(app, "GET", "/api/jobs/" + completed[0]["id"] + "/download", headers=first)
        assert "attachment" in headers["Content-Disposition"]
        assert Image.open(io.BytesIO(result)).size == (2600, 2000)
        assert result != photo
        until(lambda: not monitoring()["active"], 55)
        until(lambda: app.health()["ready_workers"] == 2, 24)
        print("Access control, upload limits, queue and ETA, progress, 2 -> 4 -> 2 workers, both cancellations, JPEG result", flush=True)
    finally:
        app.scheduler.close()

    reopened = Gateway(directory, "native", 8443, 19451, "verification-reopen")
    try:
        rows = reopened.store.jobs()
        assert any(job["status"] == "completed" for job in rows)
        assert sum(job["status"] == "cancelled" for job in rows) == 2
        print("SQLite persistence", flush=True)
    finally:
        reopened.scheduler.executor.shutdown()
        reopened.scheduler.log.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="verification-", dir=config.ROOT / ".runtime" if (config.ROOT / ".runtime").exists() else config.ROOT) as folder:
        run(Path(folder))
