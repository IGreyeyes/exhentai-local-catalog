"""Temporary library and simulated public release metadata for UI checks."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Catalog, Server
from test_desktop import make_library

root=Path(sys.argv[1]).resolve()
server=Server(('127.0.0.1',0),Catalog(make_library(root)))
server.release_history._request=lambda url:[{'tag_name':'v1.2.0-beta.10','name':'模拟预发布版本','prerelease':True,
    'published_at':'2026-10-11T00:00:00Z','body':'- **测试改进**：预发布内容\n\n<script>window.historyInjected=true</script>\n[危险链接](javascript:alert)\n```text\n代码示例\n```'}]
print(f'http://127.0.0.1:{server.server_port}',flush=True)
try:server.serve_forever()
finally:server.server_close()
