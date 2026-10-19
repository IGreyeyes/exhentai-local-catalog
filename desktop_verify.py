"""Exercise the actual packaged WebView2 against an isolated miniature library."""

from contextlib import closing, nullcontext, redirect_stdout
from pathlib import Path
import base64
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import faulthandler
import traceback


def verify_first_run(report_path):
    """Check the real startup window, missing-file retry, and archive preparation."""
    import webview
    from compression import zstd
    from unittest.mock import patch
    from desktop import open_desktop
    from first_run import CATALOG_DOWNLOAD_URL
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    original_create, original_start = webview.create_window, webview.start
    windows = []
    opened = []
    def create(*args, **kwargs):
        kwargs["hidden"] = True
        window = original_create(*args, **kwargs)
        windows.append((window, kwargs["js_api"]))
        return window

    with tempfile.TemporaryDirectory(prefix="excatalog-first-run-") as directory:
        root = Path(directory)
        def start_with_check(start, **kwargs):
            def check():
                window, api = windows[0]
                try:
                    start()
                    if not window.events.loaded.wait(20):
                        raise TimeoutError("Missing catalog page did not load")
                    state = window.evaluate_js("({title:document.querySelector('h1').textContent,waiting:!document.querySelector('progress').hidden,path:document.querySelector('.directory').textContent,buttons:document.querySelectorAll('.actions button').length})")
                    assert state == {"title": "未找到数据库", "waiting": False, "path": str(root.resolve()), "buttons": 3}, state
                    assert api._session.server is None
                    report["checks"].append("missing database immediately shows instructions, actual directory, and three actions without loading indefinitely")
                    assert api.open_catalog_download().get("opened")
                    assert api.open_catalog_directory().get("opened")
                    assert opened == [CATALOG_DOWNLOAD_URL, root]
                    reloaded = threading.Event()
                    window.events.loaded += reloaded.set
                    assert api.retry_startup().get("starting")
                    if not reloaded.wait(20):
                        raise TimeoutError("Missing-file retry did not finish")
                    assert window.evaluate_js("document.querySelector('h1').textContent") == "未找到数据库"
                    report["checks"].append("download and directory actions use fixed targets; retry with no file returns to instructions")
                    source = root / "test-source.sqlite3"
                    with closing(sqlite3.connect(source)) as db:
                        db.executescript("""
                            CREATE TABLE gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                            CREATE TABLE tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                            CREATE TABLE gid_tid(gid INTEGER,tid INTEGER);
                            INSERT INTO gallery VALUES(1,'abcdef0123','First run test','','Non-H',1,10,'4.0',0,0,0);
                            INSERT INTO tag VALUES(1,'language:english');
                            INSERT INTO gid_tid VALUES(1,1);
                        """)
                    (root / "e-hentai.db.zstd").write_bytes(zstd.compress(source.read_bytes()))
                    (root / "data").mkdir(exist_ok=True)
                    (root / "data" / "tag-translations.json").write_text('{"data":[]}', encoding="utf-8")
                    reloaded.clear()
                    assert api.retry_startup().get("starting")
                    if not reloaded.wait(25):
                        raise TimeoutError("Startup did not continue after adding database")
                    assert window.evaluate_js("!!document.querySelector('#search-form')")
                    assert (root / "data" / "catalog.sqlite3").is_file()
                    assert api._session.server is not None
                    assert "error" in api.retry_startup(), "Main page must not restart the service"
                    report["checks"].append("adding a valid archive and retrying extracts and indexes it, then opens the search page without restarting the application")
                    report["passed"] = True
                except Exception:
                    report["error"] = traceback.format_exc()
                finally:
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    window.destroy()
            return original_start(check, **kwargs)
        with patch("webview.create_window", side_effect=create), patch("webview.start", side_effect=start_with_check), \
             patch("desktop.webbrowser.open", side_effect=lambda target: opened.append(target) or True), \
             patch("desktop.os.startfile", side_effect=lambda target: opened.append(target)), \
             patch("desktop.ClientUpdater.start_startup_check", return_value={"phase": "failed", "message": "Isolated check"}):
            open_desktop(root)
    return 0 if report["passed"] else 1


