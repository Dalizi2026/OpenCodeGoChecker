# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

路径全部相对仓库根目录，任何人 clone 下来都能直接构建 ——
不要写死 D:/... 这类作者机器上的绝对路径。
"""
import os

ROOT = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821  (SPEC 由 PyInstaller 注入)
ENTRY = os.path.join(ROOT, "opencode_go_checker.py")
ASSETS = os.path.join(ROOT, "assets")

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ASSETS, "app.ico"), "assets"),
        (os.path.join(ASSETS, "tailwindcss.min.js"), "assets"),
    ],
    hiddenimports=[
        # 模块级的 try/except 导入有被静态分析漏掉的风险，显式声明
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 单文件工具用不到，剔掉能显著减小体积
        "tkinter",
        "unittest",
        "pydoc_data",
        "test",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="OpenCodeGoChecker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                  # 无控制台窗口；出错会弹原生对话框 + 写 startup.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ASSETS, "app.ico")],
)
