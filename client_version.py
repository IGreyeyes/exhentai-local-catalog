"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.2.11"
RELEASE_TITLE = "合并结果提示与目录下载引导"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """# v1.2.11 · 合并结果提示与目录下载引导

## v1.2.11 · 本次更新

- **合并结果更醒目**：收藏数与失败记录合并时，在操作区显示「正在合并记录」和进度提示；实际完成后以醒目状态卡片显示「合并成功」，分别列出成功收藏数和失败记录的新增、更新、保留或跳过数量。结果在自动刷新及重新打开页面后仍可查看。
- **检查、导出与失败都有反馈**：文件检查完成后明确提示「等待确认合并」，导出显示成功或取消；合并和检查失败时就地显示原因。连接中断时提示结果待确认，恢复连接后重新核对后台状态，只有实际完成才显示成功。
- **作品目录下载引导更醒目**：「更新作品目录」新增独立下载提示卡片和「前往 GitHub 下载目录」按钮，清楚展示下载新版文件、放到指定位置、检查后确认应用三个步骤，小窗口也能完整查看。

**升级说明**：已有收藏数、失败记录、阅读状态、采集队列和设置保留，无需重新下载数据库、重新采集或重新部署云端服务。检查文件只预览，确认合并后才会备份并写入记录；更新作品目录仍需检查并确认应用。

下载 `ExCatalog-Windows-x64.zip`，完整解压后运行 `ExCatalog.exe`；同时提供 `SHA256SUMS.txt`。程序自带 Python，需要 Windows x64 和 WebView2。

v1.1.0 及以上可通过「检查客户端更新」升级；v1.0.0 首次升级或旧版更新失败时，请手动更新。更新前建议备份并关闭应用，只替换程序、完整 `_internal` 和说明文件，保留 `data`、`logs`、`backups` 和 `e-hentai.db.zstd`。自动安装失败时恢复旧程序。
"""
