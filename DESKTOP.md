# 桌面版高级入口

完整的下载、首次启动、日常操作、升级和备份说明见 [README.md](README.md)。构建与源码测试见 [DEVELOPMENT.md](DEVELOPMENT.md)。本文仅说明桌面程序的可选入口，普通使用双击 `ExCatalog.exe` 即可。

## 指定已有资料的位置

默认资料目录为 `ExCatalog.exe` 所在文件夹。需要让程序使用另一位置的资料时，可在命令行运行：

```powershell
ExCatalog.exe --data-root "D:\原资料库文件夹"
```

指定包含 `data` 的父文件夹，不是 `data` 本身。程序运行文件仍来自当前 exe 的 `_internal`，资料、日志和备份按指定位置及已保存的备份设置管理。桌面版与浏览器版不能同时管理同一资料库。

开发项目中双击 `启动桌面.cmd` 时，若当前目录有 `desktop.py`、`client_version.py` 和 `.venv-desktop/Scripts/pythonw.exe`，脚本会优先启动当前源码并显式指定原项目为资料目录，避免新版在独立目录构建后仍启动 `dist` 中的旧 EXE。源码环境未齐备时，依次使用同目录的 `ExCatalog.exe`、`dist/ExCatalog/ExCatalog.exe`。完整发布包直接运行 `ExCatalog.exe`；切换前关闭原窗口，避免两个服务同时占用同一资料库。

## 浏览器备用入口

```powershell
ExCatalog.exe --browser
ExCatalog.exe --stop
```

第一条启动浏览器版，第二条停止浏览器备用入口的服务。浏览器备用入口关闭标签页后仍可能在后台运行，请停止服务后再切换回桌面窗口。桌面窗口使用自己的本机端口，请直接关闭窗口安全退出。

## 桌面隔离验证

```powershell
ExCatalog.exe --verify-desktop "D:\检查结果.json"
```

命令创建临时测试资料库，验证 WebView2 页面、导出、文件选择组件和正常退出，输出 JSON 报告及页面截图，不使用现有个人资料。

## 错误记录

桌面启动或退出错误记录在资料目录的 `logs/desktop.log`。缺少 WebView2 时会提示并打开微软官方网站，安装 Evergreen Runtime 后重新打开应用。分享排查信息时，先核对截图、路径和日志中是否包含不希望公开的个人资料。
