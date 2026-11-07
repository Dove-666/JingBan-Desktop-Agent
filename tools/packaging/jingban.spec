# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH).parents[1]
datas = [(str(root / "desktop_pet/ui/web_chat/index.html"), "desktop_pet/ui/web_chat"),
         (str(root / "desktop_pet/ui/web_chat/THIRD_PARTY_NOTICES.txt"), "desktop_pet/ui/web_chat")]
datas += [(str(root / "shared/reasoning/profiles.json"), "shared/reasoning")]
for distribution in ("keyring", "openai", "iroh"):
    datas += copy_metadata(distribution, recursive=True)
hidden = collect_submodules("desktop_pet") + [
    "keyring.backends.Windows", "keyring.backends.chainer", "keyring.backends.fail",
    "win32ctypes.pywin32.pywintypes", "win32ctypes.pywin32.win32cred",
]
a = Analysis([str(root / "main.py")], pathex=[str(root)],
             binaries=collect_dynamic_libs("iroh"), datas=datas,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[],
             excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "matplotlib", "torch", "tensorflow"],
             noarchive=False, optimize=0)
# Qt on supported Windows uses the OS's unversioned ICU API. An unrelated
# Poppler on a developer PATH exports version-suffixed ICU symbols instead.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != "icuuc.dll"]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="鲸伴", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, icon=str(root / "assets/branding/windows/whale-maid.ico"),
          version=str(root / "tools/packaging/windows_version.txt"))
b = Analysis([str(root / "tools/packaging/browser_host_entry.py")], pathex=[str(root)],
             binaries=[], datas=[], hiddenimports=["PySide6.QtCore", "PySide6.QtNetwork"],
             hookspath=[], runtime_hooks=[], excludes=["PyQt5", "PyQt6", "PySide2"], noarchive=False)
b.binaries = [entry for entry in b.binaries if Path(entry[0]).name.lower() != "icuuc.dll"]
browser_pyz = PYZ(b.pure)
browser_exe = EXE(browser_pyz, b.scripts, [], exclude_binaries=True, name="鲸伴网页桥", debug=False,
                  strip=False, upx=False, console=True,
                  icon=str(root / "assets/branding/windows/whale-maid.ico"))
coll = COLLECT(exe, browser_exe, a.binaries, a.datas, b.binaries, b.datas,
               strip=False, upx=False, name="鲸伴")
