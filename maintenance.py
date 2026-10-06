"""Local library maintenance and official Tag dictionary updates."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from initialize_catalog import initialize
from prepare_database import prepare, inspect_database
from favorites import FavoriteStore
from update_translations import check_latest, compare_version, latest_release, local_status, update

ROOT = Path(__file__).resolve().parent


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+"."+secrets.token_hex(4)+".partial")
    with temporary.open("x",encoding="utf-8") as output:
        json.dump(value,output,ensure_ascii=False,indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary,path)


def read_json(path, default=None):
    try:return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:return default


def digest_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def contained(path, root):
    path,root = Path(path).resolve(),Path(root).resolve()
    if path==root or not path.is_relative_to(root):
        raise ValueError("维护文件路径超出了项目范围。")
    return path


def sqlite_backup(source, destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True,timeout=30)) as original, closing(sqlite3.connect(destination)) as copied:
        original.backup(copied,pages=2048,sleep=0.05)
        if [row[0] for row in copied.execute("PRAGMA quick_check")] != ["ok"]:
            raise ValueError("备份数据库完整性检查失败，未标记为成功。")
        copied.execute("PRAGMA journal_mode=DELETE")


def checkpoint(path):
    with closing(sqlite3.connect(path,timeout=5)) as db:
        status = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if status[0]: raise ValueError("数据库仍有未结束的访问，请稍后应用更新。")
        if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise ValueError("数据库仍被其他程序占用，暂时无法切换目录。")


def recover_import(catalog_path):
    """Recover an interrupted replacement before opening any catalog connections."""
    catalog_path = Path(catalog_path).resolve()
    restored = recover_restore(catalog_path)
    journal_path = catalog_path.with_name("catalog-swap.json")
    journal = read_json(journal_path)
    if not journal: return restored
    if Path(journal["catalog"]).resolve()!=catalog_path:
        raise ValueError("更新恢复记录与当前目录不一致，请保留文件并检查备份。")
    stage = contained(journal["stage"],catalog_path.parent/".maintenance")
    previous = stage/"previous"
    if journal["state"]!="committed":
        old = previous/"catalog.sqlite3"
        if old.exists():
            if catalog_path.exists():
                catalog_path.rename(stage/("interrupted-"+secrets.token_hex(4)+".sqlite3"))
            old.rename(catalog_path)
            info_path = catalog_path.with_name("catalog_info.json")
            old_info = previous/"catalog_info.json"
            if old_info.exists():
                if info_path.exists():info_path.rename(stage/("interrupted-info-"+secrets.token_hex(4)+".json"))
                old_info.rename(info_path)
            elif not journal["had_info"] and info_path.exists():
                info_path.rename(stage/("interrupted-info-"+secrets.token_hex(4)+".json"))
        elif journal["state"]!="planned":
            raise ValueError("无法自动找到回退目录，请使用更新前的完整备份恢复。")
        inspect_database(catalog_path)
    journal_path.unlink()
    return "上次目录切换已恢复。" if journal["state"]!="committed" else "上次目录更新已完成。"


RESTORE_FILES = {"favorites.sqlite3", "catalog.sqlite3", "catalog_info.json", "tag-translations.json", "tag-translations-info.json"}


def recover_restore(catalog_path):
    catalog_path = Path(catalog_path).resolve()
    data = catalog_path.parent
    journal_path = data/"restore-swap.json"
    journal = read_json(journal_path)
    if not journal:return None
    stage = contained(journal["stage"],data/".maintenance")
    if journal["state"] != "committed":
        for item in reversed(journal["files"]):
            name = item["name"]
            if name not in RESTORE_FILES:raise ValueError("备份恢复记录包含未知文件，请保留文件并检查。")
            target, old = catalog_path if name=="catalog.sqlite3" else data/name, stage/"previous"/name
            if old.exists() or (not item["had_original"] and not (stage/"incoming"/name).exists()):
                if target.exists():
                    if target.suffix == ".sqlite3":checkpoint(target)
                    target.rename(stage/("interrupted-"+secrets.token_hex(4)+"-"+name))
                if old.exists():old.rename(target)
    journal_path.unlink()
    return "上次备份导入已完成。" if journal["state"] == "committed" else "上次备份导入已回退，原资料已恢复。"


class LibraryMaintenance:
    def __init__(self,catalog_path,project_root):
        self.catalog_path = Path(catalog_path).resolve()
        self.root = Path(project_root).resolve()
        contained(self.catalog_path,self.root)
        self.data = self.catalog_path.parent
        settings = read_json(self.data/"maintenance-settings.json",{})
        self.backups = self.backup_directory(settings.get("backup_directory", ""))
        self.staging = self.data/".maintenance"
        self.prepared_file = self.data/"prepared-import.json"
        self.restore_file = self.data/"prepared-restore.json"
        self.translation_check_path = self.data/"tag-translations-check.json"
        self.translation_signature = None
        self.translation_local = {}
        self.translation_check_memory = None

    def translation_status(self):
        paths = [self.data/"tag-translations.json", self.data/"tag-translations-info.json"]
        signature = tuple((path.stat().st_mtime_ns, path.stat().st_size) if path.is_file() else None for path in paths)
        if signature != self.translation_signature:
            self.translation_local = local_status(paths[0])
            self.translation_signature = signature
        local = dict(self.translation_local)
        try:
            checked = self.translation_check_memory if self.translation_check_memory is not None else read_json(self.translation_check_path, {})
        except (OSError, ValueError):
            checked = {}
        if not isinstance(checked, dict):
            checked = {}
        matches = checked and checked.get("dictionary_sha256") == local.get("dictionary_sha256")
        state = checked.get("state", "unchecked") if matches else "unchecked" if local.get("available") else "not_installed"
        return {"local": local, "state": state, "latest": checked.get("latest"), "checked_at": checked.get("checked_at", "")}

    def save_translation_check(self, result):
        self.translation_check_memory = result
        try:
            save_json(self.translation_check_path, result)
        except OSError:
            result["message"] += " 本次结果已显示，但检查记录未能保存；请检查磁盘空间与文件权限。"

    def check_translations(self, progress=lambda message: None):
        progress("正在查询官方中文词库版本…")
        result = check_latest(self.data/"tag-translations.json")
        self.save_translation_check(result)
        return result

    def update_translations(self, reload_translations, progress=lambda message: None):
        progress("正在核对官方最新词库版本…")
        destination = self.data/"tag-translations.json"
        latest = latest_release()
        if compare_version(local_status(destination), latest) == "up_to_date":
            changed = False
            # Also reload when a CLI update has already installed the same release.
            reload_translations()
        else:
            update(latest["download_url"], latest.get("archive_sha256") or None, destination, release=latest, progress=progress, on_installed=reload_translations)
            changed = True
        local = local_status(destination)
        result = {"state": "up_to_date", "latest": latest, "checked_at": datetime.now(timezone.utc).isoformat(), "dictionary_sha256": local["dictionary_sha256"], "changed": changed, "message": "中文词库更新成功，已立即生效。已打开的搜索和记录页刷新后即可显示新译名。" if changed else "当前词库已是官方最新版本，并已载入；无需重复下载。"}
        self.save_translation_check(result)
        self.translation_signature = None
        return result

    def backup_directory(self, value):
        if not isinstance(value,str) or len(value)>4096:raise ValueError("备份位置格式不正确。")
        if not value.strip():return self.root/"backups"
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():raise ValueError("请输入备份文件夹的完整路径，例如 D:\\EX备份。")
        path = path.resolve()
        if path==self.root or path==self.data or path.is_relative_to(self.data) or path.is_relative_to(self.root/"static"):
            raise ValueError("请选择独立备份文件夹，不能使用程序或数据文件夹。")
        if path.exists() and not path.is_dir():raise ValueError("备份位置必须是文件夹。")
        return path

    def backup(self,include_catalog=False,reason="手动备份",progress=lambda message:None):
        self.backups.mkdir(parents=True,exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")+("-full-" if include_catalog else "-data-")+secrets.token_hex(3)
        temporary = self.backups/(name+".partial")
        temporary.mkdir()
        files = []
        sources = [(self.data/"favorites.sqlite3","data/favorites.sqlite3")]
        if include_catalog:sources.insert(0,(self.catalog_path,"data/catalog.sqlite3"))
        required_space = sum(path.stat().st_size for path,_ in sources if path.exists())+100*1024**2
        if shutil.disk_usage(self.backups).free<required_space:
            raise ValueError("磁盘剩余空间不足，暂时无法创建这份备份。")
        for source,relative in sources:
            if source.exists():
                source=contained(source,self.root)
                progress("正在一致性备份 "+source.name)
                destination = temporary/relative
                sqlite_backup(source,destination)
                files.append(relative)
        metadata = ["catalog_info.json","tag-translations.json","tag-translations-info.json","maintenance-settings.json"]
        for name in metadata:
            source = self.data/name
            if source.exists():
                source=contained(source,self.root)
                value = read_json(source)
                destination = temporary/"data"/name
                save_json(destination,value)
                files.append("data/"+name)
        progress("备份程序文件并校验备份内容")
        for source in self.root.iterdir():
            if source.is_file() and (source.suffix in {".py",".cmd"} or source.name in {"README.md","THIRD_PARTY_NOTICES.md",".gitignore"}):
                source=contained(source,self.root)
                shutil.copy2(source,temporary/source.name)
                files.append(source.name)
        static = self.root/"static"
        if static.is_dir():
            for source in static.iterdir():
                if source.is_file() and source.suffix in {".js",".css",".html",".svg"}:
                    source=contained(source,self.root)
                    (temporary/"static").mkdir(exist_ok=True)
                    shutil.copy2(source,temporary/"static"/source.name)
                    files.append("static/"+source.name)
        manifest = {
            "format":1,"complete":True,"created_at":datetime.now(timezone.utc).isoformat(),
            "include_catalog":include_catalog,"reason":reason,
            "files":[{"path":name,"size":(temporary/name).stat().st_size,"sha256":digest_file(temporary/name)} for name in files],
        }
        manifest["size_bytes"] = sum(item["size"] for item in manifest["files"])
        save_json(temporary/"manifest.json",manifest)
        final = temporary.with_name(temporary.name.removesuffix(".partial"))
        temporary.rename(final)
        return {"path":str(final),"name":final.name,**{key:manifest[key] for key in ("created_at","include_catalog","reason","size_bytes")}}

    def list_backups(self):
        if not self.backups.is_dir():return []
        items=[]
        for folder in sorted(self.backups.iterdir(),reverse=True):
            if not folder.is_dir() or folder.name.endswith(".partial"):continue
            try:
                manifest=read_json(contained(folder,self.backups)/"manifest.json")
                if manifest and manifest.get("complete"):
                    items.append({"name":folder.name,"path":str(folder),**{key:manifest.get(key) for key in ("created_at","include_catalog","reason","size_bytes")}})
            except (OSError,ValueError):continue
            if len(items)>=10:break
        return items

    def prepare_restore(self, folder, include_catalog=False, progress=lambda message:None):
        if not isinstance(folder,str) or not folder.strip():raise ValueError("请选择或填写含 manifest.json 的备份文件夹。")
        source = Path(folder.strip()).expanduser()
        if not source.is_absolute():raise ValueError("请输入备份文件夹的完整路径。")
        source = source.resolve()
        if source.name.endswith(".partial"):raise ValueError("这份备份尚未完成，无法导入。")
        manifest = read_json(source/"manifest.json")
        if not isinstance(manifest,dict) or manifest.get("format")!=1 or manifest.get("complete") is not True:
            raise ValueError("未找到有效的完整备份清单 manifest.json。")
        entries = manifest.get("files")
        if not isinstance(entries,list) or not entries:raise ValueError("备份文件清单为空或格式不正确。")
        if include_catalog and not manifest.get("include_catalog"):raise ValueError("这份备份没有作品目录，请取消同时恢复作品目录。")
        self.restore_file.unlink(missing_ok=True)
        verified={}
        for item in entries:
            if not isinstance(item,dict):raise ValueError("备份文件清单格式不正确。")
            name=item.get("path")
            if not isinstance(name,str) or "\\" in name or ":" in name or name.startswith("/") or ".." in name.split("/") or name in verified:
                raise ValueError("备份清单包含不安全或重复的路径。")
            path=contained(source/name,source)
            progress("校验备份文件："+name)
            if not path.is_file() or path.stat().st_size!=item.get("size") or digest_file(path)!=item.get("sha256"):
                raise ValueError("备份文件缺失或校验失败："+name)
            verified[name]=path
        if "data/favorites.sqlite3" not in verified:raise ValueError("备份未包含收藏资料库，无法导入。")
        names=["favorites.sqlite3","tag-translations.json","tag-translations-info.json"]
        if include_catalog:
            if "data/catalog.sqlite3" not in verified:raise ValueError("完整备份缺少作品目录。")
            names += ["catalog.sqlite3","catalog_info.json"]
        required = sum(verified["data/"+name].stat().st_size for name in names if "data/"+name in verified)
        if shutil.disk_usage(self.data).free<required+100*1024**2:raise ValueError("磁盘空间不足，无法准备备份导入。")
        identifier=secrets.token_hex(12)
        stage=contained(self.staging/("restore-"+identifier),self.staging)
        incoming=stage/"incoming";incoming.mkdir(parents=True)
        progress("暂存已校验的备份资料")
        for name in names:
            original=verified.get("data/"+name)
            if original is None:continue
            target=incoming/name
            if name.endswith(".sqlite3"):sqlite_backup(original,target)
            else:
                value=read_json(original)
                if not isinstance(value,dict):raise ValueError("备份资料格式不正确："+name)
                save_json(target,value)
        favorites=incoming/"favorites.sqlite3"
        with closing(sqlite3.connect(favorites)) as db:
            for query in ("SELECT gid,favorite_count,checked_at,source FROM favorites LIMIT 1", "SELECT id,state FROM jobs LIMIT 1", "SELECT job_id,gid,token,state FROM tasks LIMIT 1"):
                try:db.execute(query).fetchall()
                except sqlite3.DatabaseError as error:raise ValueError("备份收藏资料库的结构不正确。") from error
        store=FavoriteStore(favorites)
        with store.connection() as db:
            db.execute("UPDATE jobs SET state='paused',message='从备份导入后已暂停，请确认登录设置后继续。' WHERE state IN ('running','preparing')")
            summary={"favorites":db.execute("SELECT COUNT(*) FROM favorites").fetchone()[0],"reading_states":db.execute("SELECT COUNT(*) FROM record_status WHERE state!='none'").fetchone()[0],"jobs":db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]}
        checkpoint(favorites)
        if include_catalog:
            inspect_database(incoming/"catalog.sqlite3")
            if not (incoming/"catalog_info.json").exists():
                with closing(sqlite3.connect(incoming/"catalog.sqlite3")) as db:
                    info={"gallery_count":db.execute("SELECT COUNT(*) FROM gallery").fetchone()[0],"tag_count":db.execute("SELECT COUNT(*) FROM tag").fetchone()[0],"latest_posted":db.execute("SELECT MAX(posted) FROM gallery").fetchone()[0]}
                save_json(incoming/"catalog_info.json",info)
        result={"id":identifier,"stage":str(stage),"source":str(source),"created_at":manifest.get("created_at"),"include_catalog":include_catalog,"summary":summary,"files":[{"name":path.name,"sha256":digest_file(path)} for path in sorted(incoming.iterdir()) if path.is_file()]}
        save_json(self.restore_file,result)
        return result

    def prepared_restore(self):
        result=read_json(self.restore_file)
        if result and (contained(result["stage"],self.staging)/"incoming"/"favorites.sqlite3").is_file():return result
        return None

    def restore(self,identifier,reload_catalog=lambda:None,progress=lambda message:None):
        ready=self.prepared_restore()
        if not ready or not isinstance(identifier,str) or not secrets.compare_digest(ready["id"],identifier):
            raise ValueError("请先检查备份，再确认导入。")
        stage=contained(ready["stage"],self.staging)
        for item in ready["files"]:
            if item["name"] not in RESTORE_FILES or digest_file(stage/"incoming"/item["name"])!=item["sha256"]:
                raise ValueError("准备好的备份资料发生变化，请重新检查。")
        progress("导入前自动备份当前资料")
        backup=self.backup(ready["include_catalog"],"备份导入前自动备份",progress)
        (stage/"previous").mkdir()
        target_path=lambda name:self.catalog_path if name=="catalog.sqlite3" else self.data/name
        files=[{"name":item["name"],"had_original":target_path(item["name"]).exists()} for item in ready["files"]]
        journal={"stage":str(stage),"state":"planned","files":files,"backup":backup["path"]}
        journal_path=self.data/"restore-swap.json"
        for item in files:
            target=target_path(item["name"])
            if target.exists() and target.suffix==".sqlite3":checkpoint(target)
        save_json(journal_path,journal)
        try:
            progress("正在导入收藏数、阅读状态、采集进度和词库")
            for item in files:
                name=item["name"];target=target_path(name)
                if item["had_original"]:target.rename(stage/"previous"/name)
                (stage/"incoming"/name).rename(target)
            reload_catalog()
            journal["state"]="committed";save_json(journal_path,journal)
        except Exception:
            recover_restore(self.catalog_path)
            reload_catalog()
            raise
        self.restore_file.unlink(missing_ok=True)
        self.prepared_file.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
        return {"backup":backup,"summary":ready["summary"],"include_catalog":ready["include_catalog"],"message":"备份已导入，采集任务保持暂停。导入前的资料已自动备份："+backup["path"]}

    def prepare_archive(self,expected_sha256="",progress=lambda message:None):
        source=contained(self.root/"e-hentai.db.zstd",self.root)
        if not source.is_file():raise ValueError("请先将新版 e-hentai.db.zstd 放到项目文件夹中。")
        if expected_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}",expected_sha256):
            raise ValueError("SHA-256 应为 64 位十六进制字符，也可以留空。")
        self.prepared_file.unlink(missing_ok=True)
        progress("计算压缩包校验值")
        source_stat=source.stat()
        archive_hash=digest_file(source)
        if expected_sha256 and archive_hash!=expected_sha256.lower():raise ValueError("压缩包校验值不匹配，请重新下载后再试。")
        current_info=read_json(self.data/"catalog_info.json",{})
        if current_info.get("archive_sha256")==archive_hash:
            return {"same_archive":True,"message":"这个压缩包已经应用过，无需重复导入。"}
        free=shutil.disk_usage(self.root).free
        if free<self.catalog_path.stat().st_size*2+source_stat.st_size+512*1024**2:
            raise ValueError("剩余空间不足以同时保存新版暂存目录和旧版备份。")
        identifier=secrets.token_hex(12)
        stage=contained(self.staging/identifier,self.root)
        stage.mkdir(parents=True)
        candidate=stage/"catalog.sqlite3"
        progress("正在解压新版目录，现有资料库仍可使用")
        prepare(source,candidate,None,progress=lambda size:progress(f"正在解压新版目录，已解压 {size/1024**3:.2f} GiB"))
        report=initialize(candidate,progress=progress)
        if not report["gallery_count"] or not report["tag_count"]:
            raise ValueError("新版目录为空，未替换当前资料库。")
        checkpoint(candidate)
        progress("校验准备结果")
        if source.stat().st_size!=source_stat.st_size or source.stat().st_mtime_ns!=source_stat.st_mtime_ns or digest_file(source)!=archive_hash:
            raise ValueError("准备过程中压缩包发生变化，请重新检查新文件。")
        report.update(archive_sha256=archive_hash,archive_size=source_stat.st_size,size_bytes=candidate.stat().st_size)
        save_json(stage/"catalog_info.json",report)
        result={"id":identifier,"stage":str(stage),"archive_sha256":archive_hash,"catalog_sha256":digest_file(candidate),"old":current_info,"new":report,"same_archive":False,"prepared_at":datetime.now(timezone.utc).isoformat()}
        result["older"]=(report.get("latest_posted") or 0)<(current_info.get("latest_posted") or 0)
        result["fewer_records"]=report["gallery_count"]<(current_info.get("gallery_count") or 0)
        save_json(self.prepared_file,result)
        return result

    def prepared(self):
        result=read_json(self.prepared_file)
        if result and (contained(result["stage"],self.staging)/"catalog.sqlite3").is_file():return result
        return None

    def apply(self,identifier,allow_older=False,reload_catalog=lambda:None,progress=lambda message:None):
        prepared=self.prepared()
        if not prepared or not isinstance(identifier,str) or not secrets.compare_digest(prepared["id"],identifier):
            raise ValueError("请先检查并准备新版目录，再确认应用更新。")
        stage=contained(prepared["stage"],self.staging)
        candidate=stage/"catalog.sqlite3"
        current=read_json(self.data/"catalog_info.json",{})
        older=(prepared["new"].get("latest_posted") or 0)<(current.get("latest_posted") or 0)
        if older and not allow_older:raise ValueError("候选目录的最新作品日期较旧，未自动回退；如确有需要，请勾选允许较旧快照。")
        if digest_file(candidate)!=prepared["catalog_sha256"]:raise ValueError("准备好的目录发生变化，请重新准备。")
        progress("更新前自动创建完整备份")
        backup=self.backup(True,"目录更新前自动备份",progress)
        previous=stage/"previous"
        previous.mkdir()
        info_path=self.data/"catalog_info.json"
        journal_path=self.data/"catalog-swap.json"
        journal={"catalog":str(self.catalog_path),"stage":str(stage),"backup":backup["path"],"state":"planned","had_info":info_path.exists()}
        checkpoint(self.catalog_path)
        checkpoint(candidate)
        save_json(journal_path,journal)
        try:
            progress("切换作品目录；收藏数与词库继续保留")
            self.catalog_path.rename(previous/"catalog.sqlite3")
            if info_path.exists():info_path.rename(previous/"catalog_info.json")
            journal["state"]="old_moved";save_json(journal_path,journal)
            candidate.rename(self.catalog_path)
            (stage/"catalog_info.json").rename(info_path)
            journal["state"]="installed";save_json(journal_path,journal)
            reload_catalog()
            journal["state"]="committed";save_json(journal_path,journal)
        except Exception:
            recover_import(self.catalog_path)
            reload_catalog()
            raise
        # Only remove these named temporary originals after a verified full backup.
        warning=""
        try:
            for name in ("catalog.sqlite3","catalog_info.json"):
                path=contained(previous/name,self.staging)
                if path.is_file():path.unlink()
            self.prepared_file.unlink(missing_ok=True)
            journal_path.unlink()
        except OSError:
            warning="部分旧版暂存文件未能清理，完整备份已保留。"
        return {"backup":backup,"new":prepared["new"],"message":"新版作品目录已生效；收藏数、任务进度和中文词库已保留。"+warning}


class MaintenanceManager:
    def __init__(self,engine,exclusive,reload_catalog,reload_translations=None):
        self.engine,self.exclusive,self.reload_catalog=engine,exclusive,reload_catalog
        self.reload_translations = reload_translations or (lambda: None)
        self.lock=threading.RLock()
        self.state_path=engine.data/"maintenance-state.json"
        self.settings_path=engine.data/"maintenance-settings.json"
        self.history_path=engine.data/"maintenance-history.json"
        self.history=read_json(self.history_path,[])
        self.settings={"backup_on_completion":False,**read_json(self.settings_path,{})}
        self.state=read_json(self.state_path,{"kind":"","phase":"idle","message":"维护工具已就绪。","busy":False})
        if self.state.get("busy"):self.state.update(busy=False,phase="interrupted",message="上次维护被中断，旧数据和暂存文件已保留；请重新检查。")
        self.thread=None
        self.stopping=False
        self.pending_auto=None
        self.picker_lock=threading.Lock()

    def choose_directory(self,purpose,initial=""):
        if purpose not in {"backup","restore"}:raise ValueError("文件夹选择用途不正确。")
        if not isinstance(initial,str) or len(initial)>4096:raise ValueError("文件夹路径格式不正确。")
        with self.lock:
            if self.stopping or self.state.get("busy"):raise ValueError("请等待维护完成后再选择文件夹。")
            if not self.picker_lock.acquire(blocking=False):raise ValueError("文件夹选择窗口已打开，请先完成选择。")
        try:
            # Run Tk in its own process: the HTTP handler and background server have no GUI event loop.
            script="""import json,sys,tkinter as tk
