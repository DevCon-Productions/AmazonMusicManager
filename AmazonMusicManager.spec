# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for Amazon Music Manager (onedir).
#
#   Build:   pyinstaller --noconfirm AmazonMusicManager.spec
#   Output:  dist/AmazonMusicManager/   (folder with AmazonMusicManager.exe + deps)
#
# The app drives the user's INSTALLED Chrome over CDP, so Playwright's bundled
# browser binaries are NOT needed — but the playwright package and its node
# driver ARE, hence collect_all('playwright'). pycaw (volume control) needs
# comtypes collected too.

from PyInstaller.utils.hooks import collect_all

datas = [
    ('AMM_Icon.png', '.'),
    ('AmazonMusicManager_Logo2-Square.png', '.'),
    ('AmazonMusicManager_Logo2-Square.ico', '.'),
    ('AmazonMusicManager_TaskbarIcon2.png', '.'),
    ('AmazonMusicManager_TaskbarIcon2.ico', '.'),
    ('Developer.jpeg', '.'),
]
binaries = []
hiddenimports = []

for _pkg in ('playwright', 'pycaw', 'comtypes'):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AmazonMusicManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='AmazonMusicManager_Logo2-Square.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AmazonMusicManager',
)
