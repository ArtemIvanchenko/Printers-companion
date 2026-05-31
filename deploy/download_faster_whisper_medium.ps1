$ErrorActionPreference = "Stop"

$script = @'
from pathlib import Path
import time
import requests

base = "https://modelscope.cn/models/pengzhendong/faster-whisper-medium/resolve/master"
target = Path("/models/stt/preloaded/faster-whisper-medium")
target.mkdir(parents=True, exist_ok=True)

files = {
    "config.json": 2257,
    "tokenizer.json": 2203239,
    "vocabulary.txt": 459861,
    "model.bin": 1527906378,
}

session = requests.Session()
chunk_size = 1024 * 1024
progress_step = 50 * 1024 * 1024
for name, expected in files.items():
    url = f"{base}/{name}"
    final = target / name
    part = target / f"{name}.part"
    if final.exists() and final.stat().st_size == expected:
        print(f"OK {name}", flush=True)
        continue
    if final.exists():
        final.rename(part)
    attempt = 0
    while True:
        attempt += 1
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        print(f"{name}: attempt={attempt}, offset={offset}/{expected}", flush=True)
        try:
            with session.get(url, headers=headers, stream=True, timeout=(60, 180)) as response:
                response.raise_for_status()
                mode = "ab" if offset and response.status_code == 206 else "wb"
                downloaded = offset if mode == "ab" else 0
                next_progress = ((downloaded // progress_step) + 1) * progress_step
                with part.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if downloaded >= next_progress:
                                handle.flush()
                                percent = downloaded / expected * 100
                                print(f"{name}: {downloaded}/{expected} ({percent:.1f}%)", flush=True)
                                next_progress += progress_step
            size = part.stat().st_size
            if size == expected:
                part.replace(final)
                print(f"DONE {name}", flush=True)
                break
            print(f"INCOMPLETE {name}: {size}/{expected}", flush=True)
        except Exception as exc:
            print(f"FAILED {name}: {exc}", flush=True)
        time.sleep(20)
print("ALL DONE", flush=True)
'@

$script | docker compose exec -T telegram-bot python -