def verify_desktop(report_path, fixture_root=None):
    import webview
    from desktop import DesktopApi
    from desktop_service import DesktopSession
    from initialize_catalog import initialize
    from client_version import APP_VERSION
    from unittest.mock import patch
    from cloud_sync import CloudSync, FORMAT, VERSION
    from urllib.parse import parse_qs, urlsplit
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"passed": False, "frozen": bool(getattr(sys, "frozen", False)), "python": sys.version.split()[0], "checks": []}
    trace_file = report_path.with_suffix(".stack.txt").open("w", encoding="utf-8")
    faulthandler.dump_traceback_later(25, file=trace_file)
    def stage(value):
        report["stage"] = value
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if fixture_root is not None and not fixture_root.resolve().name.startswith("excatalog-desktop-check-"):
        raise ValueError("隔离验证只接受专用临时测试资料目录。")
    fixture = nullcontext(str(fixture_root.resolve())) if fixture_root else tempfile.TemporaryDirectory(prefix="excatalog-desktop-check-")
    with fixture as directory:
        root = Path(directory)
        data = root / "data"
        data.mkdir(parents=True, exist_ok=True)
        import tkinter
        picker = tkinter.Tk()
        picker.withdraw()
        picker.destroy()
        report["checks"].append("bundled Tk file-dialog runtime initializes")
        database = data / "catalog.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS gallery(gid INTEGER PRIMARY KEY,token TEXT,title TEXT,title_jpn TEXT,category TEXT,posted INTEGER,filecount INTEGER,rating TEXT,removed INTEGER DEFAULT 0,replaced INTEGER DEFAULT 0,expunged INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tag(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS gid_tid(gid INTEGER,tid INTEGER,PRIMARY KEY(gid,tid));
                INSERT OR IGNORE INTO gallery VALUES(1,'abcdef0123','Desktop verification','','Non-H',1,10,'4.0',0,0,0);
                INSERT OR IGNORE INTO tag VALUES(1,'language:english');
                INSERT OR IGNORE INTO gid_tid VALUES(1,1);
            """)
        with redirect_stdout(io.StringIO()):
            initialize(database)
        (data / "tag-translations.json").write_text(json.dumps({"data":[{"namespace":"language","data":{"english":{"name":"英语"}}}]}), encoding="utf-8")
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        session = DesktopSession(root)
        cloud_submissions=[]
        cloud_names=[]
        cloud_public_snapshot=None
        def mock_cloud(endpoint,path,payload=None,token=""):
            if path=="/health":return {"version":VERSION,"max_upload_batch":50,"upload_preflight":True,"max_upload_check_batch":500,"public_correction_state":True,"upload_min_interval_days":14}
            if path.startswith('/v3/upload-state?'):
                dataset=parse_qs(urlsplit(path).query).get('dataset_id',[''])[0]
                return {"version":VERSION,"dataset_id":"0"*32,"reset":dataset!="0"*32,"cursor":1,"until":1,"has_more":False,"corrections":[]}
            if path=="/v3/upload-check":
                return {"version":VERSION,"dataset_id":"0"*32,"source_id":"00000000-0000-4000-8000-000000000002","status":"review",
                    "records":[{"gid":r["gid"],"skip":r["gid"]==1,"revision":1 if r["gid"]==1 else 0} for r in payload["records"]]}
            if path=="/v3/register":
                if "uploader_name" in payload:cloud_names.append(payload["uploader_name"])
                return {"version":VERSION,"source_id":"00000000-0000-4000-8000-000000000002","status":"review"}
            if payload is not None:
                cloud_submissions.extend(dict(item) for item in payload["records"])
                return {"version":VERSION,"accepted":0,"pending":len(payload["records"]),"kept":0}
            after=int(parse_qs(urlsplit(path).query).get("after",["0"])[0])
            return {"format":FORMAT,"version":VERSION,"dataset_id":"0"*32,
                    "cursor":1,"until":1,"head":1,"has_more":False,
                    "records":[] if after else [{"gid":1,"revision":1,"action":"snapshot","snapshot":cloud_public_snapshot}]}
        class VerifiedCloudSync(CloudSync):
            def __init__(self,*args,**kwargs):
                kwargs["transport"]=mock_cloud
                super().__init__(*args,**kwargs)
        with patch("app.CloudSync",VerifiedCloudSync):
            url = session.start(lambda message: None)
        session.server.catalog.favorites.save(1, 123, "exhentai.org", 30)
        with session.server.catalog.favorites.connection() as db:cloud_public_snapshot=session.server.cloud_sync._read_snapshot(db,1)
        api = DesktopApi(session)
        startup_requests=[]
        def mock_release(request,timeout):
            startup_requests.append(request.full_url)
            return io.BytesIO(json.dumps({"tag_name":APP_VERSION,"body":"隔离验证：当前版本已是最新版。"}).encode())
        api._updates.opener.open=mock_release
        notes_pending = api._updates.info()["show_notes"]
        expected_view = session.server.catalog.favorites.get_record_view() or "extended"
        report["initial_saved_view"] = expected_view
        expected_search_view = session.server.catalog.favorites.get_search_view() or "extended"
        window = webview.create_window("桌面版隔离验证", url=url, js_api=api, width=1360, height=900, hidden=True)
        api._window = window

        def promise(script):
            event = threading.Event()
            value = []
            def done(result):
                value.append(result)
                event.set()
            window.evaluate_js(script, callback=done)
            if not event.wait(20):
                raise TimeoutError("WebView2 JavaScript verification timed out")
            return value[0]

        def capture_page(destination):
            from System import Action, Func, Object, String
            from System.Threading.Tasks import Task
            form = window.gui.BrowserView.instances[window.uid]
            task = form.Invoke(Func[Object](lambda: form.browser.webview.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.captureScreenshot", '{"format":"png","captureBeyondViewport":false}')))
            captured = threading.Event()
            captures = []
            def completed_capture(completed):
                captures.append(str(completed.Result))
                captured.set()
            task.ContinueWith(Action[Task[String]](completed_capture))
            if not captured.wait(10):
                raise TimeoutError("WebView2 screenshot timed out")
            destination.write_bytes(base64.b64decode(json.loads(captures[0])["data"]))

        def check():
            try:
                stage("waiting for WebView2 page")
                if not window.events.loaded.wait(30):
                    raise TimeoutError("WebView2 did not load")
                report["renderer"] = window.gui.renderer
                if report["renderer"] != "edgechromium":
                    raise AssertionError("Embedded renderer is not WebView2")
                for page, selector in (("/", "#search-form"), ("/records", "#records-list"), ("/contact", "#contact-qq-qr"), ("/maintenance", "#favorites-export")):
                    stage("loading " + page)
                    if page != "/":
                        window.events.loaded.clear()
                        window.load_url(url + page)
                        if not window.events.loaded.wait(30):
                            raise TimeoutError("Page did not load: " + page)
                    state = window.evaluate_js("({title:document.title, content:!!document.querySelector(" + json.dumps(selector) + "), bridge:typeof window.pywebview?.api?.save_attachment, platform:typeof window.catalogSaveAttachment})")
                    if not state["content"] or state["bridge"] != "function" or state["platform"] != "function":
                        raise AssertionError(state)
                    report["checks"].append({"page": page, **state})
                    if page == "/contact":
                        contact = promise("document.querySelector('#contact-qq-qr').decode().then(()=>{const image=document.querySelector('#contact-qq-qr'),link=document.querySelector('#contact-github');return {width:image.naturalWidth,height:image.naturalHeight,url:link.href,target:link.target,selected:document.querySelector('.nav-contact').getAttribute('aria-current'),overflow:document.documentElement.scrollWidth>innerWidth+1};})")
                        if (contact["width"] != 840 or contact["height"] != 840
                                or contact["url"] != "https://github.com/IGreyeyes/exhentai-local-catalog"
                                or contact["target"] != "_blank" or contact["selected"] != "page" or contact["overflow"]):
                            raise AssertionError(contact)
                        report["checks"].append("contact page, local QQ QR image, GitHub destination and selected navigation render in packaged WebView2")
                    client = promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(document.querySelector('#client-check-update')){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Update UI did not initialize'));}},50);})")
                    if not client:
                        raise AssertionError("Client update controls missing")
                    if page == "/" and notes_pending:
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(document.querySelector('#client-update-dialog[open]')){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Update notes did not open'));}},50);})")
                        styled = window.evaluate_js("(()=>{const dialog=document.querySelector('#client-update-dialog'),notes=dialog.querySelector('.client-release-notes');return {width:dialog.getBoundingClientRect().width,font:getComputedStyle(notes).fontSize,items:notes.querySelectorAll('li').length,highlights:notes.querySelectorAll('strong').length,display:getComputedStyle(dialog).display};})()")
                        if not 620 <= styled["width"] <= 680 or styled["font"] != "14px" or styled["items"] < 1 or styled["display"] != "flex":
                            raise AssertionError(styled)
                        report["checks"].append({"styled_update_dialog":styled})
                        asset = promise("fetch('/client-updates.css').then(r=>({status:r.status,type:r.headers.get('Content-Type')}))")
                        if asset["status"] != 200 or not asset["type"].startswith("text/css"):
                            raise AssertionError(asset)
                        result = promise("window.pywebview.api.client_info()")
                        if not result.get("show_notes"):
                            raise AssertionError("New version notes were not pending")
                        guide_loaded=threading.Event()
                        def guide_navigation():guide_loaded.set()
                        window.events.loaded+=guide_navigation
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{const button=document.querySelector('#client-notes-read');if(button&&!button.disabled){clearInterval(timer);button.click();resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Read-notes button stayed disabled'));}},50);})")
                        if not guide_loaded.wait(20):
                            diagnostic=window.evaluate_js("({path:location.href,message:document.querySelector('#client-update-message')?.textContent,open:document.querySelector('#client-update-dialog')?.open,guide:!!document.querySelector('#cloud-upload-guide'),disabled:document.querySelector('#client-notes-read')?.disabled,handler:typeof document.querySelector('#client-notes-read')?.onclick})")
                            raise TimeoutError("Reading notes did not navigate to upload guide: " + json.dumps(diagnostic,ensure_ascii=False))
                        window.events.loaded-=guide_navigation
                        guide=promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(async()=>{const section=document.querySelector('#cloud-sync-section'),save=document.querySelector('#cloud-sync-save');if(section&&save&&!save.disabled){const info=await window.pywebview.api.client_info();if(!info.upload_guide_url){clearInterval(timer);resolve({path:location.pathname,guide:!document.querySelector('#cloud-upload-guide').hidden,first:document.querySelector('.maintenance-card').id,upload:document.querySelector('#cloud-sync-upload').checked,download:document.querySelector('#cloud-sync-enabled').checked,consent:document.querySelector('#cloud-sync-consent').checked});return;}}if(++tries>120){clearInterval(timer);reject(new Error('Upload guide did not initialize'));}},50);})")
                        if guide!={"path":"/maintenance","guide":True,"first":"cloud-sync-section","upload":False,"download":False,"consent":False}:
                            raise AssertionError(guide)
                        if session.server.cloud_sync.status()["enabled"]:
                            raise AssertionError("Visiting the guide activated sharing before consent")
                        report["checks"].append("read-notes action automatically navigates to top upload section; upload, download and consent start unchecked, guide persists as viewed")
                        window.evaluate_js("document.querySelector('#cloud-upload-guide-done').click()")
                        window.events.loaded.clear()
                        window.load_url(url)
                        if not window.events.loaded.wait(20):raise TimeoutError("Could not return from upload guide")
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(document.querySelector('#client-check-update')){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Update controls did not reload'));}},50);})")
                        if promise("window.pywebview.api.client_info()").get("upload_guide_url"):
                            raise AssertionError("Completed upload guide was repeated")
                    else:
                        result = promise("window.pywebview.api.client_info()")
                        if result.get("show_notes"):
                            raise AssertionError("Read notes were shown again after navigation")
                    if page == "/maintenance":
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{const button=document.querySelector('#cloud-sync-save');if(button&&!button.disabled){clearInterval(timer);resolve(true);}else if(++tries>150){clearInterval(timer);reject(new Error('Sync settings did not load'));}},50);})")
                        saved=session.server.cloud_sync.status()
                        if window.evaluate_js("document.querySelector('#cloud-sync-name').value") != saved["uploader_name"]:
                            raise AssertionError("Saved uploader nickname was not rendered")
                        choices=window.evaluate_js("({upload:document.querySelector('#cloud-sync-upload').checked,download:document.querySelector('#cloud-sync-enabled').checked})")
                        if choices!={"upload":saved["upload_enabled"],"download":saved["download_enabled"]}:
                            raise AssertionError("Saved sync choices were not rendered: " + json.dumps(choices))
                        if not saved["configured"]:
                            window.evaluate_js("const name=document.getElementById('cloud-sync-name');name.value='测试上传者';name.dispatchEvent(new Event('input'));['cloud-sync-upload','cloud-sync-enabled','cloud-sync-consent'].forEach(id=>{const input=document.getElementById(id);input.checked=true;input.dispatchEvent(new Event('input'));});document.getElementById('cloud-sync-save').click()")
                            promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(async()=>{const state=await fetch('/api/cloud-sync').then(r=>r.json());if(state.configured&&state.upload_enabled&&state.download_enabled&&!state.busy){clearInterval(timer);resolve(true);}else if(++tries>200){clearInterval(timer);reject(new Error('Both sync choices did not save'));}},50);})")
                            settings_loaded=threading.Event()
                            def settings_navigation():settings_loaded.set()
                            window.events.loaded+=settings_navigation
                            window.load_url(url + "/maintenance?settings-view=1")
                            if not settings_loaded.wait(20):raise TimeoutError("Saved sync settings did not reload")
                            window.events.loaded-=settings_navigation
                            promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{const button=document.querySelector('#cloud-sync-save');if(button&&!button.disabled){clearInterval(timer);resolve(true);}else if(++tries>150){clearInterval(timer);reject(new Error('Saved settings page did not initialize'));}},50);})")
                            choices=window.evaluate_js("({upload:document.querySelector('#cloud-sync-upload').checked,download:document.querySelector('#cloud-sync-enabled').checked})")
                            if choices!={"upload":True,"download":True}:raise AssertionError(choices)
                            if window.evaluate_js("document.querySelector('#cloud-sync-name').value")!='测试上传者' or '测试上传者' not in cloud_names:
                                raise AssertionError("Uploader nickname did not save, register and reload")
                            report["checks"].append("both sync choices saved through GUI and remain checked after page reload")
                        else:
                            if choices!={"upload":True,"download":True}:raise AssertionError(choices)
                            if saved["uploader_name"]!='测试上传者':raise AssertionError("Uploader nickname was lost after process restart")
                            report["checks"].append("uploader nickname persists through GUI save, reload and a separate EXE process restart")
                            report["checks"].append("new client process reopens saved upload and download choices as checked")
                    if page == "/":
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(!document.querySelector('#search-view').disabled){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Search view did not initialize'));}},50);})")
                        actual_view = window.evaluate_js("document.querySelector('#search-view').value")
                        if actual_view != expected_search_view:
                            raise AssertionError({"expected_search":expected_search_view,"actual":actual_view})
                        report["initial_search_rendered_view"] = actual_view
                        window.evaluate_js("document.querySelector('#sort').value='newest';document.querySelector('#tag-input').value='language:english';document.querySelector('#search-form').requestSubmit()")
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(document.querySelector('#results .saved-card')&&document.querySelector('#results').getAttribute('aria-busy')==='false'){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Search results did not load'));}},50);})")
                        for mode in ("minimal", "thumbnails", "extended", "compact"):
                            check_view = window.evaluate_js("(()=>{const select=document.querySelector('#search-view');select.value=" + json.dumps(mode) + ";select.dispatchEvent(new Event('change'));return {cards:document.querySelectorAll('#results .saved-card').length,ratio:document.querySelector('#results .saved-ratio')?.textContent,overflow:document.documentElement.scrollWidth>innerWidth+1};})()")
                            if check_view["cards"] != 1 or check_view["ratio"] != "收藏/评分 4.1（良好）" or check_view["overflow"]:
                                raise AssertionError(check_view)
                            promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(!document.querySelector('#search-view').disabled){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Search view did not save'));}},50);})")
                        preference = promise("fetch('/api/preferences').then(r=>r.json())")
                        if preference["search_view"] != "compact" or (preference["records_view"] or "extended") != expected_view:
                            raise AssertionError(preference)
                        report["checks"].append("all four search views render in actual packaged WebView2; search preference saves independently")
                    if page == "/records":
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(document.querySelector('.saved-card')){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Records did not load'));}},50);})")
                        actual_view = window.evaluate_js("document.querySelector('#record-view').value")
                        if actual_view != expected_view:
                            raise AssertionError({"expected":expected_view,"actual":actual_view})
                        report["initial_rendered_view"] = actual_view
                        default_rating = window.evaluate_js("document.querySelector('#records-list .saved-ratio')?.textContent")
                        if default_rating != "收藏/评分 4.1（良好）":
                            raise AssertionError(default_rating)
                        window.evaluate_js("document.querySelector('#record-sort').value='favorites_per_rating';document.querySelector('#records-form').requestSubmit()")
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(document.querySelector('#records-list .saved-ratio')){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('Record ratios did not load'));}},50);})")
                        for mode in ("minimal", "compact", "extended", "thumbnails"):
                            check_view = window.evaluate_js("(()=>{const select=document.querySelector('#record-view');select.value=" + json.dumps(mode) + ";select.dispatchEvent(new Event('change'));return {cards:document.querySelectorAll('.saved-card').length,ratio:document.querySelector('.saved-ratio')?.textContent,overflow:document.documentElement.scrollWidth>innerWidth+1};})()")
                            if check_view["cards"] != 1 or check_view["ratio"] != "收藏/评分 4.1（良好）" or check_view["overflow"]:
                                raise AssertionError(check_view)
                        report["checks"].append("all four record views render in actual packaged WebView2")
                        guide = window.evaluate_js("(()=>{const guide=document.querySelector('#rating-system-guide');guide.open=true;return {rows:guide.querySelectorAll('tbody tr').length,minimum:guide.textContent.includes('至少 20 人评分'),example:guide.textContent.includes('2.7（一般）')};})()")
                        if guide != {"rows":5,"minimum":True,"example":True}:
                            raise AssertionError(guide)
                        report["checks"].append("alternative ratings render with other sorts and ratio sort in both pages; all four views and expandable standards work in packaged WebView2")
                        promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{if(!document.querySelector('#record-view').disabled){clearInterval(timer);resolve(true);}else if(++tries>100){clearInterval(timer);reject(new Error('View preference did not save'));}},50);})")
                        preference = promise("fetch('/api/preferences').then(r=>r.json())")
                        if preference["records_view"] != "thumbnails":
                            raise AssertionError(preference)
                        report["checks"].append("thumbnail preference committed to local database before exit")
                stage("search API")
                startup=promise("window.pywebview.api.startup_client_update_status()")
                if startup["phase"] != "completed" or len(startup_requests) != 1 or startup["release"]["available"]:
                    raise AssertionError({"state":startup,"requests":len(startup_requests)})
                report["checks"].append("one automatic release check per process; page navigation does not repeat it")
                result = promise("fetch('/api/search?tag=language%3Aenglish').then(r=>r.json()).then(d=>({total:d.total}))")
                if result["total"] != 1:
                    raise AssertionError(result)
                report["checks"].append("search returns fixture record")
                stage("native export bridge")
                destination = root / "export.json"
                real_dialog = window.create_file_dialog
                window.create_file_dialog = lambda *args, **kwargs: (str(destination),)
                try:
                    result = promise("window.pywebview.api.save_attachment('/api/maintenance/favorites-export')")
                    sharing = promise("document.querySelector('#favorites-export').onclick().then(()=>({open:document.querySelector('#favorites-share-guide').open,visible:!document.querySelector('#favorites-share-generated').hidden,title:document.querySelector('#favorites-share-title').value,body:document.querySelector('#favorites-share-body').value,steps:document.querySelectorAll('.favorites-share-steps>li').length,overflow:document.documentElement.scrollWidth>innerWidth+1}))")
                finally:
                    window.create_file_dialog = real_dialog
                if result.get("error") or result.get("record_count") != 1 or not destination.is_file():
                    raise AssertionError(result)
                if result.get("share_info", {}).get("client_version") != APP_VERSION or not sharing["open"] or not sharing["visible"] or sharing["steps"] != 8 or sharing["overflow"] or "1 条" not in sharing["title"] or "v" + APP_VERSION not in sharing["body"]:
                    raise AssertionError({"share_info": result.get("share_info"), "ui": sharing})
                report["checks"].append("native bridge exports favorites to chosen file")
                report["checks"].append("share guide expands and generates title/body in actual packaged WebView2")
                stage("complete offline release history")
                history_loaded=threading.Event()
                def history_navigation():history_loaded.set()
                window.events.loaded+=history_navigation
                window.load_url(url+'/changelog')
                if not history_loaded.wait(20):raise TimeoutError("Release history page did not load")
                window.events.loaded-=history_navigation
                history_ui=promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{const rows=document.querySelectorAll('#history-list button');if(rows.length>10){clearInterval(timer);resolve({count:rows.length,current:document.querySelector('#history-list button[aria-pressed=true]').dataset.version,heading:document.querySelector('#history-title').textContent,overflow:document.documentElement.scrollWidth>innerWidth+1});}else if(++tries>200){clearInterval(timer);reject(new Error('Offline release history did not render'));}},50);})")
                if history_ui['current']!=APP_VERSION or history_ui['overflow']:raise AssertionError(history_ui)
                report['checks'].append('actual packaged WebView2 renders offline full release history and selects current version')
                history_loaded.clear();window.events.loaded+=history_navigation
                window.load_url(url+'/maintenance')
                if not history_loaded.wait(20):raise TimeoutError("Could not return from release history")
                window.events.loaded-=history_navigation
                promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(()=>{const button=document.querySelector('#cloud-sync-save');if(button&&!button.disabled){clearInterval(timer);resolve(true);}else if(++tries>200){clearInterval(timer);reject(new Error('Maintenance did not reload'));}},50);})")
                stage("mixed collector library upload")
                other="00000000-0000-4000-8000-000000000002"
                with session.server.catalog.favorites.connection() as db:
                    db.execute("INSERT INTO favorites VALUES(2,100,?,'e-hentai.org',?,'测试原采集者')",(int(time.time()),other))
                    db.execute("INSERT INTO favorites VALUES(3,100,?,'e-hentai.org','','')",(int(time.time()),))
                session.server.cloud_sync.trigger()
                mixed=promise("new Promise((resolve,reject)=>{let tries=0;const timer=setInterval(async()=>{const state=await fetch('/api/cloud-sync').then(r=>r.json());if(state.phase==='waiting'&&state.upload_total===3&&state.upload_confirmed===3&&state.upload_remaining===0&&document.querySelector('#cloud-sync-progress').textContent.includes('服务器已确认 3 条')){clearInterval(timer);resolve(state);return;}if(++tries>200){clearInterval(timer);reject(new Error('Mixed collector upload did not finish'));}},50);})")
                foreign=next((item for item in reversed(cloud_submissions) if item['gid']==2),None)
                unknown=next((item for item in reversed(cloud_submissions) if item['gid']==3),None)
                if not foreign or foreign['collector_id']!=other or foreign['collector_name']!='测试原采集者' or not unknown or unknown['collector_id'] or unknown['collector_name']:
                    raise AssertionError("Packaged upload did not preserve foreign and unknown attribution")
                scope=window.evaluate_js("document.querySelector('label:has(#cloud-sync-upload)').textContent")
                if mixed['upload_batch_size']!=50:raise AssertionError("Packaged client did not negotiate fifty-record batches")
                if mixed['upload_skipped']!=1:raise AssertionError("Packaged client did not skip the public snapshot")
                if mixed['upload_min_interval_days']!=14:raise AssertionError("Packaged client did not read the server update interval")
                report["checks"].append("packaged client checks public versions and skips existing data without resending the full snapshot")
                report["checks"].append("packaged client negotiates fifty-record uploads and uses the persistent queue index")
                if '全部成功收藏数' not in scope:raise AssertionError("Packaged sharing scope is stale")
                report["checks"].append("actual packaged GUI confirms own, foreign and unknown collector records, skips the public snapshot and uploads differences with original attribution")
                with session.server.catalog.favorites.connection() as db:
                    db.execute("DELETE FROM favorites WHERE gid IN (2,3)")
                # Capture only this application-owned test page, never the user's desktop.
                stage("screenshot")
                capture_page(report_path.with_suffix(".png"))
                report["checks"].append("WebView2 rendered screenshot")
                session.close()
                if before != hashlib.sha256(database.read_bytes()).hexdigest():
                    raise AssertionError("Catalog changed during desktop checks")
                report["checks"].append("safe shutdown leaves catalog bytes unchanged")
                report["passed"] = True
            except Exception:
                report["error"] = traceback.format_exc()
            finally:
                session.close(wait_for_maintenance=True)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                window.destroy()

        try:
            webview.start(check, gui="edgechromium", private_mode=True)
        except Exception:
            report["error"] = traceback.format_exc()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            session.close(wait_for_maintenance=True)
    faulthandler.cancel_dump_traceback_later()
    trace_file.close()
    return 0 if report["passed"] else 1
