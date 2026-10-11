"""Bundled public release notes and optional GitHub history, without personal data."""
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
import json
import re
import threading

from client_version import APP_VERSION, RELEASE_NOTES, RELEASE_TITLE, REPOSITORY
from runtime_paths import resource_root

RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
VERSION_PATTERN = r"v?(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?"


def version_key(version):
    match = re.fullmatch(VERSION_PATTERN, str(version))
    if not match:
        raise ValueError("更新日志版本号格式不正确。")
    major, minor, patch, suffix = match.groups()
    suffix_key = tuple((0, int(part)) if part.isdecimal() else (1, part.lower())
                       for part in re.split(r"(\d+)", suffix or "") if part)
    return (int(major), int(minor), int(patch), not bool(suffix), suffix_key)


def version_sections(notes):
    matches = list(re.finditer(r"^#{2,6}\s+(v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\b[^\n]*\n", notes, re.M))
    result = {}
    for index, match in enumerate(matches):
        content = notes[match.end():matches[index+1].start() if index+1<len(matches) else len(notes)]
        result[match[1].removeprefix("v")] = re.split(r"^##\s+历史更新概览\s*$", content, maxsplit=1, flags=re.M)[0].strip()
    return result


def changes_for(notes, version):
    scoped = version_sections(notes).get(version, notes)
    changes = "\n".join(line for line in scoped.splitlines() if line.startswith("- "))
    return changes or scoped


def bundled_history():
    entries = json.loads((resource_root() / "static" / "release-history.json").read_text(encoding="utf-8"))
    entries = [dict(item) for item in entries]
    entries = [item for item in entries if item["version"] != APP_VERSION]
    entries.append({"version": APP_VERSION, "title": RELEASE_TITLE, "date": None,
                    "prerelease": False, "notes": RELEASE_NOTES, "changes": changes_for(RELEASE_NOTES, APP_VERSION),
                    "release_url": RELEASES_URL, "source": "bundled"})
    return sorted(entries, key=lambda item: version_key(item["version"]), reverse=True)


def upgrade_notes(entries, current, target, target_notes):
    current_key, target_key = version_key(current), version_key(target)
    selected = {item["version"]: item for item in entries if not item.get("prerelease")
                and current_key < version_key(item["version"]) <= target_key}
    if target not in selected:
        selected[target] = {"version": target, "title": "更新内容", "changes": changes_for(target_notes, target)}
    ordered = sorted(selected.values(), key=lambda item: version_key(item["version"]), reverse=True)
    text = f"从 v{current} 升级到 v{target}：以下包含 {len(ordered)} 个版本的更新。\n\n"
    text += "\n\n".join(f"## v{item['version']} · {item['title']}\n\n{item.get('changes') or item['notes']}" for item in ordered)
    return text, [item["version"] for item in ordered]


class ReleaseHistory:
    def __init__(self):
        self.entries = bundled_history()
        self.lock = threading.Lock()
        self.refreshed = False

    def info(self):
        return {"current_version": APP_VERSION, "entries": self.entries, "releases_url": RELEASES_URL,
                "refreshed": self.refreshed}

    def _request(self, url):
        request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ExCatalog/" + APP_VERSION})
        for index in range(2):
            try:
                with build_opener(ProxyHandler() if not index else ProxyHandler({})).open(request, timeout=15) as response:
                    raw = response.read(3 * 1024**2 + 1)
                if len(raw) > 3 * 1024**2:
                    raise ValueError("发布历史超过大小限制。")
                return json.loads(raw)
            except HTTPError as error:
                error.close()
                raise ValueError("GitHub 发布历史暂不可用，请稍后刷新；内置更新日志仍可查看。") from None
            except (URLError, OSError, TimeoutError):
                if index:
                    raise ValueError("无法连接 GitHub，内置更新日志仍可查看。") from None

    def refresh(self, requester=None):
        if not self.lock.acquire(blocking=False):
            raise ValueError("正在读取发布历史，请稍候。")
        try:
            request = requester or self._request
            merged = {item["version"]: item for item in self.entries}
            for page in range(1, 11):
                document = request(f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100&page={page}")
                if not isinstance(document, list) or len(document) > 100:
                    raise ValueError("发布历史格式不正确，内置日志已保留。")
                for item in document:
                    if not isinstance(item, dict) or item.get("draft"):
                        continue
                    tag = item.get("tag_name", "")
                    try:
                        key = version_key(tag)
                    except ValueError:
                        continue
                    version = tag.removeprefix("v")
                    raw_notes = str(item.get("body") or "暂无单独的更新说明。")[:160000]
                    notes = version_sections(raw_notes).get(version, raw_notes)
                    old = merged.get(version, {})
                    title = re.sub(r"^v?" + re.escape(version) + r"\s*[·：:]?\s*", "", str(item.get("name") or version))[:160]
                    merged[version] = {"version": version, "title": title or "更新内容", "date": item.get("published_at"),
                        "prerelease": bool(item.get("prerelease") or not key[3]), "notes": notes,
                        "changes": old.get("changes") if version == "1.1.17" else changes_for(raw_notes, version),
                        "release_url": RELEASES_URL + "/tag/" + quote(tag, safe=""), "source": "github"}
                if len(document) < 100:
                    break
            else:
                raise ValueError("发布历史分页过多，请在 GitHub 查看全部发布。")
            self.entries = sorted(merged.values(), key=lambda item: version_key(item["version"]), reverse=True)
            self.refreshed = True
            return self.info()
        finally:
            self.lock.release()
