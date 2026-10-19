"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.2.2"
RELEASE_TITLE = "首次使用引导与采集交互修复"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """# v1.2.2 · 首次使用引导与采集交互修复

## v1.2.2 · 本次更新

- **首次启动检测数据库**：缺少数据库时直接显示下载入口、准确文件名和本机存放目录，可打开目录，放好文件后点击「重新检测」继续准备，避免停留在加载状态。
- **登录导入反馈更清楚**：导入加密登录文件时显示处理中、成功或失败原因；成功后立即更新源站与登录状态，并提示输入框留空即可沿用。登录信息载入后仍需验证源站访问。
- **明确开始采集入口**：将「预估采集耗时」改为「开始采集」。点击后先展示数量和耗时预估，确认后开始，继续保留访问验证与保护冷却。
- **记住 HTTP 采集代理**：填写、修改或清空后自动加密保存，下次打开自动回填；应用本次设置后用于采集。独立「网络代理」页的设置优先规则继续生效。
- **整理网络代理导航**：保留侧栏独立入口，移除资料库维护菜单中的重复项；页面标题和选中高亮保持一致，旧链接及前进后退继续可用。
- **README 开头补充准备说明**：醒目列出数据库下载链接、文件名和放置位置示例，与首次配置提示一致。

**首次使用**：程序包不包含作品数据库。请从 [URenko/e-hentai-db nightly 发布页](https://github.com/URenko/e-hentai-db/releases/tag/nightly) 下载 `e-hentai.db.zstd`，无需解压，放到 `ExCatalog.exe` 同一文件夹；已有 `data/catalog.sqlite3` 的用户无需重新下载。

**升级说明**：已有收藏数、阅读状态、采集队列、请求间隔和网络代理设置保留，无需重新导入资料或部署云端 Worker。旧版未保存的采集代理需填写一次；保存仅记住地址，修改后请应用设置并重新验证。登录 Cookie 仍只保留在本次运行的内存中，下次可导入加密登录文件。

下载 `ExCatalog-Windows-x64.zip`，完整解压后运行 `ExCatalog.exe`，同时提供 `SHA256SUMS.txt`。程序自带 Python，需要 Windows x64 和 WebView2。v1.1.0 及以上可通过「检查客户端更新」升级；遇到 WinError 10054 或 v1.0.0 首次升级时，请手动更新。

更新前建议备份并关闭应用，只替换程序、完整 `_internal` 和说明文件，保留 `data`、`logs`、`backups` 和作品目录压缩包。自动安装失败时恢复旧程序。
"""
