"""Run the shipped exe with Python removed from PATH and isolated test data."""

from contextlib import closing
from compression import zstd
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from test_desktop import make_library
from client_version import APP_VERSION


def verify(program_directory=None):
    exe = (Path(program_directory) if program_directory else PROJECT / "dist" / "ExCatalog") / "ExCatalog.exe"
    environment = os.environ.copy()
    windows = environment.get("SYSTEMROOT", environment.get("SystemRoot", r"C:\Windows"))
    environment["PATH"] = str(Path(windows) / "System32")
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    report_path = PROJECT / "logs" / "desktop-exe-check.json"
    report_path.parent.mkdir(parents=True,exist_ok=True)
    first_run_report = report_path.with_name("desktop-first-run-check.json")
    result = subprocess.run([str(exe), "--verify-first-run", str(first_run_report)], env=environment, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    first_run = json.loads(first_run_report.read_text(encoding="utf-8"))
    if result.returncode or not first_run.get("passed") or not first_run.get("frozen"):
        raise AssertionError(first_run)
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
        def wait_task(task):
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                state=get('/api/maintenance')
                finished=next((item for item in state.get('recent_tasks',[]) if item['id']==task['requested_id']),None)
                if finished:
                    if finished['phase']!='completed':raise AssertionError(finished['message'])
                    return state
                time.sleep(0.1)
            raise TimeoutError('Packaged maintenance did not complete')
        def archive_with_new_record(gid):
            incoming=root/f'incoming-{gid}.sqlite3'
            shutil.copy2(database,incoming)
            with closing(sqlite3.connect(incoming)) as db:
                db.execute("INSERT INTO gallery VALUES (?,'abcdef0123','Packaged update test','','Non-H',?,10,'4.0',0,0,0)",(gid,gid))
                db.execute('INSERT INTO gid_tid VALUES(?,1)',(gid,))
                db.commit()
            (root/'e-hentai.db.zstd').write_bytes(zstd.compress(incoming.read_bytes()))
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
            network = get("/api/network")
            if network["enabled"] or network["has_password"] or network["fallback"] != "system":
                raise AssertionError("Packaged proxy must start with system defaults and no credentials")
            settings = {key: network[key] for key in ("enabled", "fallback", "host", "port", "username", "domains")}
            settings.update(fallback="direct", password="test-packaged-proxy-password", username="test-packaged-proxy-user")
            saved_network = post("/api/network/settings", settings, token)
            if "password" in saved_network or not saved_network["has_password"] or saved_network["fallback"] != "direct":
                raise AssertionError("Packaged proxy persistence or password redaction failed")
            encrypted = (root / "data" / "network-settings.ehproxy").read_bytes()
            if b"test-packaged-proxy-password" in encrypted or b"test-packaged-proxy-user" in encrypted:
                raise AssertionError("Packaged proxy preferences were not encrypted")
            for asset in ("network-settings.js", "network-settings.css", "cover-retry.js"):
                with opener.open(base + "/" + asset, timeout=10) as response:
                    if response.status != 200 or not response.read():
                        raise AssertionError("Packaged network or cover retry asset missing")
            if post("/api/covers/retry", {}, token) != {"cleared": 0, "waiting": 0}:
                raise AssertionError("Packaged cover retry API failed")
            for page in ("/", "/records", "/maintenance"):
                with opener.open(base + page, timeout=10) as response:
                    page_body = response.read()
                    if b'id="cover-retry"' not in page_body:
                        raise AssertionError("Packaged cover retry button missing")
                    if page == "/maintenance" and (b'value="cover"' not in page_body or b'id="network-cover-url"' not in page_body):
                        raise AssertionError("Packaged cover connection test is missing")
            report["cover_retry"] = "retry API, script and all three page controls included"
            cloud = get("/api/cloud-sync")
            if cloud["enabled"] or cloud["upload_enabled"] or cloud["source_id"]:
                raise AssertionError("Packaged cloud sync must start disabled without credentials")
            if cloud["download_enabled"] or cloud["configured"]:
                raise AssertionError("Fresh client should wait for explicit sync settings")
            if cloud.get("session_upload_processed") != 0 or cloud.get("next_sync_at") is not None:
                raise AssertionError("Packaged sync progress or scheduling status is missing")
            with opener.open(base+"/cloud-sync.js",timeout=10) as response:
                if response.status != 200 or b"/api/cloud-sync/" not in response.read():
                    raise AssertionError("Packaged cloud sync UI script is missing")
            with opener.open(base+"/maintenance",timeout=10) as response:
                maintenance=response.read()
                if b'id="cloud-sync-source-state"' not in maintenance:
                    raise AssertionError("Packaged sync progress display is missing")
                if maintenance.index(b'id="cloud-sync-section"')>maintenance.index(b'id="backup-section"'):
                    raise AssertionError("Upload section must appear above backups")
            with opener.open(base+"/contact",timeout=10) as response:
                contact=response.read()
                if b'id="contact-qq-qr"' not in contact or b'1127148424' not in contact:
                    raise AssertionError("Packaged contact page is missing")
            with opener.open(base+"/changelog",timeout=10) as response:
                history_page=response.read()
                if b'id="history-list"' not in history_page or b'/release-notes.js' not in history_page:
                    raise AssertionError("Packaged complete release history page is missing")
            history=get('/api/release-history')
            if history['current_version'] != APP_VERSION:
                raise AssertionError('Packaged client version does not match this release')
            configured=post('/api/collector/settings',{'host':'e-hentai.org','collection_mode':'rating_full','rating_priority':False,'skip_existing':True},token)
            if configured['collection_mode']!='rating_full' or not configured['rating_priority']:
                raise AssertionError('Packaged full-library rating mode is unavailable')
            preview=post('/api/collector/preview',{},token)
            if preview['query'].get('scope')!=['library'] or (preview['total'],preview['pending'],preview['skipped'])!=(1,0,1):
                raise AssertionError('Packaged library preview must work without a search and honor cached data')
            completed=post('/api/collector/start',{'preview_id':preview['preview_id']},token)
            if completed['job']['state']!='completed' or completed['job']['cached']!=1:
                raise AssertionError('Packaged cached-only library plan should complete without source requests')
            report['rating_library']='empty-query full-library preview, fixed rating order and cached-only confirmation passed'
            if not any(item['version']=='1.1.10' for item in history['entries']) or not any(item['version']==history['current_version'] for item in history['entries']):
                raise AssertionError("Packaged offline history is incomplete")
            with opener.open(base+"/qq-group-qr.png",timeout=10) as response:
                if response.headers.get_content_type()!='image/png' or response.read()!=(PROJECT/'static'/'qq-group-qr.png').read_bytes():
                    raise AssertionError("Packaged QQ QR image differs from the supplied asset")
            if get("/api/search?tag=language%3Aenglish")["total"] != 1:
                raise AssertionError("Packaged search failed")
            or_result = get("/api/search?tag=language%3Aenglish&tag_mode=or")
            if or_result['total'] != 1 or or_result['tag_mode'] != 'or':
                raise AssertionError('Packaged OR search failed')
            interval_settings = post('/api/collector/settings', {'host':'e-hentai.org','interval':0.75}, token)
            if interval_settings['interval'] != 0.75 or get('/api/collector')['interval'] != 0.75:
                raise AssertionError('Packaged custom collection interval failed')
            for asset, marker in (('app.js', b'catalog-search-session-v1'), ('collector.js', b'matchesSavedTask'), ('', b'id="collector-custom-interval"')):
                with opener.open(base+'/'+asset, timeout=10) as response:
                    if marker not in response.read():
                        raise AssertionError('Packaged search recovery or custom interval asset missing')
            report['search_workflow'] = 'OR search, custom interval and search/task recovery assets included'
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
            if hashlib.sha256(database.read_bytes()).digest()!=before:
                raise AssertionError('Packaged search or backup modified the catalog')
            for gid,create_backup in ((2,False),(3,True)):
                archive_with_new_record(gid)
                state=wait_task(post('/api/maintenance/prepare',{},token))
                state=wait_task(post('/api/maintenance/apply',{'prepared_id':state['prepared']['id'],'create_backup':create_backup},token))
                if state['catalog_info']['gallery_count']!=gid or get('/api/search?tag=language%3Aenglish')['total']!=gid:
                    raise AssertionError('Packaged catalog update did not become active')
                if state['backup_summary']['total']!=(2 if create_backup else 1):
                    raise AssertionError('Packaged backup inventory is wrong')
                saved=state['result']['backup']
                if bool(saved)!=create_backup:raise AssertionError('Packaged backup choice was not respected')
                if saved:
                    with closing(sqlite3.connect(Path(saved['path'])/'data/catalog.sqlite3')) as db:
                        if db.execute('SELECT COUNT(*) FROM gallery').fetchone()[0]!=2:
                            raise AssertionError('Protection backup does not contain the previous catalog')
                state=wait_task(post('/api/maintenance/prepare',{},token))
                last=next(task for task in reversed(state['catalog_tasks']) if task['kind']=='apply')
                if not state['result']['same_archive'] or last['result']['backup']!=saved:
                    raise AssertionError('Recheck lost the packaged application result')
            snapshot = post('/api/maintenance/favorites-export', {}, token)
            snapshot['records'].append({'gid':999, 'favorite_count':456, 'checked_at':1712345678, 'source':'e-hentai.org', 'collector_id':'', 'collector_name':''})
            request = Request(base+'/api/maintenance/favorites-prepare', data=json.dumps(snapshot).encode(), headers={'Content-Type':'application/octet-stream', 'X-Catalog-Token':token}, method='POST')
            with opener.open(request, timeout=10) as response:
                task = json.load(response)
            state = wait_task(task)
            state = wait_task(post('/api/maintenance/favorites-import', {'prepared_id':state['prepared_favorites']['id']}, token))
            imported = state['result']
            if imported['summary']['added'] != 1 or imported['backup']['include_catalog']:
                raise AssertionError('Packaged favorites import did not create a light protection backup')
            with closing(sqlite3.connect(Path(imported['backup']['path'])/'data/favorites.sqlite3')) as db:
                if db.execute('SELECT COUNT(*) FROM favorites WHERE gid=999').fetchone()[0] != 0 or db.execute('SELECT favorite_count FROM favorites WHERE gid=1').fetchone()[0] != 123:
                    raise AssertionError('Import backup must contain the original favorites before merging')
            stop = Request(base + "/api/shutdown", data=b"", headers={"X-Local-Token":runtime["token"]}, method="POST")
            with opener.open(stop, timeout=10) as response:
                json.load(response)
            if process.wait(timeout=10) != 0:
                raise AssertionError("Packaged service did not exit cleanly")
            with closing(sqlite3.connect(database)) as db:
                if db.execute('SELECT COUNT(*) FROM gallery').fetchone()[0]!=3:
                    raise AssertionError('Packaged catalog update was not saved')
            with closing(sqlite3.connect(root / "data" / "favorites.sqlite3")) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("SELECT favorite_count FROM favorites WHERE gid=1").fetchone()[0] != 123 or db.execute("SELECT state FROM record_status WHERE gid=1").fetchone()[0] != "watched":
                    raise AssertionError("Personal records changed")
                if db.execute('SELECT favorite_count,checked_at FROM favorites WHERE gid=999').fetchone() != (456,1712345678):
                    raise AssertionError('Packaged favorites import did not persist the new count and original timestamp')
            report["packaged_service"] = "version, search, runtime backup, direct and full-backup catalog updates, inventory, recheck results, favorites import with protection backup, graceful shutdown and record preservation passed"
            report["python_on_path"] = False
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            if process.poll() is None:
                # This is only our process with a disposable fixture, never a user's service.
                process.terminate()
                process.wait(timeout=10)
            if sys.exception() is not None:
                for name in ('desktop-output.log','desktop.log'):
                    diagnostic=root/'logs'/name
                    if diagnostic.is_file():
                        shutil.copy2(diagnostic,PROJECT/'logs'/('packaged-check-failure-'+name))
    print("Packaged WebView2 and service checks passed with Python removed from PATH.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-directory", type=Path)
    verify(parser.parse_args().program_directory)
