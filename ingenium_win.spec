# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. No copying, adaptation, redistribution, reverse
# engineering or commercial use without the author's written permission.
# See LICENCE.txt. Every distributed copy carries a build marker.
# -*- mode: python ; coding: utf-8 -*-
#
# Ingenium -- standalone Windows app (single .exe).
#
#   R75: with INGENIUM_ONEDIR=1 in the environment the same recipe makes the
#   app as a folder instead (Ingenium.exe beside an _internal folder). That is
#   what Ingenium-Setup.exe installs: nothing is unpacked at each start, so it
#   opens at once, and the engine sits unpacked beside it (engine\windows-x64).
#
#   Built by "Build Ingenium (Windows).bat" ON A WINDOWS PC. You do not run this file
#   yourself. It mirrors the macOS recipe (ingenium.spec) but targets the
#   Windows web-view backend and produces one portable executable.
#
# Uses only PyInstaller arguments that exist in both 5 and 6.

import json
import os
import re

from PyInstaller.utils.hooks import collect_all

# R65: who the app is comes from app.json (Ingenium's builder writes a new one
# for each new app): the program's file name and its window title.
try:
    with open(os.path.join(SPECPATH, 'app.json'), encoding='utf-8') as _fh:
        _cfg = json.load(_fh)
except Exception:
    _cfg = {}
APP_FILE = re.sub(r'[^A-Za-z0-9._-]', '', str(_cfg.get('file_name') or '')) or 'Ingenium'
# 6.70: the program carries its own name. Her words (10 November 2026): "I want the "exe" to be removed from the app's name when I
# build it inside of Ingenium." Without a version resource Windows names a program by its file, "Name.exe" (Task Manager, the
# taskbar, notifications, Open with); its FileDescription and ProductName are now the app's own name (app.json "name").
# PyInstaller's EXE takes a VSVersionInfo for version= (PyInstaller/building/api.py, 6.22.3).
APP_NAME = str(_cfg.get('name') or '').strip() or APP_FILE
APP_VERSION = str(_cfg.get('version') or os.environ.get('INGENIUM_VERSION') or '1.0').strip()
_vn = ([min(65535, int(x)) for x in re.findall(r'\d+', APP_VERSION)][:2] + [0, 0, 0, 0])[:4]
try:
    from PyInstaller.utils.win32 import versioninfo as _vi
    VERSION_INFO = _vi.VSVersionInfo(
        ffi=_vi.FixedFileInfo(filevers=tuple(_vn), prodvers=tuple(_vn)),
        kids=[_vi.StringFileInfo([_vi.StringTable('040904B0', [
                  _vi.StringStruct('FileDescription', APP_NAME), _vi.StringStruct('ProductName', APP_NAME),
                  _vi.StringStruct('FileVersion', APP_VERSION), _vi.StringStruct('ProductVersion', APP_VERSION),
                  _vi.StringStruct('InternalName', APP_FILE), _vi.StringStruct('OriginalFilename', APP_FILE + '.exe')])]),
              _vi.VarFileInfo([_vi.VarStruct('Translation', [1033, 1200])])])
except Exception:
    VERSION_INFO = None
ONEDIR = os.environ.get('INGENIUM_ONEDIR') == '1'
_own = [('ingenium.html', '.'), ('ingenium.ico', '.'), ('ingenium.png', '.')]
for _f in ('app.json', 'THIRD_PARTY_NOTICES.txt', 'LICENCE.txt'):
    if os.path.isfile(os.path.join(SPECPATH, _f)):
        _own.append((_f, '.'))
