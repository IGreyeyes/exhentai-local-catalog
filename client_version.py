"""Public release metadata. Bump the version and notes for every client release."""

APP_VERSION = "1.1.20"
REPOSITORY = "IGreyeyes/exhentai-local-catalog"
PACKAGE_NAME = "ExCatalog-Windows-x64.zip"
RELEASE_NOTES = """本次修复全库上传时的「上传结果不正确，保留本地队列以便重试」。

1. **上传确认计数修复**：服务器改为按实际写入的记录行计算公开接受、待审核和保留数量，避免 D1 的历史及计数触发器被算成上传记录，导致客户端拒绝确认。
2. **原队列继续处理**：原上传确认及未完成队列保留。部署修复后的 Worker，再点击「立即同步」即可继续；失败批次已在服务器保存的相同记录不会重复入库。
3. **全库上传继续保留**：自己、他人及未知采集者的成功收藏数都可提交，原采集者信息保持；新来源和异常数据仍需审核，限流及每日额度继续生效。
4. **服务升级说明**：维护者须部署最新 cloudflare/worker.js，在 /health 核对 upload_confirmation: returning_rows。已有 v2 / v3 数据库、来源和开关保留，无需执行 SQL 或重建 D1。v1.1.19 客户端也可在服务器修复后继续同步，协议仍为 v3。
5. **客户端升级**：完整程序包升级保留 data、logs、backups、作品目录压缩包及已保存的同步设置；客户端更新重试、续传与系统代理修复继续保留。"""
