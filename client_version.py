"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.6"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """1. **搜索结果统一视图**：标签与标题搜索结果采用与「已记录」一致的卡片，支持最小化、紧凑 + 标签、扩展、缩略图四种显示方式。
2. **完整标签与收藏数**：扩展视图按命名空间完整展示标签，匹配标签高亮；统一彩色分类、醒目收藏数和原采集者信息，未知收藏数与零收藏分别显示。
3. **分别记住显示方式**：搜索页与记录页各自保存选择，重启和升级后继续使用；切换视图保留搜索条件、排序和当前页。
4. **明确目录暂缺的处理**：导入提示说明缺失作品仍按 GID 保存收藏数，更新到包含相同 GID 的作品目录后自动匹配，无需再次导入；补充完整更新与重启场景的回归验证。"""
