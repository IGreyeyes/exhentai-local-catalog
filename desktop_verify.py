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
import faulthandler
import traceback


def verify_desktop(report_path, fixture_root=None):
    import webview
    from desktop import DesktopApi
    from desktop_service import DesktopSession
    from initialize_catalog import initialize
    from client_version import APP_VERSION
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
        url = session.start(lambda message: None)
        session.server.catalog.favorites.save(1, 123, "exhentai.org", 30)
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

        def check():
            try:
                stage("waiting for WebView2 page")
                if not window.events.loaded.wait(30):
                    raise TimeoutError("WebView2 did not load")
                report["renderer"] = window.gui.renderer
                if report["renderer"] != "edgechromium":
                    raise AssertionError("Embedded renderer is not WebView2")
                for page, selector in (("/", "#search-form"), ("/records", "#records-list"), ("/maintenance", "#favorites-export")):
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
                        promise("window.pywebview.api.acknowledge_client_notes()")
                        window.evaluate_js("document.querySelector('#client-update-dialog').close()")
                        report["checks"].append("client version and native notes acknowledgement bridge work")
                    else:
                        result = promise("window.pywebview.api.client_info()")
                        if result.get("show_notes"):
                            raise AssertionError("Read notes were shown again after navigation")
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
                # Capture only this application-owned test page, never the user's desktop.
                stage("screenshot")
                from System import Action, Func, Object, String
                from System.Threading.Tasks import Task
                form = window.gui.BrowserView.instances[window.uid]
                task = form.Invoke(Func[Object](lambda: form.browser.webview.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.captureScreenshot", '{"format":"png","captureBeyondViewport":false}')))
                captured = threading.Event()
                captures = []
                def captured_image(completed):
                    captures.append(str(completed.Result))
                    captured.set()
                task.ContinueWith(Action[Task[String]](captured_image))
                if not captured.wait(10):
                    raise TimeoutError("WebView2 screenshot timed out")
                screenshot = json.loads(captures[0])["data"]
                report_path.with_suffix(".png").write_bytes(base64.b64decode(screenshot))
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
