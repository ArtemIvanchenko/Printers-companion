#!/usr/bin/env python3
"""Stable host entrypoint, standard library only (Python 3.9+)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description="Безопасное обновление операторского приложения")
    parser.add_argument("action", choices=["check", "status", "configure", "apply", "rollback", "launch"], nargs="?", default="apply")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--version", help="Явно выбранный опубликованный стабильный релиз")
    parser.add_argument("--compose-file", action="append", dest="compose_files")
    parser.add_argument("--env-file")
    parser.add_argument("--project-name")
    parser.add_argument("--context")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=180)
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    source = Path(__file__).resolve().parents[2]
    state_path = root / ".update-state/state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            current = state.get("current") or {}
            candidate = Path(current.get("source_path") or root).resolve()
            if candidate != root and candidate.is_relative_to(root / ".update-state/releases"):
                commit = subprocess.check_output(["git", "-C", str(candidate), "rev-parse", "HEAD"], text=True).strip()
                dirty = subprocess.check_output(["git", "-C", str(candidate), "status", "--porcelain", "--untracked-files=normal"], text=True).strip()
                if commit != current.get("sha") or dirty:
                    raise ValueError("staged updater identity differs")
                source = candidate
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Не подтверждена целостность установленного обновлятора; выполнение запрещено.", file=sys.stderr)
            return 1
    sys.path.insert(0, str(source))
    from core.updating.releases import UpdateError, stable_release, update_comparison
    from core.updating.runtime import Updater

    if not 0 <= arguments.timeout <= 900:
        parser.error("--timeout должен быть от 0 до 900 секунд")
    updater = Updater(root)
    try:
        if arguments.action == "status":
            result = updater.status()
        elif arguments.action == "check":
            release = stable_release(arguments.version)
            current = updater.status().get("current") or {}
            version, sha = current.get("version", "unknown"), current.get("sha", "unknown")
            try:
                with urlopen("http://127.0.0.1:8000/health", timeout=3) as response:
                    live = json.load(response)
                version, sha = live.get("version", version), live.get("build", {}).get("git_sha", sha)
            except (OSError, ValueError):
                pass
            result = update_comparison(version or "unknown", sha or "unknown", release)
        elif arguments.action == "configure":
            result = updater.configure(compose_files=arguments.compose_files, env_file=arguments.env_file,
                                       project_name=arguments.project_name, context=arguments.context, url=arguments.url)
        elif arguments.action == "rollback":
            result = updater.rollback(timeout=arguments.timeout)
        else:
            if not updater.settings_path.is_file():
                updater.configure(compose_files=arguments.compose_files, env_file=arguments.env_file,
                                  project_name=arguments.project_name, context=arguments.context, url=arguments.url)
            result = (updater.launch(timeout=arguments.timeout) if arguments.action == "launch"
                      else updater.apply(version=arguments.version, timeout=arguments.timeout))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except UpdateError as exc:
        print(f"Обновление не применено: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Обновление прервано. Журнал сохранён; следующий launch/rollback восстановит состояние.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
