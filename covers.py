"""Lazy, cookie-free cover downloads cached on local disk."""

from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import hashlib
import os
import secrets
import sqlite3
import threading
import time

SOURCE_HOSTS = {"ehgt.org", "s.exhentai.org", "exhentai.org"}
PUBLIC_HOST = "ehgt.org"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
FAILURE_TTL = 24 * 60 * 60


class CoverUnavailable(Exception):
    pass


def normalize_cover_url(value):
    """Map known thumbnail hosts to the public thumbnail host; reject every other target."""
    if not isinstance(value, str) or len(value) > 4096:
        raise CoverUnavailable("封面地址无效。")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in SOURCE_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise CoverUnavailable("封面地址不属于允许的公开图片域名。")
    if not parsed.path.startswith("/") or parsed.path in {"", "/"}:
        raise CoverUnavailable("封面路径无效。")
    return urlunparse(("https", PUBLIC_HOST, parsed.path, "", "", ""))


def detect_image(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise CoverUnavailable("远程响应不是支持的缩略图格式。")


class PublicCoverRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or target.hostname != PUBLIC_HOST or target.username or target.password or target.port not in {None, 443}:
            raise CoverUnavailable("封面服务器跳转到了未允许的地址。")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def download_public_cover(url, opener=None):
    """Fetch a public thumbnail. The request intentionally has no Cookie or Authorization header."""
    opener = opener or build_opener(PublicCoverRedirect())
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36",
        "Accept": "image/webp,image/png,image/jpeg,image/gif;q=0.8,*/*;q=0.1",
        "Accept-Encoding": "identity",
    })
    try:
        with opener.open(request, timeout=25) as response:
            declared = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if declared and not declared.startswith("image/"):
                raise CoverUnavailable("封面服务器没有返回图片。")
            content_length = response.headers.get("Content-Length", "")
            if content_length.isdigit() and int(content_length) > MAX_IMAGE_BYTES:
                raise CoverUnavailable("封面文件超过大小限制。")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise CoverUnavailable("封面文件超过大小限制。")
        content_type, extension = detect_image(data)
        return data, content_type, extension
    except HTTPError as error:
        code = error.code
        error.close()
        raise CoverUnavailable(f"封面服务器返回 HTTP {code}。") from None
    except (URLError, TimeoutError, ConnectionError, OSError):
        raise CoverUnavailable("封面下载失败或超时。") from None


class CoverCache:
    def __init__(self, root, index_path=None, downloader=download_public_cover, interval=0.25, concurrency=3):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path or self.root.with_name("covers.sqlite3")).resolve()
        self.downloader = downloader
        self.interval = max(0.0, float(interval))
        self.slots = threading.BoundedSemaphore(max(1, int(concurrency)))
        self.rate_lock = threading.Lock()
        self.next_request_at = 0.0
        self.stripes = [threading.Lock() for _ in range(64)]
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS covers (
                    source_hash TEXT PRIMARY KEY, source_url TEXT NOT NULL,
                    relative_path TEXT, content_type TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('ok','failed')),
                    checked_at INTEGER NOT NULL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS covers_status_checked ON covers(status,checked_at DESC);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.index_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def key(url):
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _safe_path(self, relative_path):
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise CoverUnavailable("封面缓存路径无效。")
        return path

    def _cached(self, key, now):
        with self.connection() as db:
            row = db.execute("SELECT * FROM covers WHERE source_hash=?", (key,)).fetchone()
        if row is None:
            return None
        if row["status"] == "failed":
            if now - row["checked_at"] < FAILURE_TTL:
                raise CoverUnavailable(row["error"] or "封面暂时不可用。")
            return None
        path = self._safe_path(row["relative_path"])
        if not path.is_file() or path.stat().st_size != row["size_bytes"]:
            return None
        return path, row["content_type"]

    def _wait_for_slot(self):
        with self.rate_lock:
            now = time.monotonic()
            delay = max(0, self.next_request_at - now)
            if delay:
                time.sleep(delay)
            self.next_request_at = time.monotonic() + self.interval

    def get(self, source):
        url = normalize_cover_url(source)
        key = self.key(url)
        lock = self.stripes[int(key[:2], 16) % len(self.stripes)]
        with lock:
            now = int(time.time())
            cached = self._cached(key, now)
            if cached:
                return cached
            try:
                with self.slots:
                    self._wait_for_slot()
                    data, content_type, extension = self.downloader(url)
                relative = Path(key[:2]) / f"{key}.{extension}"
                destination = self._safe_path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + "." + secrets.token_hex(4) + ".partial")
                try:
                    with temporary.open("xb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, destination)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                with self.connection() as db:
                    db.execute("""
                        INSERT INTO covers VALUES (?,?,?,?,?,'ok',?,'')
                        ON CONFLICT(source_hash) DO UPDATE SET source_url=excluded.source_url,
                        relative_path=excluded.relative_path,content_type=excluded.content_type,
                        size_bytes=excluded.size_bytes,status='ok',checked_at=excluded.checked_at,error=''
                    """, (key, url, str(relative), content_type, len(data), now))
                return destination, content_type
            except CoverUnavailable as error:
                with self.connection() as db:
                    db.execute("""
                        INSERT INTO covers(source_hash,source_url,status,checked_at,error) VALUES (?,?,'failed',?,?)
                        ON CONFLICT(source_hash) DO UPDATE SET source_url=excluded.source_url,status='failed',
                        checked_at=excluded.checked_at,error=excluded.error
                    """, (key, url, now, str(error)))
                raise

    def stats(self):
        with self.connection() as db:
            row = db.execute("SELECT SUM(status='ok'),COALESCE(SUM(CASE WHEN status='ok' THEN size_bytes ELSE 0 END),0),SUM(status='failed'),MAX(CASE WHEN status='ok' THEN checked_at END) FROM covers").fetchone()
        return {"cached": row[0] or 0, "size_bytes": row[1] or 0, "failed": row[2] or 0, "last_cached_at": row[3], "directory": str(self.root), "uses_credentials": False, "public_host": PUBLIC_HOST}