from tkinter import filedialog
root=tk.Tk()
root.withdraw()
root.attributes('-topmost',True)
try:
    path=filedialog.askdirectory(parent=root,title=sys.argv[1],initialdir=sys.argv[2],mustexist=sys.argv[3]=='restore')
    print(json.dumps({'path':path},ensure_ascii=False))
finally:
    root.destroy()
"""
            initial_path=Path(initial).expanduser() if initial.strip() else self.engine.backups
            while not initial_path.is_dir() and initial_path!=initial_path.parent:initial_path=initial_path.parent
            flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
            result=subprocess.run([sys.executable,"-X","utf8","-c",script,"选择备份保存位置" if purpose=="backup" else "选择要导入的备份文件夹",str(initial_path),purpose],capture_output=True,text=True,encoding="utf-8",timeout=300,creationflags=flags)
            if result.returncode:raise ValueError("无法打开文件夹选择窗口，请直接粘贴文件夹的完整路径。")
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired as error:
            raise ValueError("文件夹选择已超时，请重新选择或直接粘贴完整路径。") from error
        finally:self.picker_lock.release()

    def status(self):
        with self.lock:
            return {**self.state,"settings":dict(self.settings),"recent_tasks":list(self.history),"backup_directory":str(self.engine.backups),"default_backup_directory":str(self.engine.root/"backups"),"archive_path":str(self.engine.root/"e-hentai.db.zstd"),"archive_exists":(self.engine.root/"e-hentai.db.zstd").is_file(),"catalog_size":self.engine.catalog_path.stat().st_size if self.engine.catalog_path.exists() else 0,"prepared":self.engine.prepared(),"prepared_restore":self.engine.prepared_restore(),"backups":self.engine.list_backups(),"translations":self.engine.translation_status()}

    def publish(self,message,**updates):
        with self.lock:
            self.state.update(message=message,updated_at=datetime.now(timezone.utc).isoformat(),**updates)
            save_json(self.state_path,self.state)

    def configure(self,enabled=None,backup_directory=None):
        if enabled is not None and not isinstance(enabled,bool):raise ValueError("自动备份选项须为开关值。")
        with self.lock:
            if self.stopping:raise ValueError("服务正在停止，暂时不能修改设置。")
            if self.state.get("busy"):raise ValueError("维护任务正在运行，请完成后再修改备份设置。")
            settings=dict(self.settings)
            if enabled is not None:settings["backup_on_completion"]=enabled
            directory=self.engine.backups
            if backup_directory is not None:
                directory=self.engine.backup_directory(backup_directory)
                directory.mkdir(parents=True,exist_ok=True)
                settings["backup_directory"]=str(directory)
            save_json(self.settings_path,settings)
            self.settings=settings
            self.engine.backups=directory
        return self.status()

    def start(self,kind,include_catalog=False,expected_sha256="",identifier="",allow_older=False,reason="手动备份",restore_path=""):
        with self.lock:
            if self.stopping:raise ValueError("服务正在停止，暂时不能启动维护。")
            if self.state.get("busy"):raise ValueError("已有维护任务正在运行，请等待完成。")
            if kind not in {"backup","prepare","apply","prepare-restore","restore","translations-check","translations-update"}:raise ValueError("未知维护操作。")
            if not isinstance(include_catalog,bool) or not isinstance(allow_older,bool) or not isinstance(expected_sha256,str) or not isinstance(restore_path,str):raise ValueError("维护选项格式不正确。")
            self.state={"kind":kind,"phase":"running","message":"正在准备维护任务…","busy":True,"id":secrets.token_hex(8),"started_at":datetime.now(timezone.utc).isoformat()}
            task_id=self.state["id"]
            save_json(self.state_path,self.state)
            self.thread=threading.Thread(target=self._run,args=(kind,include_catalog,expected_sha256,identifier,allow_older,reason,restore_path),daemon=False,name="catalog-maintenance")
            self.thread.start()
        return {**self.status(),"requested_id":task_id}

    def finish(self,message,phase,result=None):
        with self.lock:
            finished={**self.state,"message":message,"phase":phase,"result":result,"busy":False,"updated_at":datetime.now(timezone.utc).isoformat()}
            self.history=(self.history+[finished])[-10:]
            save_json(self.history_path,self.history)
            self.publish(message,phase=phase,result=result,busy=False)

    def _run(self,kind,include_catalog,expected_sha256,identifier,allow_older,reason,restore_path):
        try:
            if kind=="translations-check":
                result=self.engine.check_translations(self.publish)
                message=result["message"]
            elif kind=="translations-update":
                result=self.engine.update_translations(self.reload_translations,self.publish)
                message=result["message"]
            elif kind=="backup":
                result=self.engine.backup(include_catalog,reason,self.publish)
                message="备份完成："+result["path"]
            elif kind=="prepare":
                result=self.engine.prepare_archive(expected_sha256,self.publish)
                message=result.get("message","新版目录已准备好，请核对信息后确认备份并应用。")
            elif kind=="prepare-restore":
                result=self.engine.prepare_restore(restore_path,include_catalog,self.publish)
                message="备份已校验，请核对内容后确认导入。"
            else:
                self.publish("正在暂停采集并等待当前请求结束")
                with self.exclusive():
                    if kind=="restore":result=self.engine.restore(identifier,self.reload_catalog,self.publish)
                    else:result=self.engine.apply(identifier,allow_older,self.reload_catalog,self.publish)
                message=result["message"]
            self.finish(message,"completed",result)
        except Exception as error:
            self.finish("维护未完成："+str(error),"failed")
        finally:
            with self.lock:
                pending=self.pending_auto
                if pending is not None and self.settings.get("backup_on_completion") and not self.stopping and not self.state.get("busy"):
                    self.pending_auto=None
                    self.start("backup",reason=f"采集任务 {pending} 完成后自动备份")

    def on_collection_completed(self,job_id):
        with self.lock:
            if not self.settings.get("backup_on_completion") or self.stopping:return
            if self.state.get("busy"):
                self.pending_auto=job_id
            else:
                self.start("backup",reason=f"采集任务 {job_id} 完成后自动备份")

    def reserve_stop(self):
        with self.lock:
            if self.state.get("busy"):raise ValueError("维护任务正在运行，请完成后再停止服务。")
            if self.picker_lock.locked():raise ValueError("文件夹选择窗口已打开，请先完成选择再停止服务。")
            self.stopping=True

    def close(self):
        with self.lock:
            self.stopping=True
        if self.thread and self.thread.is_alive():self.thread.join()


def command_line():
    from urllib.request import Request,urlopen
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["backup","import"])
    parser.add_argument("--full",action="store_true",help="Include the large base catalog in backups")
    options=parser.parse_args()
    subprocess.run([sys.executable,str(ROOT/"launch.py"),"--no-browser"],cwd=ROOT,check=True)
    root="http://127.0.0.1:8765"
    with urlopen(root+"/api/status") as response:token=json.load(response)["action_token"]
    def post(action,payload):
        request=Request(root+"/api/maintenance/"+action,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json","X-Catalog-Token":token})
        with urlopen(request,timeout=30) as response:return json.load(response)
    def wait(task_id):
        last=""
        while True:
            with urlopen(root+"/api/maintenance",timeout=30) as response:state=json.load(response)
            completed=next((task for task in state.get("recent_tasks",[]) if task["id"]==task_id),None)
            if completed:
                print(completed["message"],flush=True)
                if completed["phase"]!="completed":raise RuntimeError(completed["message"])
                return completed
            if state["message"]!=last:print(state["message"],flush=True);last=state["message"]
            if state["id"]!=task_id:raise RuntimeError("Maintenance task changed. Check the web maintenance panel.")
            time.sleep(1)
    if options.action=="backup":
        task=post("backup",{"include_catalog":options.full});wait(task["requested_id"])
    else:
        task=post("prepare",{});state=wait(task["requested_id"])
        if state.get("result",{}).get("same_archive"):return
        candidate=state["result"]
        if candidate["older"]:raise RuntimeError("This is an older snapshot. Review it in the web maintenance panel.")
        print(f"New catalog records: {candidate['new']['gallery_count']:,}. Backing up and applying...",flush=True)
        task=post("apply",{"prepared_id":candidate["id"]});wait(task["requested_id"])


if __name__=="__main__":
    try:command_line()
    except Exception as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(1)
