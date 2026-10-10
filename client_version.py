"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.18"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """本次修复客户端更新时的连接中断，包含 v1.1.17 的全部功能。

1. **连接中断自动重试**：检查更新或下载时遇到 WinError 10054、超时及临时服务错误，最多自动重试 3 次，下载期间显示重试状态。
2. **更新包断点续传**：本次下载中断后保留已下载进度并尝试续传；服务器不支持续传时重新下载。完整下载后仍须通过大小和 SHA-256 校验。
3. **支持系统代理**：更新请求使用已配置的 HTTP/HTTPS 系统代理或环境代理，连接失败时交替尝试直连；收藏数采集的代理设置独立使用。
4. **手动下载入口**：更新持续失败时，弹窗提供「打开发布页手动下载」入口，并说明如何保留本地资料。
5. **旧版升级说明**：若旧客户端仍报 WinError 10054，请先用浏览器下载完整 ExCatalog-Windows-x64.zip，关闭应用后替换 ExCatalog.exe、完整 _internal 和说明文件，保留 data、logs、backups 和作品目录压缩包。此修复安装后才生效。"""
