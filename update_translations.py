"""Download and validate the official plain-text EhTagTranslation dictionary."""

from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile

from translations import DEFAULT_TRANSLATIONS, parse_translation_data

DEFAULT_URL = "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json.gz"
RELEASE_URL = "https://api.github.com/repos/EhTagTranslation/Database/releases/latest"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024


def _download(url, limit):
    request = Request(url, headers={"User-Agent": "LocalTagCatalog/1", "Accept": "application/vnd.github+json" if url == RELEASE_URL else "application/octet-stream"})
    try:
        with urlopen(request, timeout=40) as response:
            raw = response.read(limit + 1)
    except HTTPError as error:
        if error.code in {403, 429}:
            raise ValueError("GitHub 查询受到限制，请稍后重试。") from error
        raise ValueError("无法连接官方词库，请检查网络后重试。") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError("无法连接官方词库，请检查网络后重试。") from error
    if len(raw) > limit:
        raise ValueError("官方词库响应超过大小限制，未安装。")
    return raw


def local_status(destination=DEFAULT_TRANSLATIONS):
    destination = Path(destination)
    status = {"available": False, "revision": "", "updated_at": "", "entry_count": 0, "release_tag": "", "downloaded_at": "", "archive_sha256": "", "dictionary_sha256": ""}
    if not destination.is_file():
        return status
    try:
        raw = destination.read_bytes()
        payload = json.loads(raw)
        names, namespaces = parse_translation_data(payload)
        head = payload.get("head", {})
        metadata_path = destination.with_name("tag-translations-info.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
        if not isinstance(metadata, dict):
            metadata = {}
        digest = hashlib.sha256(raw).hexdigest()
        revision = head.get("sha", "")
        matches = (not metadata.get("dictionary_sha256") or metadata["dictionary_sha256"] == digest) and (not metadata.get("revision") or metadata["revision"] == revision)
        status.update(available=True, revision=revision, updated_at=head.get("committer", {}).get("when", ""), entry_count=len(names), namespace_count=len(namespaces), dictionary_sha256=digest, metadata_matches_file=matches)
        if matches:
            status.update({key: metadata.get(key, "") for key in ("release_tag", "downloaded_at", "archive_sha256")})
    except (OSError, ValueError, TypeError, AttributeError):
        status["error"] = "本地词库或版本信息无法读取，可重新安装官方词库。"
    return status


def latest_release():
    payload = json.loads(_download(RELEASE_URL, 2 * 1024 * 1024))
    if not isinstance(payload, dict):
        raise ValueError("官方版本信息格式不正确，请稍后重试。")
    asset = next((item for item in payload.get("assets", []) if isinstance(item, dict) and item.get("name") == "db.text.json.gz"), None)
    if not asset:
        raise ValueError("官方发布中没有找到中文词库文件，请稍后重试。")
    url = asset.get("browser_download_url", "")
    if not isinstance(url, str) or not url.startswith("https://github.com/EhTagTranslation/Database/releases/download/") or not url.endswith("/db.text.json.gz"):
        raise ValueError("官方词库下载地址不正确，未下载。")
    digest = asset.get("digest") or ""
    if digest and (not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:") or any(character not in "0123456789abcdefABCDEF" for character in digest[7:])):
        raise ValueError("官方词库校验信息不正确，未下载。")
    return {"tag": payload.get("tag_name", ""), "revision": payload.get("target_commitish", ""), "published_at": payload.get("published_at", ""), "download_url": url, "archive_sha256": digest[7:].lower() if digest else ""}


def compare_version(local, latest):
    if not local["available"]:
        return "not_installed"
    if not local.get("metadata_matches_file", True):
        return "unknown"
    if local.get("archive_sha256") and latest.get("archive_sha256"):
        return "up_to_date" if local["archive_sha256"] == latest["archive_sha256"] else "update_available"
    revision = latest.get("revision", "")
    if isinstance(revision, str) and len(revision) == 40 and all(character in "0123456789abcdefABCDEF" for character in revision) and local.get("revision"):
        return "up_to_date" if local["revision"] == revision else "update_available"
    return "unknown"


def check_latest(destination=DEFAULT_TRANSLATIONS):
    local, latest = local_status(destination), latest_release()
    state = compare_version(local, latest)
    messages = {"not_installed": "尚未安装可用的中文词库，可以安装官方最新版。", "up_to_date": "检查完成：当前词库已是官方最新版本。", "update_available": "检查完成：发现新版中文词库，可以更新。", "unknown": "检查完成：本地版本信息不足，无法确认版本差异，可以安装官方最新版。"}
    return {"state": state, "latest": latest, "checked_at": datetime.now(timezone.utc).isoformat(), "dictionary_sha256": local["dictionary_sha256"], "message": messages[state]}


def update(url=DEFAULT_URL, expected_sha256=None, destination=DEFAULT_TRANSLATIONS, *, release=None, progress=None, on_installed=None):
    progress = progress or (lambda message: print(message, flush=True))
    progress("正在下载官方中文词库…")
    compressed = _download(url, MAX_ARCHIVE_BYTES)
    digest = hashlib.sha256(compressed).hexdigest()
    if expected_sha256 and digest != expected_sha256.lower():
        raise ValueError("词库压缩包校验失败，原词库未修改。")
    progress("正在解压并校验中文词库…")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
        raw = archive.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("词库内容超过大小限制，原词库未修改。")
    payload = json.loads(raw)
    names, namespaces = parse_translation_data(payload)
    destination = Path(destination)
    destination.parent.mkdir(exist_ok=True, parents=True)
    report = {
        "source_url": url, "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": digest, "entry_count": len(names), "namespace_count": len(namespaces),
        "revision": payload.get("head", {}).get("sha"),
        "dictionary_sha256": hashlib.sha256(raw).hexdigest(),
        "release_tag": (release or {}).get("tag", ""),
    }
    metadata_path = destination.with_name("tag-translations-info.json")
    progress("正在保存词库并载入新版本…")
    with tempfile.TemporaryDirectory(prefix="translation-", dir=destination.parent) as folder:
        stage = Path(folder)
        replacements = [(destination, raw), (metadata_path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))]
        originals = {}
        installed = []
        for index, (target, content) in enumerate(replacements):
            incoming = stage / f"new-{index}"
            with incoming.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            original = stage / f"old-{index}"
            if target.exists():
                shutil.copy2(target, original)
                originals[target] = original
        try:
            for index, (target, _) in enumerate(replacements):
                os.replace(stage / f"new-{index}", target)
                installed.append(target)
            if on_installed:
                on_installed()
            if destination in originals:
                os.replace(originals[destination], destination.with_suffix(".previous.json"))
        except Exception:
            for target in reversed(installed):
                if target in originals:
                    os.replace(originals[target], target)
                else:
                    target.unlink(missing_ok=True)
            if on_installed:
                try:
                    on_installed()
                except Exception:
                    # A missing/invalid original dictionary has no version to reload.
                    pass
            raise
    progress(f"中文词库已准备好，共 {len(names):,} 个词条。")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--sha256")
    parser.add_argument("--check", action="store_true", help="Check the official latest release without installing")
    options = parser.parse_args()
    if options.check:
        print(json.dumps(check_latest(), ensure_ascii=False, indent=2))
    else:
        update(options.url, options.sha256)
        print("如服务已在运行，请重新启动服务，或使用网页词库更新以立即生效。", flush=True)
