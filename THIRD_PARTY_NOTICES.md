# 第三方数据说明

## EhTagTranslation 译文数据库

- 项目：[EhTagTranslation/Database](https://github.com/EhTagTranslation/Database)
- 版权归属：EhTagTranslation 全体编辑者。
- 文本许可：[署名—非商业性使用—相同方式共享 3.0 中国大陆（CC BY-NC-SA 3.0 CN）](https://creativecommons.org/licenses/by-nc-sa/3.0/cn/)。
- 完整许可文本：[上游 LICENSE.md](https://github.com/EhTagTranslation/Database/blob/master/LICENSE.md)。
- 数据形式：官方发布的 `db.text.json.gz`，解压为本地 `data/tag-translations.json`，保留官方 JSON 内容。具体版本、下载地址及校验值记录于 `data/tag-translations-info.json`。
- 本程序选取原始标签的中文名称和命名空间名称用于显示与搜索，不修改源站标签键；少量中文检索同义词由本程序补充，独立于官方词库。
- 没有收录译文的标签显示原文。译文覆盖率和内容准确性取决于上游数据；本地作品目录与词库的更新日期也可能不同。

使用或再分发译文数据时应保留署名与许可链接，遵守非商业性使用和相同方式共享要求。上游 README 另请下游项目提交数据库使用说明 Issue，项目维护者可按上游要求登记使用情况。

## 作品元数据库

本地基础作品目录来自用户下载的 [URenko/e-hentai-db](https://github.com/URenko/e-hentai-db/releases/tag/nightly)。它与标签译文库是两个独立数据来源。

## 桌面版运行组件

Windows 桌面版额外包含 Python 运行环境、[pywebview](https://github.com/r0x0r/pywebview)（BSD 3-Clause）、[Python.NET](https://github.com/pythonnet/pythonnet)（MIT）及其运行依赖。使用 PyInstaller 构建，适用其允许分发打包程序的许可例外。随发行包附带的许可文本位于 `_internal/licenses`。

内嵌网页使用 Microsoft Edge WebView2 SDK 的运行组件；WebView2 Evergreen Runtime 由系统或用户通过微软官方下载页安装，适用微软相关许可。程序包不包含作品数据库、译文数据库、个人资料或登录信息。
