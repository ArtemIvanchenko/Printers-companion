"""Run the current checkout against the existing, local OrbStack storage.

Credentials are read in memory from the storage containers, never printed or
written into this script. This does not start old application images.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import time

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".operator-state" / "desktop-import"


def container_env(name):
    info = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
    return dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)


def configure():
    from sqlalchemy import URL

    pg = container_env("printerscompanion-postgres-1")
    minio = container_env("printerscompanion-minio-1")
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "raw").mkdir(exist_ok=True)
    os.environ.update({
        "APP_ENV": "local",
        "DATABASE_URL": URL.create(
            "postgresql+psycopg", username=pg["POSTGRES_USER"],
            password=pg["POSTGRES_PASSWORD"], database=pg["POSTGRES_DB"],
            host="127.0.0.1", port=55432,
        ).render_as_string(hide_password=False),
        "MINIO_ENDPOINT": "127.0.0.1:9000",
        "MINIO_ROOT_USER": minio["MINIO_ROOT_USER"],
        "MINIO_ROOT_PASSWORD": minio["MINIO_ROOT_PASSWORD"],
        "REDIS_URL": "redis://printerscompanion-redis-1.orb.local:6379/0",
        "STARTUP_IMPORT_ENABLED": "false",
        "LLM_PROVIDER": "null",
        "COMPUTE_NODE_ID": "local-operator",
        "OPERATOR_INSTANCE_FILE": str(ROOT / ".operator-state" / "instance-id"),
        "RAW_LOGS_CONTAINER_PATH": str(STATE / "raw"),
        "INCOMING_PATH": str(STATE / "raw"),
        "FILE_STABILITY_SECONDS": "0",
        "GIT_COMMIT": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
    })
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "migrate", "serve", "worker", "estimate-worker", "run", "proxy", "start"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "start":
        subprocess.run(["docker", "start", "printerscompanion-postgres-1",
                        "printerscompanion-minio-1", "printerscompanion-redis-1"], check=True)
    configure()
    if args.action == "start":
        for action, port in (("proxy", 55432), ("serve", 8000)):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    print(f"Port {port} is already occupied; leaving its process unchanged")
                continue
            except OSError:
                pass
            with (STATE / f"{action}.log").open("ab") as stream:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), action],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True)
            (STATE / f"{action}.pid").write_text(str(child.pid) + "\n")
            for _ in range(30):
                if child.poll() is not None:
                    raise RuntimeError(f"{action} exited; inspect its local log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError(f"{action} did not become ready")
        from urllib.request import urlopen
        with urlopen("http://127.0.0.1:8000/health", timeout=15) as response:
            print("Local health:", response.status)
        import fcntl
        for action in ('worker', 'estimate-worker'):
            with (STATE / f'{action}.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(f'{action}: already running')
                    continue
                fcntl.flock(lock, fcntl.LOCK_UN)
            with (STATE / f'{action}.log').open('ab') as stream:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), action],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True)
            time.sleep(1)
            if child.poll() is not None:
                raise RuntimeError(f'{action} exited; inspect its local log')
        print("Open http://127.0.0.1:8000/; local import and estimate workers are running")
    elif args.action == "proxy":
        import asyncio

        async def forward(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        async def accept(reader, writer):
            try:
                upstream, output = await asyncio.open_connection(
                    "printerscompanion-postgres-1.orb.local", 5432,
                )
                await asyncio.gather(forward(reader, output), forward(upstream, writer))
            except (OSError, ConnectionError):
                writer.close()

        async def proxy():
            server = await asyncio.start_server(accept, "127.0.0.1", 55432)
            async with server:
                await server.serve_forever()

        asyncio.run(proxy())
    elif args.action == "backup":
        from datetime import datetime, timezone
        pg = container_env("printerscompanion-postgres-1")
        destination = STATE / (datetime.now(timezone.utc).strftime("before-import-%Y%m%dT%H%M%SZ") + ".dump")
        with destination.open("xb") as stream:
            subprocess.run(["docker", "exec", "printerscompanion-postgres-1", "pg_dump",
                            "-U", pg["POSTGRES_USER"], "-d", pg["POSTGRES_DB"], "-Fc"],
                           stdout=stream, check=True)
        destination.chmod(0o600)
        print(f"Backup: {destination} ({destination.stat().st_size} bytes)")
    elif args.action == "migrate":
        from storage.db.migrate import upgrade_to_head
        upgrade_to_head()
    elif args.action == "serve":
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "api.main:app",
                                 "--host", "127.0.0.1", "--port", "8000"])
    elif args.action in {"worker", "estimate-worker"}:
        import fcntl
        lock = (STATE / f'{args.action}.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('This local worker is already running') from None
        os.set_inheritable(lock.fileno(), True)
        (STATE / f'{args.action}.pid').write_text(str(os.getpid()) + '\n')
        module = 'worker.tasks' if args.action == 'worker' else 'worker.estimate_tasks'
        os.execv(sys.executable, [sys.executable, "-m", module])
    else:
        os.execv(sys.executable, [sys.executable, *args.args])


if __name__ == "__main__":
    main()
