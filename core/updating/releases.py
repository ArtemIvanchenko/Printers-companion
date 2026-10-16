"""Published stable releases with verified, portable HTTPS trust."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

REPOSITORY = "ArtemIvanchenko/Printers-companion"
IMAGE_REPOSITORY = "ghcr.io/artemivanchenko/printers-companion"
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class UpdateError(RuntimeError):
    """Bounded, credential-free operator message."""


class UpdateBusy(UpdateError):
    """Owner-local computations must finish before switching the application."""


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if not match:
        raise UpdateError("Релиз должен иметь стабильную версию X.Y.Z.")
    return tuple(map(int, match.groups()))


def validate_sha(value: str) -> str:
    if not _SHA.fullmatch(value):
        raise UpdateError("У релиза нет подтверждённого полного SHA исходников.")
    return value


def github_json(path: str) -> dict:
    # Path comes exclusively from a validated version or fixed endpoint.
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "Printer-Companion-Updater/1",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        # Retain OS trust (including Windows stores), and package the public CA
        # roots so the frozen host does not depend on build-machine OpenSSL paths.
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        with urlopen(request, timeout=15, context=context) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateError("Ответ сервера релизов слишком большой.")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except HTTPError as exc:
        raise UpdateError(f"GitHub недоступен для проверки релиза (HTTP {exc.code}).") from None
    except (URLError, OSError, ValueError):
        raise UpdateError("Не удалось проверить релиз: сеть или ответ GitHub недоступны.") from None


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    sha: str
    url: str
    published_at: str
    title: str
    assets: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return dict(vars(self))


def stable_release(version: str | None = None) -> Release:
    tag = f"v{'.'.join(map(str, version_tuple(version)))}" if version else None
    data = github_json(f"releases/tags/{tag}" if tag else "releases/latest")
    actual_tag = data.get("tag_name", "")
    normalized = ".".join(map(str, version_tuple(actual_tag)))
    if actual_tag != f"v{normalized}" or (tag and actual_tag != tag):
        raise UpdateError("Тег GitHub не соответствует запрошенному релизу.")
    if data.get("draft") or data.get("prerelease") or not data.get("published_at"):
        raise UpdateError("Черновик или предварительный релиз не устанавливается.")
    assets = data.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 256:
        raise UpdateError("Некорректный или слишком большой список файлов релиза.")
    assets = tuple({
        "name": asset["name"],
        "state": "uploaded" if asset.get("state") == "uploaded" else None,
        "size": asset.get("size") if isinstance(asset.get("size"), int) and not isinstance(asset.get("size"), bool) else None,
        "digest": asset.get("digest") if isinstance(asset.get("digest"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"]) else None,
    } for asset in assets if isinstance(asset, dict) and isinstance(asset.get("name"), str) and len(asset["name"]) <= 256)
    # target_commitish may be 'main'; resolve the tag itself, never that branch.
    obj = github_json(f"git/ref/tags/{actual_tag}").get("object", {})
    for _ in range(4):
        sha = validate_sha(obj.get("sha", ""))
        if obj.get("type") == "commit":
            return Release(normalized, actual_tag, sha,
                           f"https://github.com/{REPOSITORY}/releases/tag/{actual_tag}",
                           data["published_at"], str(data.get("name") or actual_tag)[:160], assets)
        if obj.get("type") != "tag":
            break
        obj = github_json(f"git/tags/{sha}").get("object", {})
    raise UpdateError("Не удалось однозначно разрешить тег релиза в commit.")


def compatible_installer(release: Release, *, platform: str, arch: str) -> dict | None:
    """Select manual download metadata, never authorize package installation."""
    version = ".".join(map(str, version_tuple(release.version)))
    if release.tag != f"v{version}":
        raise UpdateError("Тег установщика не соответствует стабильной версии релиза.")
    if platform not in {"darwin", "win32"} or arch not in {"arm64", "x64"}:
        raise UpdateError("Не поддерживается платформа или архитектура установщика.")
    if platform == "win32" and arch != "x64":
        return None
    suffix = f"mac-{arch}.dmg" if platform == "darwin" else "win-x64.exe"
    name = f"Printer-Companion-{version}-{suffix}"
    matching = [asset for asset in release.assets if asset.get("name") == name]
    test_build = not matching
    if test_build:
        sha = validate_sha(release.sha)
        pattern = rf"Printer-Companion-{re.escape(version)}-test\.{sha[:12]}\.[0-9a-f]{{8}}-{re.escape(suffix)}"
        matching = [asset for asset in release.assets if re.fullmatch(pattern, asset.get("name", ""))]
    if len(matching) > 1:
        raise UpdateError("У релиза неоднозначный набор совместимых установщиков.")
    if not matching:
        return None
    asset = matching[0]
    name = asset["name"]
    size = asset.get("size")
    if asset.get("state") != "uploaded" or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    result = {"name": name, "url": f"https://github.com/{REPOSITORY}/releases/download/{release.tag}/{name}", "size": size, "test_build": test_build}
    digest = asset.get("digest")
    if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        result["sha256"] = digest.removeprefix("sha256:")
    return result


def update_comparison(current_version: str, current_sha: str, release: Release) -> dict:
    try:
        current = version_tuple(current_version)
        latest = version_tuple(release.version)
    except UpdateError:
        current = latest = None
    unknown = current is None or not _SHA.fullmatch(current_sha or "")
    ahead = not unknown and current > latest
    conflict = not unknown and current == latest and current_sha != release.sha
    return {
        "channel": "stable", "update_available": not unknown and current < latest,
        "version_unknown": unknown, "ahead_of_release": ahead, "identity_conflict": conflict,
        "current_version": current_version, "current_commit": current_sha[:8],
        "latest_version": release.version, "latest_commit": release.sha[:8],
        "latest_sha": release.sha, "latest_date": release.published_at,
        "latest_message": release.title, "release_url": release.url,
    }
