from pathlib import Path
from importlib.metadata import distribution
import sys
from PyInstaller.utils.hooks import collect_data_files

project = Path(SPECPATH)
desktop_data = collect_data_files("webview", includes=["lib/**/*", "js/**/*"])
license_data = []
for package in ("pywebview", "pythonnet", "clr_loader", "cffi", "bottle", "proxy_tools", "typing_extensions", "pycparser", "pyinstaller"):
    metadata = distribution(package)
    for entry in metadata.files or []:
        if "license" in str(entry).lower() or str(entry).lower().endswith("copying.txt"):
            path = Path(metadata.locate_file(entry))
            if path.is_file():
                license_data.append((str(path), "licenses/" + package))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    license_data.append((str(python_license), "licenses/python"))
a = Analysis(
    [str(project / "desktop.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / "static"), "static"), *desktop_data, *license_data],
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "clr_loader", "pythonnet", "compression.zstd", "tkinter.filedialog"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "webview.platforms.gtk", "webview.platforms.cocoa", "webview.platforms.qt"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ExCatalog", debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ExCatalog")