# The native engine travels packed (one file inside the exe); the app unpacks
# it once into its data folder. Built without it, the app uses the slower
# engine inside the page.
if ONEDIR:
    print('ingenium: folder build; the installer places the engine beside the program')
    # 6.57: the installed app carries the speech models dictation writes with, when the build fetched them (build-windows.yml)
    if os.path.isdir(os.path.join(SPECPATH, 'speech')):
        _own.append(('speech', 'speech'))
    # 6.59: and the folder search (rag/*.js and rag/vendor/), beside the page, when the build fetched its files (build-windows.yml);
    # the portable exe does not carry it (84 MB more to unpack at every start): there the chat's folders are not offered
    if os.path.isfile(os.path.join(SPECPATH, 'rag', 'main.js')) and os.path.isdir(os.path.join(SPECPATH, 'rag', 'vendor')):
        _own.append(('rag', 'rag'))
elif os.path.isfile(os.path.join(SPECPATH, 'engine', 'windows-x64.zip')):
    _own.append((os.path.join('engine', 'windows-x64.zip'), 'engine'))
else:
    print('ingenium: no engine/windows-x64.zip; the app is built without its native engine')

extra_datas = []
extra_binaries = []
extra_hidden = []

# On Windows, pywebview draws its window with the Edge WebView2 backend and
# loads support code through pythonnet (the "clr" module). Those packages, and
# pywebview's own on-disk files, must travel inside the exe. A package that is
# absent is simply skipped, so this stays safe if a name is not installed.
# certifi travels too: its cacert.pem is a DATA file, and the relay verifies
# https calls (Ollama cloud, Claude) against it.
for _pkg in ('webview', 'proxy_tools', 'clr', 'pythonnet', 'certifi'):
    try:
        _d, _b, _h = collect_all(_pkg)
        extra_datas += _d
        extra_binaries += _b
        extra_hidden += _h
    except Exception as _e:
        print('ingenium: nothing collected for %s (%s)' % (_pkg, _e))

a = Analysis(
    ['ingenium_app.py'],
    pathex=[],
    binaries=extra_binaries,
    # the app page and the seal travel inside the exe
    datas=extra_datas + _own,
    # Named outright because PyInstaller cannot see them: pywebview chooses its
    # backend while running, so nothing in the source imports these by name.
    # Left out, the app builds cleanly and then cannot draw a window.
    hiddenimports=[
        'webview.platforms.winforms',
        # 6.83: Fabrica brings everything from GitHub with git's own protocol (dulwich, a git written in Python; app_hub_683.py)
        'dulwich', 'dulwich.client', 'dulwich.repo', 'dulwich.objects', 'dulwich.pack', 'dulwich.protocol', 'urllib3',
        # 6.86: Fabrica reads every repository on this computer from the drive (app_disk_686.py)
        'dulwich.object_store', 'dulwich.index',
        'webview.platforms.edgechromium',
        # R78: the clipboard for the edit panel's Paste and Copy (ingenium_app._win_clipboard_*)
        'ctypes',
        'ctypes.wintypes',
        'clr',
        # R57: the relay chain. R56 and earlier named none of these on Windows,
        # which is exactly why the frozen exe could not reach Ollama or any
        # cloud provider: the modules were never carried inside the exe.
        'urllib.request',
        'urllib.error',
        'urllib.parse',
        'http.client',
        'http.server',
        'socketserver',
        'ssl',
        '_ssl',
        'certifi',
        'email',
        'email.message',
        'email.parser',
        'email.feedparser',
        'email.header',
        'email.charset',
        'email.utils',
        'quopri',
        'uu',
        'mimetypes',
        'socket',
        'selectors',
        'tempfile',
        'json',
        'pathlib',
        'datetime',
        'shutil',
        'subprocess',
        'threading',
        'functools',
        'base64',
        'hmac',
        'hashlib',
    ] + extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# One portable file: binaries and data are folded into the exe itself, so there
# is a single Ingenium.exe with nothing to keep beside it. The launcher finds
# the bundled page through sys._MEIPASS, so this needs no folder.
if ONEDIR:
    # R75: the folder form, for the installer
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_FILE,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon='ingenium.ico',
        version=VERSION_INFO,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_FILE,
    )
else:
  exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_FILE,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon='ingenium.ico',
    version=VERSION_INFO,
)
