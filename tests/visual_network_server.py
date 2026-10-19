"""Disposable network-settings UI fixture; all test targets use a local fake proxy."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from network_settings import TEST_TARGETS
from tests.test_desktop import make_library
from tests.test_network_settings import start_proxy


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    database = make_library(root)
    proxy, proxy_thread = start_proxy()
    for key, (label, _) in list(TEST_TARGETS.items()):
        TEST_TARGETS[key] = (label, "http://source.example.invalid/")
    server = Server(("127.0.0.1", 0), Catalog(database))
    print(json.dumps({"base": f"http://127.0.0.1:{server.server_port}", "proxy_port": proxy.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join()
