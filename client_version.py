"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.2.10"
RELEASE_TITLE = "精简上手指南与图文说明"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """# v1.2.10 · 精简上手指南与图文说明

## v1.2.10 · 本次更新

- **上手指南更简洁**：重写 README，围绕下载、首次启动、搜索、常用操作及升级备份说明使用方法，删去重复的功能介绍和参数细节。
- **新增中文操作配图**：用文件放置示意图说明数据库位置，用带标注的界面图说明搜索步骤；配图使用临时演示资料，并随客户端打包，支持离线查看。

**升级说明**：本次为使用说明更新，已有收藏数、阅读状态、采集队列和设置保留，无需重新下载数据库或重新采集。

下载 `ExCatalog-Windows-x64.zip`，完整解压后运行 `ExCatalog.exe`；同时提供 `SHA256SUMS.txt`。程序自带 Python，需要 Windows x64 和 WebView2。

v1.1.0 及以上可通过「检查客户端更新」升级；v1.0.0 首次升级或旧版更新失败时，请手动更新。更新前建议备份并关闭应用，只替换程序、完整 `_internal` 和说明文件，保留 `data`、`logs`、`backups` 和 `e-hentai.db.zstd`。自动安装失败时恢复旧程序。
"""
