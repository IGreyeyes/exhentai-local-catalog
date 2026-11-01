# EX资料库

在 Windows 本地用中文或英文标签查找 E-Hentai / ExHentai 作品，查看收藏数、记录阅读状态。

**[下载客户端](https://github.com/IGreyeyes/exhentai-local-catalog/releases/latest) · [下载作品数据库](https://github.com/URenko/e-hentai-db/releases/tag/nightly) · [反馈问题](https://github.com/IGreyeyes/exhentai-local-catalog/issues)**

适用于 Windows 10 / 11 x64，自带 Python，无需安装 Python 或输入命令。缺少 WebView2 时，按启动提示安装 [微软 WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。

## 三步开始

1. **下载并解压程序**：在客户端发布页的 **Assets（附件）** 中下载 `ExCatalog-Windows-x64.zip`，完整解压到固定文件夹。不要在 ZIP 内运行，也不要只取出 exe；`Source code` 是源码。
2. **放入数据库**：在数据库下载页下载 **`e-hentai.db.zstd`**，无需解压，放到 **`ExCatalog.exe` 同一文件夹**，不要放进 `_internal`。
3. **双击启动**：运行 `ExCatalog.exe`，等待自动解压数据库、建立索引和准备中文词库，完成后进入搜索页。首次准备需要一些时间，请保持窗口打开。

![首次使用：完整解压程序，将 e-hentai.db.zstd 放在 ExCatalog.exe 旁，再双击启动](static/readme/quick-start.png)

> 程序包不含作品数据库。已有 `data/catalog.sqlite3` 的用户可直接启动，无需重新下载。提示「未找到数据库」时，点击「打开存放目录」，放好文件后点「重新检测」。

## 第一次搜索

1. 在 **标签搜索** 输入 `中文` 或 `language:chinese`，从建议中选中标签。
2. 按需填写标题、选择分类和排序，然后点击 **搜索资料库**。
3. 点击结果中的作品标题，在默认浏览器打开源站页面。需要排除标签时，展开 **更多筛选**。

![搜索界面操作图：先选标签，再设置分类和排序，最后点击搜索资料库；左侧进入记录与维护](static/readme/search-guide.png)

*图中为临时演示资料。中文词库暂不可用时，可先用 `language:chinese` 等英文标签搜索。*

## 常用操作

| 想做什么 | 到哪里、怎么操作 |
| --- | --- |
| 补充收藏数 | 先搜索，再打开 **收藏数采集**；配置源站访问与采集策略 → **应用本次设置** → 验证访问 → 预估并确认开始。 |
| 查看记录、标记看过 | 打开 **已记录**，点击爱心标记「看过」、划线圆圈标记「不看」；再次点击取消。 |
| 备份资料 | **资料库维护 → 备份资料**，选择保存位置，点击 **立即备份**；换电脑时建议勾选「同时备份作品目录」。 |
| 恢复备份 | **资料库维护 → 恢复备份**，选择具体的一份备份文件夹，检查后确认恢复。 |
| 更新作品目录 | 下载新的 `e-hentai.db.zstd` 放回程序文件夹，在 **资料库维护 → 作品目录** 检查，再点击 **备份旧版并应用更新**。 |
| 导入他人的收藏数 | **资料库维护 → 收藏数导入导出**，选择 `.ehfavorites.json` 文件，检查后确认合并。 |
| 在线同步收藏数 | **资料库维护 → 在线同步**，按需勾选上传或下载并保存；两项首次默认关闭，开启上传前请阅读分享范围。 |
| 配置网络 | 打开 **网络代理**，保存设置后使用页面上的连接测试。 |

收藏数显示「未知」时，可自行采集、导入分享文件或开启公共数据下载。数据库不自带收藏数，采集需要能访问所选源站。平时关闭窗口即可退出，等待当前请求保存完成。

## 升级与保留资料

点击客户端的 **检查客户端更新**，按提示下载并安装；仅检查版本不会自动安装。

手动升级时，先在维护页备份并关闭应用。将新版解压到临时文件夹，再替换原目录中的 `ExCatalog.exe`、完整 `_internal` 和说明文件。

**请保留原有 `data`、`logs`、`backups` 和 `e-hentai.db.zstd`。** 个人收藏数、阅读状态和采集进度保存在 `data` 中，无需重新采集或下载数据库。v1.0.0 首次升级、旧版检查更新报错时，使用手动升级。

## 遇到问题

| 情况 | 处理方法 |
| --- | --- |
| 中文标签没有建议 | **资料库维护 → 中文词库**，安装或更新词库。 |
| 换了数据库文件，作品没变化 | 在 **资料库维护 → 作品目录** 检查并应用，放入文件不会自动替换现有资料库。 |
| 封面加载失败 | 点击失败封面旁的 **测试封面连接**，网络恢复后再点 **重试封面**。 |
| 采集验证失败或限流 | 先在浏览器确认同一源站可访问，再核对登录与代理设置；限流时按提示等待后重新验证。 |
| 更新检查或下载失败 | 点击 **重新检查**，或从[发布页](https://github.com/IGreyeyes/exhentai-local-catalog/releases/latest)下载完整包手动升级。 |

更多帮助见客户端 **联系我们**；完整版本历史见 **更新日志**。

---

[v1.2.10 更新说明](RELEASE_NOTES.md) · [源码与构建](DEVELOPMENT.md) · [桌面高级选项](DESKTOP.md) · [同步服务部署](https://github.com/IGreyeyes/exhentai-local-catalog/blob/master/cloudflare/README.md)

数据来源：[URenko/e-hentai-db](https://github.com/URenko/e-hentai-db)；中文标签：[EhTagTranslation/Database](https://github.com/EhTagTranslation/Database)。译文归 EhTagTranslation 全体编辑者所有，采用 [CC BY-NC-SA 3.0 CN](https://creativecommons.org/licenses/by-nc-sa/3.0/cn/) 许可。详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
