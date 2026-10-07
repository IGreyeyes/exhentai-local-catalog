"""A desktop-owned service that shuts down without interrupting writes."""

from pathlib import Path
import threading

from app import Catalog, Server
from first_run import prepare_library
from maintenance import recover_import
from runtime_paths import library_lock


class DesktopSession:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.server = None
        self.thread = None
        self.base_url = None
        self._lease = None
        self._lifecycle = threading.RLock()

    def start(self, progress=None):
        with self._lifecycle:
            self._lease = library_lock(self.root)
            self._lease.__enter__()
            try:
                prepare_library(self.root, progress)
                database = self.root / "data" / "catalog.sqlite3"
                recover_import(database)
                self.server = Server(("127.0.0.1", 0), Catalog(database))
                self.base_url = f"http://127.0.0.1:{self.server.server_port}"
                self.thread = threading.Thread(target=self._serve, name="desktop-service", daemon=False)
                self.thread.start()
                return self.base_url
            except BaseException:
                if self.server is not None:
                    self.server.server_close()
                self._release()
                raise

    def _serve(self):
        try:
            self.server.serve_forever()
        finally:
            self.server.server_close()
            collector = self.server.collector.thread
            if collector:
                collector.join()

    def _release(self):
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None

    def close(self, wait_for_maintenance=False):
        with self._lifecycle:
            if self.server is not None and self.thread and self.thread.is_alive():
                if wait_for_maintenance:
                    maintenance = self.server.maintenance.thread
                    if maintenance:
                        maintenance.join()
                    with self.server.maintenance.picker_lock:
                        pass
                # Refuse to close during a backup, directory swap or open picker.
                self.server.maintenance.reserve_stop()
                self.server.shutdown()
                self.thread.join()
            self._release()
