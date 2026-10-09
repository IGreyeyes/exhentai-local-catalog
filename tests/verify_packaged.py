"""Run the shipped exe with Python removed from PATH and isolated test data."""

from contextlib import closing
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from test_desktop import make_library


def verify():
    exe = PROJECT / "dist" / "ExCatalog" / "ExCatalog.exe"
    environment = os.environ.copy()
    windows = environment.get("SYSTEMROOT", environment.get("SystemRoot", r"C:\Windows"))
    environment["PATH"] = str(Path(windows) / "System32")
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    report_path = PROJECT / "logs" / "desktop-exe-check.json"
    report_path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="excatalog-desktop-check-",dir=PROJECT / "logs") as directory:
        for expected in ("extended","thumbnails"):
            result = subprocess.run([str(exe), "--verify-desktop", str(report_path), directory], env=environment, creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            expected_search = "extended" if expected == "extended" else "compact"
            if result.returncode or not report.get("passed") or not report.get("frozen") or report.get("initial_rendered_view") != expected or report.get("initial_search_rendered_view") != expected_search:
                raise AssertionError(report)
        report["desktop_restart"] = "separate private WebView2 processes restore independent search compact and record thumbnail views from the database"
    opener = build_opener(ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="packaged-service-", dir=PROJECT / "logs") as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to((PROJECT / "logs").resolve()):
            raise AssertionError("Unexpected temporary directory")
        database = make_library(root)
        before = hashlib.sha256(database.read_bytes()).digest()
        process = subprocess.Popen([str(exe), "--data-root", str(root), "--serve", "--port", "0"], env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        runtime = None
        base = None
        def get(path):
            with opener.open(base + path, timeout=10) as response:
                return json.load(response)
        def post(path, payload, token):
            request = Request(base + path, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "X-Catalog-Token":token}, method="POST")
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                markers = list((root / "logs").glob("server-*.json"))
                if markers:
                    try:
                        runtime = json.loads(markers[0].read_text(encoding="utf-8"))
                        break
                    except json.JSONDecodeError:
                        pass
                if process.poll() is not None:
                    raise RuntimeError("Packaged service failed to start")
                time.sleep(0.1)
            if runtime is None:
                raise TimeoutError("Packaged service did not start")
            base = f"http://127.0.0.1:{runtime['port']}"
            token = get("/api/status")["action_token"]
            if get("/api/search?tag=language%3Aenglish")["total"] != 1:
                raise AssertionError("Packaged search failed")
            task = post("/api/maintenance/backup", {"include_catalog":False}, token)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                state = get("/api/maintenance")
                finished = next((item for item in state.get("recent_tasks", []) if item["id"] == task["requested_id"]), None)
                if finished:
                    if finished["phase"] != "completed":
                        raise AssertionError(finished["message"])
                    break
                time.sleep(0.1)
            else:
                raise TimeoutError("Packaged backup did not complete")
            backups = list((root / "backups").glob("*/manifest.json"))
            backup = backups[0].parent
            if not (backup / "ExCatalog.exe").is_file() or not (backup / "_internal" / "python314.dll").is_file():
                raise AssertionError("Backup is missing the packaged runtime")
            stop = Request(base + "/api/shutdown", data=b"", headers={"X-Local-Token":runtime["token"]}, method="POST")
            with opener.open(stop, timeout=10) as response:
                json.load(response)
            if process.wait(timeout=10) != 0:
                raise AssertionError("Packaged service did not exit cleanly")
            if hashlib.sha256(database.read_bytes()).digest() != before:
                raise AssertionError("Packaged service modified the catalog")
            with closing(sqlite3.connect(root / "data" / "favorites.sqlite3")) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("SELECT favorite_count FROM favorites WHERE gid=1").fetchone()[0] != 123 or db.execute("SELECT state FROM record_status WHERE gid=1").fetchone()[0] != "watched":
                    raise AssertionError("Personal records changed")
            report["packaged_service"] = "search, runtime backup, graceful shutdown and record preservation passed"
            report["python_on_path"] = False
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            if process.poll() is None:
                # This is only our process with a disposable fixture, never a user's service.
                process.terminate()
                process.wait(timeout=10)
    print("Packaged WebView2 and service checks passed with Python removed from PATH.")


if __name__ == "__main__":
    verify()
