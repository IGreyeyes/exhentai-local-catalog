"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.4"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """1. **启动时自动检查更新**：每次打开客户端后，在后台检查一次正式发布版本；切换页面不会重复检查。
2. **发现新版后提示选择**：展示新版更新内容，可选择「下载并更新」或「稍后更新」，由你决定何时更新。
3. **保持日常使用安静**：没有新版或网络检查失败时不弹出提示，可以继续使用，也可手动检查更新。
4. **避免弹窗重叠**：本版本的未读更新说明关闭后，再显示发现新版的提示；同一次启动只自动提示一次。"""
