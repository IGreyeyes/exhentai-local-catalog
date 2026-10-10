"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.21"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """本次为云端管理页新增可信来源待审核数据的一键审批。

1. **可信来源一键审批**：管理页「待审核数据」新增「一键审批可信来源的待审核数据」。读取数量并确认一次后，自动分批处理全部现有可信来源候选，包括异常候选，不限于当前显示的 100 条。
2. **保留其他来源与较新数据**：逐批检查来源仍为可信；未可信和封禁来源继续待审，公共库中时间更新或相同的记录保留，并清理对应的过时候选。
3. **审批进度与停止**：显示已批准、保留已有版本和跳过数量，可停止后续批次；失败或关闭页面后，已完成的审批保留，重新读取后可继续剩余候选。处理中新增候选留到下一次。
4. **并发与操作记录**：每批读取后发生来源或候选变化时保留待审，不用旧数据覆盖；批次审批保存操作记录和公共历史，已批准数据可通过 v3 客户端同步。
5. **升级说明**：维护者部署最新 cloudflare/worker.js，重新打开 /admin 并读取信息；/health 的 trusted_pending_bulk_approval 应为 true。已有 v2 / v3 数据库无需执行 SQL 或重建，v1.1.19 及以上客户端继续兼容。全库上传和上传确认计数修复继续保留。"""
