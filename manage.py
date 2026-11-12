import argparse
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from lumaflow.config import MIN_WORKERS, ROOT


def health(directory, port):
    try:
        context = ssl.create_default_context(cafile=str(directory / "localhost.crt"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        with opener.open(f"https://localhost:{port}/health", timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        current = state_at(directory)
        if current.get("port") == port and alive(current["pid"]):
            path = directory / ("health-" + current["instance"] + ".json")
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    return {}


def state_at(directory):
    path = directory / "service.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def alive(pid):
    from lumaflow.metrics import sample
    return sample(pid) is not None


def open_browser(directory, port):
    from lumaflow.tls import fingerprint

    if os.name != "nt":
        raise RuntimeError(f"Відкрий https://localhost:{port} та довір сертифікату {directory / 'localhost.crt'}.")
    from winreg import HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, OpenKey, QueryValue

    executable = None
    for name, standard in (("msedge.exe", "Microsoft\\Edge\\Application\\msedge.exe"),
                           ("chrome.exe", "Google\\Chrome\\Application\\chrome.exe")):
        for key in (HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE):
            try:
                with OpenKey(key, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as app:
                    executable = QueryValue(app, None)
                    break
            except OSError:
                pass
        if not executable:
            locations = (os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"))
            executable = next((str(Path(loc) / standard) for loc in locations if loc and (Path(loc) / standard).is_file()), None)
        if executable:
            break
    if not executable:
        raise RuntimeError(f"Відкрий https://localhost:{port} у браузері з довірою до сертифіката {directory / 'localhost.crt'}.")
    subprocess.Popen([executable, f"--user-data-dir={directory / 'browser'}", "--no-first-run",
                      "--no-default-browser-check", "--ignore-certificate-errors-spki-list=" + fingerprint(directory),
                      f"https://localhost:{port}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start(options):
    directory = options.data
    directory.mkdir(parents=True, exist_ok=True)
    if not options.foreground:
        current = state_at(directory)
        if current and health(directory, current["port"]).get("instance") == current["instance"]:
            if options.mode != current["mode"]:
                raise RuntimeError("Зупини поточний режим командою python manage.py stop перед перемиканням.")
            print(f"https://localhost:{current['port']}")
            if options.open:
                open_browser(directory, current["port"])
            return
        if current and alive(current["pid"]):
            raise RuntimeError("Служба ще запускається або завершує роботу. Спробуй за кілька секунд.")
    instance = secrets.token_hex(8)
    args = [sys.executable, "-m", "lumaflow.gateway", "--data", str(directory), "--mode", options.mode,
            "--port", str(options.port), "--worker-port", str(options.worker_port), "--instance", instance]
    if options.foreground:
        from lumaflow.gateway import serve

        (directory / "service.json").write_text(json.dumps({"pid": os.getpid(), "instance": instance, "port": options.port,
                                                              "mode": options.mode}), encoding="utf-8")
        serve(directory, options.mode, options.port, options.worker_port, instance)
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log_path = directory / "gateway.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=flags, start_new_session=os.name != "nt",
                                   env=dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1"))
    (directory / "service.json").write_text(json.dumps({"pid": process.pid, "instance": instance, "port": options.port,
                                                          "mode": options.mode}), encoding="utf-8")
    deadline = time.monotonic() + 42
    while time.monotonic() < deadline and process.poll() is None:
        ready = health(directory, options.port)
        if ready.get("instance") == instance and ready["ready_workers"] >= MIN_WORKERS:
            print(f"LumaFlow · https://localhost:{options.port} · {ready['ready_workers']} сервери · {options.mode}")
            if options.open:
                open_browser(directory, options.port)
            return
        time.sleep(0.3)
    if process.poll() is None:
        (directory / ("stop-" + instance)).touch()
    tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:])
    raise RuntimeError(f"Сервери не вийшли на готовність. {log_path}\n{tail}")


def stop(directory):
    current = state_at(directory)
    if not current or not alive(current["pid"]):
        print("LumaFlow зупинено.")
        return
    stop_path = directory / ("stop-" + current["instance"])
    if health(directory, current["port"]).get("instance") != current["instance"] and not stop_path.exists():
        raise RuntimeError("Служба ще запускається. Спробуй за кілька секунд.")
    stop_path.touch()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if not alive(current["pid"]):
            print("LumaFlow зупинено.")
            return
        time.sleep(0.3)
    raise RuntimeError("Служба ще завершує роботу. Перевір журнал у .runtime.")


def build_image():
    from lumaflow.scheduler import DOCKER_IMAGE, docker_engine

    docker_engine()
    return subprocess.run(["docker", "build", "-t", DOCKER_IMAGE, str(ROOT)], cwd=ROOT).returncode


def docker_demo(directory, current, show_browser):
    from lumaflow.scheduler import DOCKER_IMAGE, docker_engine

    docker_engine()
    images = subprocess.run(["docker", "image", "ls", "--filter", f"reference={DOCKER_IMAGE}", "--format", "{{.ID}}"],
                            capture_output=True, text=True, check=True, timeout=15)
    if not images.stdout.strip() and build_image() != 0:
        raise RuntimeError("Docker не зміг зібрати образ. Перевір вивід збірки та доступ до Docker Hub і PyPI.")
    if current.get("mode") == "native" and health(directory, current["port"]).get("instance") == current["instance"]:
        stop(directory)
    start(SimpleNamespace(data=directory, mode="docker", port=8443, worker_port=18451,
                          foreground=False, open=show_browser))


def main():
    parser = argparse.ArgumentParser(description="Керування LumaFlow")
    parser.add_argument("--data", type=Path, default=ROOT / ".runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("start")
    run.add_argument("--mode", choices=("native", "docker"), default="native")
    run.add_argument("--port", type=int, default=8443)
    run.add_argument("--worker-port", type=int, default=18451)
    run.add_argument("--open", action="store_true")
    run.add_argument("--foreground", action="store_true")
    demo = commands.add_parser("docker-demo")
    demo.add_argument("--open", action="store_true")
    for name in ("stop", "status", "open", "admin", "build"):
        commands.add_parser(name)
    options = parser.parse_args()
    options.data = options.data.resolve()
    current = state_at(options.data)
    try:
        if options.command == "start":
            if not 1024 <= options.port <= 65535 or not 1024 <= options.worker_port <= 65532 or options.worker_port <= options.port < options.worker_port + 4:
                raise RuntimeError("Вкажи різні непривілейовані порти для вебсайту та серверів обробки.")
            start(options)
        elif options.command == "stop":
            stop(options.data)
        elif options.command == "status":
            print(json.dumps(health(options.data, current.get("port", 8443)) or {"service": "stopped"}, ensure_ascii=False))
        elif options.command == "open":
            if not health(options.data, current.get("port", 8443)):
                raise RuntimeError("Спочатку запусти LumaFlow.")
            open_browser(options.data, current["port"])
        elif options.command == "admin":
            account = json.loads((options.data / "admin.json").read_text(encoding="utf-8"))
            print(f"{account['username']}\n{account['password']}")
        elif options.command == "build":
            return build_image()
        elif options.command == "docker-demo":
            docker_demo(options.data, current, options.open)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
