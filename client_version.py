"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.2.1"
RELEASE_TITLE = "网络代理设置与连接测试"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """# v1.2.1 · 网络代理设置与连接测试

## v1.2.1 · 本次更新

- **独立网络代理设置**：侧栏与资料库维护新增「网络代理」，可设置启用状态、地址、端口、可选用户名和密码，以及按域名生效的代理列表。支持 HTTP / Mixed 代理端口和 HTTPS 站点；关闭自定义代理时可选择跟随系统或直接连接。
- **一键连接测试**：可选择 E-Hentai、ExHentai 或 GitHub，使用当前表单测试，显示连接方式、HTTP 状态与耗时，并区分认证失败、访问受限和连接失败。测试不会自动保存设置，也不携带源站 Cookie；网络连通不代表账号登录或作品访问验证成功。
- **统一客户端联网设置**：保存后用于收藏数采集、封面、在线同步、中文词库、更新日志及客户端更新。自定义代理连接失败时不自动绕过代理；代理列表以外的域名直接连接，本机资料库页面保持直连。
- **加密保存与采集衔接**：代理设置和认证信息使用当前 Windows 用户加密保存，重启后恢复，密码不回显并可单独清除。保存前需暂停采集，保存后重新验证源站访问；已有数据、任务队列和保护冷却保留。

**升级说明**：默认关闭自定义代理并沿用系统连接方式。旧采集表单的临时代理仅在关闭自定义代理且选择「跟随系统」时生效。无需重新导入资料、下载作品目录或部署云端 Worker；外部浏览器继续使用自身的网络设置。

下载 `ExCatalog-Windows-x64.zip`，完整解压后运行 `ExCatalog.exe`，同时提供 `SHA256SUMS.txt`。程序自带 Python，需要 Windows x64 和 WebView2。v1.1.0 及以上可通过「检查客户端更新」升级；遇到 WinError 10054 或 v1.0.0 首次升级时，请手动更新。

更新前建议备份并关闭应用，只替换程序、完整 `_internal` 和说明文件，保留 `data`、`logs`、`backups` 和作品目录压缩包。自动安装失败时恢复旧程序。
"""
