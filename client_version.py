"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.7"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """1. **目录更新进度可见**：检查和应用按钮附近直接显示当前步骤、运行提示与耗时；数据库备份显示已复制页数及百分比，失败和中断也有明确说明。
2. **应用与备份结果保留**：完成后显示已生效的作品数、标签数和最新作品发布日期，以及本次旧版完整备份的保存路径；再次检查或重启后仍可查看最近一次应用结果。
3. **备份位置和数量明确**：更新区域同步显示实际备份目录及成功备份总数，区分完整和轻量备份；列表继续显示最近 10 份，未完成备份不计入。
4. **可跳过备份直接应用**：新增「不备份直接应用更新」，确认后跳过创建备份；保留校验、采集暂停和切换失败回退机制，成功后不保留该次旧版恢复副本。"""
