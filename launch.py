"""Start/open or stop the local catalog, without additional dependencies."""

from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser

from first_run import prepare_library

ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:8765"
APP_ID = "local-tag-catalog-v1"


def running():
    try:
        with urlopen(BASE_URL + "/api/status", timeout=2) as response:
            data = json.load(response)
        if data.get("app_id") != APP_ID:
            raise RuntimeError("Port 8765 is occupied by another application.")
        return True
    except URLError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop", action="store_true")
    options = parser.parse_args()
    if options.stop:
        if not running():
            print("The catalog is already stopped.")
            return
        runtime_path = ROOT / "logs" / "server-8765.json"
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        request = Request(BASE_URL + "/api/shutdown", data=b"", headers={"X-Local-Token": runtime["token"]}, method="POST")
        with urlopen(request, timeout=5) as response:
            json.load(response)
        deadline = time.monotonic()+35
        while runtime_path.exists() and time.monotonic()<deadline:
            time.sleep(0.25)
        if runtime_path.exists():
            raise RuntimeError("The current request is still finishing. Please wait before restarting.")
        print("Catalog stopped.")
        return
    if not running():
        prepare_library(ROOT)
        (ROOT / "logs").mkdir(exist_ok=True)
        with (ROOT / "logs" / "server.log").open("ab") as log:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen([sys.executable, "-u", str(ROOT / "app.py")], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, start_new_session=os.name != "nt")
        for _ in range(60):
            if running():
                break
            if process.poll() is not None:
                raise RuntimeError("Startup failed. See logs/server.log.")
            time.sleep(0.25)
        else:
            raise RuntimeError("Startup timed out. See logs/server.log.")
    print("Catalog ready: " + BASE_URL)
    if not options.no_browser:
        webbrowser.open(BASE_URL)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
