"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.5"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """1. **收藏数分享教程**：导出区域新增可展开的 GitHub Discussions 教程，提供分享区与发帖入口，逐步说明注册登录、压缩 ZIP、填写帖子、上传附件和下载合并。
2. **自动生成分享信息**：导出成功后，根据本次文件生成记录数量、原抓取时间范围、客户端版本和分享日期；抓取时间按北京时间 / 香港时间（UTC+8）显示。
3. **复制标题和正文**：自动展开教程，提供可直接粘贴到分享帖的标题与正文；无法自动复制时可手动选择复制。取消或失败的导出会清除旧分享信息。
4. **明确公开范围**：教程说明只分享收藏数导出文件的 ZIP，并提醒核对采集者信息与采集范围。收藏数文件继续使用 version 2 格式。"""
