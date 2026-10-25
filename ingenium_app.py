#!/usr/bin/env python3
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. No copying, adaptation, redistribution, reverse
# engineering or commercial use without the author's written permission.
# See LICENCE.txt. Every distributed copy carries a build marker.
"""
Ingenium — desktop launcher.

This is a thin native-window shell around the Ingenium studio. All of the
app's behaviour lives in the bundled HTML/JavaScript file (ingenium.html);
this script only opens it in a proper desktop window using the system's native
web view. The app still runs entirely offline: nothing is sent anywhere.

Run it with:   python3 ingenium_app.py
"""

import os
import sys

# R57: every module this program could need in a frozen build is imported here,
# at the top, where PyInstaller's static analysis can see it. Several of these
# used to be imported inside function bodies; PyInstaller never sees those, the
# frozen app then lacks them, and the first call fails with the notorious
# "[Errno 2] No such file or directory: base_library.zip". Static imports end
# that class of failure entirely. `webview` itself stays lazy on purpose: this
# script must be able to open the browser instead when pywebview is absent.
import base64
import datetime
import math
import random
import struct
from collections import Counter, defaultdict
import functools
import hashlib
import http.server
import json
import pathlib
import re
import shutil
import socketserver
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# Certificate roots for calls to https providers (ollama-cloud, Claude). The
# frozen app cannot see the system Python's certificate file, so certifi's own
# copy is bundled by the spec and pointed at here. urllib reads SSL_CERT_FILE
# when building its default SSL context.
# R84: an app built without certifi used to fall back on the certificate
# folder its Python was compiled to look in, which exists on some Macs (one
# with Homebrew, say) and not on others. There every download from Hugging
# Face was refused, which is why Velox would not install on a Mac mini while
# it did on another Mac. Without certifi, the system's own bundle is used
# (macOS keeps one at /etc/ssl/cert.pem); Windows reads its own store.
def _find_cert_file():
    try:
        import certifi as _certifi
        p = _certifi.where()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    if not sys.platform.startswith("win"):
        for p in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                  "/etc/pki/tls/certs/ca-bundle.crt"):
            if os.path.isfile(p):
                return p
    return None


_CERT_FILE = _find_cert_file()
if _CERT_FILE:
    os.environ.setdefault("SSL_CERT_FILE", _CERT_FILE)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CERT_FILE)

# R65: who this app is. app.json (the same file the phone and tablet builds
# read) names the app; Ingenium's builder writes a new one for each new app,
# so a new app keeps its own window title and its own data folder and never
# touches Ingenium's. Without the file, the app is Ingenium.
def _read_app_json():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    except Exception:
        exe_dir = here
    for base in (getattr(sys, "_MEIPASS", None), here, exe_dir, os.path.join(exe_dir, "..", "Resources")):
        if not base:
            continue
        p = os.path.join(base, "app.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    d = json.load(fh)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
    return {}


_APP_CFG = _read_app_json()
APP_NAME = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ._-]", "", str(_APP_CFG.get("name") or ""))).strip() or "Ingenium"
APP_FOLDER = re.sub(r"[^A-Za-z0-9._-]", "", str(_APP_CFG.get("file_name") or "")) or "Ingenium"

# The window title and starting size. The app itself is responsive, so these are
# just sensible defaults; the window can be resized and maximised freely.
# 6.0: they are only the smallest first size. The window opens where it was last
# left (window.json, beside the stored session), and on a first start it takes most
# of the screen (_window_start). The owner's words (20 October 2026): "it does not
# expand and stays lock in this compact view which is really annoying because I have
# big curved screen desktop display".
WINDOW_TITLE = APP_NAME
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 820
MIN_WIDTH = 380      # the app works down to a narrow width
MIN_HEIGHT = 560
ACCENT_AMETHYST = "#6a3fb5"   # the Ingenium jewel purple


def tell_user(text, title=None):
    """Say something the person will actually see.

    The packaged app is built windowed, with no console attached, so anything
    written to stderr disappears. Every message below therefore goes to a native
    dialog on Windows, and to the terminal when one exists (running the script
    directly, or on macOS and Linux).
    """
    title = title or APP_NAME
    try:
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x40)
            return
        except Exception:
            pass
    if sys.platform == "darwin":
        # An app opened from the Dock or Finder has no terminal either, so the
        # message goes to a real dialog. osascript is present on every Mac.
        try:
            import subprocess
            body = str(text).replace("\\", "\\\\").replace('"', '\\"')
            head = str(title).replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 'display dialog "%s" with title "%s" buttons {"OK"} default button "OK"'
                 % (body, head)],
                check=False, timeout=120)
            return
        except Exception:
            pass


def resource_path(filename):
    """Return an absolute path to a file that sits next to this script.

    This also works when the app has been frozen into a single executable with
    PyInstaller, where bundled files live in a temporary folder exposed as
    sys._MEIPASS.
    """
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def find_app_html():
    """Locate the bundled app HTML, with several sensible fallbacks.

    A frozen single-file build carries the page inside itself and finds it
    through resource_path first, so it needs no folder at all. The extra
    candidates cover running from source, and a build started from a different
    working folder.
    """
    here_script = os.path.dirname(os.path.abspath(__file__))
    try:
        here_exe = os.path.dirname(os.path.abspath(sys.executable))
    except Exception:
        here_exe = here_script
    candidates = [
        resource_path("ingenium.html"),
        os.path.join(here_script, "ingenium.html"),
        os.path.join(here_exe, "ingenium.html"),
        os.path.join(os.getcwd(), "ingenium.html"),
    ]
    seen = set()
    for path in candidates:
        if path and path not in seen:
            seen.add(path)
            if os.path.isfile(path):
                return path
    return None


def friendly_missing_dependency_message():
    """A clear, non-technical message shown if pywebview is not installed."""
    return (
        "\n"
        "%s needs one small free component to open its window.\n"
        "\n"
        "Please install it once by running this in a terminal or command prompt:\n"
        "\n"
        "    pip install pywebview\n"
        "\n"
        "On Windows you may instead need:   py -m pip install pywebview\n"
        "On some Linux systems you may also need a web view backend, e.g.:\n"
        "    sudo apt install python3-webview   (or)   pip install pywebview[qt]\n"
        "\n"
        "Then run this program again.\n"
    ) % APP_NAME


def storage_dir():
    """A persistent place for the web view's own storage.

    pywebview runs in PRIVATE MODE by default, which discards localStorage when
    the window closes. Everything written in the desktop app was therefore lost
    on exit, not only a sticky note: models, documents, reminders, all of it.
    A onefile build also unpacks into a temporary directory that is deleted
    afterwards, so the storage must live in the user's own profile and not
    beside the executable.
    """
    import os
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    path = os.path.join(base, APP_FOLDER, "storage")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        log_problem("storage_dir: could not create %r" % path)
        return None


def crash_log_path():
    """Beside the program if that is writable, otherwise the home folder."""
    import os
    try:
        base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
        probe = os.path.join(base, ".ingenium_write_test")
        with open(probe, "w") as fh:
            fh.write("")
        os.remove(probe)
        return os.path.join(base, "ingenium_crash.log")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "ingenium_crash.log")


def diag_path():
    """Where the load watchdog writes what it found. Beside the crash log."""
    try:
        return crash_log_path().replace("ingenium_crash.log", "ingenium_diag.txt")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "ingenium_diag.txt")


def write_diag(lines):
    """Append plain findings to the diag file; never raises."""
    try:
        import datetime
        with open(diag_path(), "a", encoding="utf-8") as fh:
            fh.write("---- %s ----\n" % datetime.datetime.now().isoformat(timespec="seconds"))
            for line in lines:
                fh.write("%s\n" % line)
    except Exception:
        pass


def log_problem(tag, exc_info=None):
    """Record a fault so a crash leaves evidence instead of vanishing."""
    import datetime, traceback
    try:
        with open(crash_log_path(), "a", encoding="utf-8") as fh:
            fh.write("\n=== %s  %s ===\n" % (datetime.datetime.now().isoformat(timespec="seconds"), tag))
            if exc_info and exc_info[0] is not None:
                traceback.print_exception(exc_info[0], exc_info[1], exc_info[2], file=fh)
            elif sys.exc_info()[0] is not None:
                traceback.print_exc(file=fh)
    except Exception:
        pass


# 6.0: one save at a time. pywebview's file dialog keeps the place chosen in one slot
# per window, so two dialogs asked for at once could hand each other's places back:
# five packages saved together came out under one another's names (the Forge's
# crossed Mac, Mac mini, iPhone and Fire packages). The page now asks for one save
# after another, and this lock makes sure of it here too.
_SAVE_LOCK = threading.Lock()


def save_bytes(win, name, mime, data_b64):
    """One save at a time: see _SAVE_LOCK."""
    with _SAVE_LOCK:
        return _save_bytes_one(win, name, mime, data_b64)


def _save_bytes_one(win, name, mime, data_b64):
    """Write an exported file from Python.

    A synthetic <a download> click inside a native web view is handed to the
    platform's download machinery; writing the bytes here avoids that entirely.
    Returns the path written, or False so the page can fall back to the anchor.
    """
    import base64, os
    try:
        raw = base64.b64decode(data_b64 or "")
    except Exception:
        log_problem("save_bytes: undecodable payload")
        return False
    keep = " ._-()[]"
    safe = "".join(c for c in str(name or "download") if c.isalnum() or c in keep).strip() or "download"
    target = None
    shown = False
    try:
        import webview
        # THE EXPORT CRASH (mac/windows): a blob download handed to the webview
        # can bring the window down on builds without download support.
        # Recent pywebview honours this switch; older ones ignore the unknown key.
        try:
            webview.settings['ALLOW_DOWNLOADS'] = True
        except Exception:
            pass
        w = win
        if w is None:
            ws = [x for x in (getattr(webview, "windows", None) or []) if x is not None]
            w = ws[0] if ws else None
        if w is not None:
            shown = True
            res = w.create_file_dialog(webview.SAVE_DIALOG, save_filename=safe)
            if res:
                target = res if isinstance(res, str) else res[0]
    except Exception:
        # a missing dialog is not a cancellation: fall through to the folder
        log_problem("save_bytes: file dialog unavailable")
        shown = False
        target = None
    if shown and not target:
        # the person chose Cancel, so nothing is written and nothing falls back
        return "cancelled"
    if not target:
        try:
            d = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.isdir(d):
                d = os.path.expanduser("~")
            target = os.path.join(d, safe)
        except Exception:
            log_problem("save_bytes: no writable folder")
            return False
    try:
        with open(target, "wb") as fh:
            fh.write(raw)
        return target
    except Exception:
        log_problem("save_bytes: write failed")
        return False


def _read_device_info():
    """Real hardware facts the browser cannot see (Safari hides device memory).
    RAM in GB, logical cores, platform and architecture. Best effort."""
    import platform as _pl
    info = {"platform": sys.platform, "arch": _pl.machine() or "",
            "cores": os.cpu_count() or 0, "ramGB": None}
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            pages = os.sysconf("SC_PHYS_PAGES")
            psize = os.sysconf("SC_PAGE_SIZE")
            info["ramGB"] = round(pages * psize / (1024.0 ** 3), 1)
        elif sys.platform.startswith("win"):
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            info["ramGB"] = round(ms.ullTotalPhys / (1024.0 ** 3), 1)
    except Exception:
        pass
    return info


class _IngSafeRedirect(urllib.request.HTTPRedirectHandler):
    """6.7 (app_relay_67.py): a redirect to another host, or from https to http, keeps no header that carries a key."""
    _SECRET = ("authorization", "proxy-authorization", "cookie", "x-api-key", "api-key", "x-goog-api-key",
               "x-subscription-token", "anthropic-api-key", "openai-organization")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        try:
            a, b = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
            moved = (a.hostname or "").lower() != (b.hostname or "").lower() or (a.port != b.port)
            downgraded = a.scheme.lower() == "https" and b.scheme.lower() != "https"
            if moved or downgraded:
                for k in list(new.headers.keys()):
                    lk = k.lower()
                    if lk in self._SECRET or "token" in lk or "secret" in lk or "password" in lk or lk.endswith("key"):
                        del new.headers[k]
        except Exception:
            return None
        return new


def _ing_urlopen(req, timeout, ctx):
    """urlopen with the safe redirect (6.7, app_relay_67.py)."""
    handlers = [_IngSafeRedirect()]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def _relay_urllib(url, method, headers, body):
    """The ordinary path: urllib. Every module it needs is imported at the top
    of this file so the frozen app always has it. HTTPS answers are verified
    against the bundled certifi roots (SSL_CERT_FILE was set up top)."""
    req = urllib.request.Request(url, data=body, method=method)
    hs = headers or {}
    if isinstance(hs, dict):
        for k, v in hs.items():
            k = str(k)
            if k.lower() in ("host", "content-length", "connection", "transfer-encoding"):
                continue
            req.add_header(k, str(v))
    if body is not None and not any(str(k).lower() == "content-type" for k in (hs or {})):
        req.add_header("Content-Type", "application/json")
    ctx = None
    try:
        ctx = ssl.create_default_context(cafile=_CERT_FILE) if _CERT_FILE else ssl.create_default_context()
    except Exception:
        ctx = None
    with _ing_urlopen(req, 300, ctx if url.startswith("https://") else None) as resp:
        raw = resp.read()
        try:
            text = raw.decode("utf-8")
        except Exception:
            text = base64.b64encode(raw).decode("ascii")
        return {"status": int(resp.status), "body": text}


def _relay_nsurlsession(url, method, headers, body):
    """The Mac-native path: NSURLSession, which needs no Python ssl/urllib
    machinery at all. This is the fallback when the frozen interpreter's own
    request chain fails (the old base_library.zip class of breakage), so the
    installed app's connections keep working regardless. Foundation travels
    inside the bundle already, because pywebview needs it to draw the window."""
    import Foundation
    done = threading.Event()
    out = {}
    u = Foundation.NSURL.URLWithString_(str(url))
    if u is None:
        raise RuntimeError("NSURL rejected the address")
    req = Foundation.NSMutableURLRequest.alloc().initWithURL_(u)
    req.setHTTPMethod_(str(method))
    hs = headers if isinstance(headers, dict) else {}
    for k, v in hs.items():
        if str(k).lower() in ("host", "content-length", "connection", "transfer-encoding"):
            continue
        req.setValue_forHTTPHeaderField_(str(v), str(k))
    if body is not None:
        if not any(str(k).lower() == "content-type" for k in hs):
            req.setValue_forHTTPHeaderField_("application/json", "Content-Type")
        req.setHTTPBody_(Foundation.NSData.dataWithBytes_length_(body, len(body)))

    def _done(data, resp, err):
        try:
            if err is not None:
                out["error"] = str(err.localizedDescription())
            else:
                code = int(resp.statusCode()) if (resp is not None and hasattr(resp, "statusCode")) else 0
                out["status"] = code
                out["body"] = bytes(data).decode("utf-8", "replace") if data is not None else ""
        except Exception as ce:
            out["error"] = "%s: %s" % (type(ce).__name__, ce)
        done.set()

    task = Foundation.NSURLSession.sharedSession().dataTaskWithRequest_completionHandler_(req, _done)
    task.resume()
    if not done.wait(315):
        try:
            task.cancel()
        except Exception:
            pass
        return {"error": "The request timed out in the native network layer."}
    return out


# ---- R67: the streaming relay, on the app's own address ----
# The page's way to the internet (a connection such as Claude or Ollama, web
# search, research, GitHub): POST /_relay on the app's own loopback address
# carries one request and streams the answer back as it arrives, so a reply
# from Claude or Ollama appears word by word. Only this app's page may use
# it: each request must carry this session's private key, and none may come
# from another website. Nothing is fetched unless the page asks.
_RELAY_SECRET = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
_RELAY_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
_RELAY_SKIP = ("host", "content-length", "connection", "transfer-encoding", "accept-encoding",
               "origin", "referer", "cookie", "x-ingenium-key")


def _relay_open(url, method, headers, data, timeout):
    """Open one upstream request. Returns (status, response); an HTTP error is
    still an answer (a 401 says the key is wrong, a 404 the model name)."""
    req = urllib.request.Request(url, data=data, method=method)
    hs = headers if isinstance(headers, dict) else {}
    for k, v in hs.items():
        if str(k).lower() in _RELAY_SKIP:
            continue
        req.add_header(str(k), str(v))
    lower = [str(k).lower() for k in hs]
    if data is not None and "content-type" not in lower:
        req.add_header("Content-Type", "application/json")
    if "user-agent" not in lower:
        req.add_header("User-Agent", _RELAY_UA)
    ctx = None
    if url.lower().startswith("https://"):
        ctx = ssl.create_default_context(cafile=_CERT_FILE) if _CERT_FILE else ssl.create_default_context()
    try:
        resp = _ing_urlopen(req, timeout, ctx)
        return int(resp.status), resp
    except urllib.error.HTTPError as he:
        return int(he.code), he


def _relay_reason(e):
    """Why a request could not be made, in plain words."""
    r = getattr(e, "reason", None)
    text = str(r if r is not None else e)
    low = text.lower()
    if "refused" in low:
        return "Nothing answered at that address (is the program there running?)."
    if "timed out" in low or isinstance(r, TimeoutError) or isinstance(e, TimeoutError):
        return "That address did not answer in time."
    if "certificate" in low or "ssl" in low:
        return "That site's security certificate could not be checked (%s)." % text
    if "getaddrinfo" in low or "name or service" in low or "nodename" in low or "11001" in low:
        return "That name could not be found. Is this computer online?"
    if "no route to host" in low or "host is down" in low or "network is unreachable" in low or "errno 65" in low or "errno 51" in low:
        return "LOCALNET: this computer was not allowed to reach that address on the home network (%s)." % text
    return text


def _relay_refuse(h, code, text, relay_error=False):
    data = text.encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "text/plain; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    if relay_error:
        h.send_header("X-Relay-Error", "1")
    h.end_headers()
    h.wfile.write(data)


def _relay_stream(h):
    try:
        if h.headers.get("X-Ingenium-Key") != _RELAY_SECRET:
            return _relay_refuse(h, 403, "This address answers only this app's own page.", True)
        port = h.server.server_address[1]
        origin = h.headers.get("Origin")
        if origin and origin not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
            return _relay_refuse(h, 403, "This address answers only this app's own page.", True)
        n = int(h.headers.get("Content-Length") or 0)
        if n > 64 * 1024 * 1024:
            return _relay_refuse(h, 413, "That request is too large to carry.", True)
        req = json.loads(h.rfile.read(n).decode("utf-8") or "{}")
        url = str(req.get("url") or "").strip()
        if not re.match(r"(?i)^https?://[^/\s]+", url):
            return _relay_refuse(h, 400, "Only web addresses (http and https) can be fetched.", True)
        method = str(req.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            return _relay_refuse(h, 400, "That kind of request is not carried.", True)
        body = req.get("body")
        data = body.encode("utf-8") if isinstance(body, str) and method not in ("GET", "HEAD") else None
        try:
            timeout = max(5, min(int(req.get("timeout") or 120), 900))
        except Exception:
            timeout = 120
        try:
            status, resp = _relay_open(url, method, req.get("headers") or {}, data, timeout)
        except Exception as e:
            return _relay_refuse(h, 502, _relay_reason(e), True)
        h.send_response(200)
        h.send_header("X-Upstream-Status", str(status))
        ct = resp.headers.get("Content-Type") if getattr(resp, "headers", None) else None
        h.send_header("Content-Type", ct or "application/octet-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "close")
        h.end_headers()
        read = getattr(resp, "read1", None) or resp.read
        try:
            while True:
                chunk = read(16384)
                if not chunk:
                    break
                h.wfile.write(chunk)
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # the page stopped listening (Stop): the upstream request ends with it
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    except Exception:
        log_problem("relay failed", sys.exc_info())


# ---- R66: a screenshot, with this computer's own picker ----
_WIN_SNIP_PS = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace IngSnip -Name U -MemberDefinition '[DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();'
$out = '__OUT__'
$seq = [IngSnip.U]::GetClipboardSequenceNumber()
$started = $true
try { Start-Process 'ms-screenclip:' } catch { $started = $false }
if ($started) {
  $names = 'ScreenClippingHost','SnippingTool','ScreenSketch'
  $seen = $false; $goneSince = $null
  $end = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $end) {
    Start-Sleep -Milliseconds 250
    if ([IngSnip.U]::GetClipboardSequenceNumber() -ne $seq) {
      Start-Sleep -Milliseconds 300
      $img = [System.Windows.Forms.Clipboard]::GetImage()
      if ($img -ne $null) { $img.Save($out, [System.Drawing.Imaging.ImageFormat]::Png); exit 0 }
      $seq = [IngSnip.U]::GetClipboardSequenceNumber()
    }
    $running = Get-Process -Name $names -ErrorAction SilentlyContinue
    if ($running) { $seen = $true; $goneSince = $null }
    elseif ($seen) {
      if ($goneSince -eq $null) { $goneSince = Get-Date }
      elseif (((Get-Date) - $goneSince).TotalSeconds -gt 2) { exit 2 }
    }
  }
  exit 2
}
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
exit 0
"""


def _take_screenshot():
    """Let the person capture part of the screen with the computer's own tool:
    on a Mac the crosshair of screencapture (drag an area, or press Space for
    a window), on Windows the Snipping Tool's area picker. Returns the picture
    as base64 PNG, or the reason there is none."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png", prefix="ingenium-shot-")
    os.close(fd)
    os.remove(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["screencapture", "-i", "-x", path], timeout=300)
        elif sys.platform.startswith("win"):
            subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command",
                            _WIN_SNIP_PS.replace("__OUT__", path.replace("'", "''"))],
                           timeout=150, creationflags=0x08000000)
        else:
            for cmd in (["gnome-screenshot", "-a", "-f", path], ["spectacle", "-r", "-b", "-n", "-o", path],
                        ["scrot", "-s", path]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, timeout=300)
                    break
            else:
                return {"ok": False, "error": "This computer has no screenshot tool the app knows."}
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return {"ok": False, "error": "No screenshot was taken (it was cancelled)."}
        with open(path, "rb") as fh:
            data = fh.read()
        return {"ok": True, "b64": base64.b64encode(data).decode("ascii")}
    except Exception as e:
        log_problem("screenshot failed", sys.exc_info())
        return {"ok": False, "error": "The screenshot could not be taken: %s" % e}
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _clipboard_read():
    """R78: the text on the computer's clipboard, for the page's own Paste.
    A web view will not hand the clipboard to the page unless the keyboard is
    used, so the edit panel asks the app instead: AppKit's general pasteboard
    on a Mac (pbpaste if AppKit is missing), the Windows clipboard otherwise."""
    try:
        if sys.platform == "darwin":
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString
                t = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
                return {"ok": True, "text": str(t) if t is not None else ""}
            except ImportError:
                env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
                r = subprocess.run(["/usr/bin/pbpaste", "-Prefer", "txt"], capture_output=True, timeout=5, env=env)
                return {"ok": True, "text": r.stdout.decode("utf-8", "replace")}
        if sys.platform.startswith("win"):
            return {"ok": True, "text": _win_clipboard_text()}
        for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, timeout=5)
                return {"ok": True, "text": r.stdout.decode("utf-8", "replace")}
        return {"ok": False, "error": "no clipboard tool on this computer"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _clipboard_write(text):
    """R78: puts text on the computer's clipboard (the panel's Copy and Cut)."""
    text = "" if text is None else str(text)
    try:
        if sys.platform == "darwin":
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                return {"ok": bool(pb.setString_forType_(text, NSPasteboardTypeString))}
            except ImportError:
                env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
                subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"), timeout=5, env=env, check=True)
                return {"ok": True}
        if sys.platform.startswith("win"):
            _win_clipboard_set(text)
            return {"ok": True}
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode("utf-8"), timeout=5, check=True)
                return {"ok": True}
        return {"ok": False, "error": "no clipboard tool on this computer"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _win_clipboard_api():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.OpenClipboard.restype = wintypes.BOOL
    u.CloseClipboard.restype = wintypes.BOOL
    u.EmptyClipboard.restype = wintypes.BOOL
    u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u.IsClipboardFormatAvailable.restype = wintypes.BOOL
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = wintypes.HANDLE
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = wintypes.LPVOID
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k.GlobalUnlock.restype = wintypes.BOOL
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = wintypes.HGLOBAL
    k.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k.GlobalFree.restype = wintypes.HGLOBAL
    return ctypes, u, k


def _win_clipboard_open(u):
    # another program may hold the clipboard for a moment; try for about a second
    for _ in range(20):
        if u.OpenClipboard(None):
            return True
        time.sleep(0.05)
    raise OSError("the clipboard is busy in another program")


def _win_clipboard_text():
    ctypes, u, k = _win_clipboard_api()
    CF_UNICODETEXT = 13
    _win_clipboard_open(u)
    try:
        if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = k.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _win_clipboard_set(text):
    ctypes, u, k = _win_clipboard_api()
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    _win_clipboard_open(u)
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            raise OSError("no memory for the clipboard")
        ptr = k.GlobalLock(h)
        if not ptr:
            k.GlobalFree(h)
            raise OSError("the clipboard memory could not be locked")
        ctypes.memmove(ptr, data, size)
        k.GlobalUnlock(h)
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            k.GlobalFree(h)
            raise OSError("the clipboard refused the text")
    finally:
        u.CloseClipboard()


# ---------------------------------------------------------------- R78: training a model from zero
# A tiny Llama-shaped model with random weights and a vocabulary learned from
# the person's own text (byte-level BPE, split the way llama.cpp's "gpt-2"
# pre-tokenizer splits), trained on that text by llama.cpp's own trainer
# (llama-finetune, carried beside llama-server). The model learns the text's
# patterns; it is far weaker than a downloaded model, and it continues text
# rather than holding a conversation. Standard library only.
_GG = dict(U8=0, I8=1, U16=2, I16=3, U32=4, I32=5, F32=6, BOOL=7, STR=8, ARR=9, U64=10, I64=11, F64=12)
_GG_FMT = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
TRAIN_SIZES = {  # name: (layers, width, heads, feed-forward width)
    "tiny": (4, 128, 4, 352),
    "small": (6, 256, 8, 704),
    # 6.5 (app_train_65.py): for a strong computer and much more text (about 16 and 41 million numbers)
    "medium": (8, 384, 6, 1024),
    "large": (12, 512, 8, 1408),
}
TRAIN_VOCAB = 2048
# 6.5: the learning rate at the start and at the end, for a training from zero and for one that goes on (the trainer starts
# its optimiser afresh, so that one starts gently); measured for each size on 150,000 characters (source/rig/train_sizes.txt)
TRAIN_LR = {"tiny": (1e-4, 1e-5), "small": (6e-5, 6e-6), "medium": (4e-5, 4e-6), "large": (3e-5, 3e-6), "base": (1e-6, 1e-7)}
TRAIN_LR_MORE = {"tiny": (3e-5, 3e-6), "small": (2e-5, 2e-6), "medium": (1.5e-5, 1.5e-6), "large": (1e-5, 1e-6), "base": (5e-7, 5e-8)}
# the batch is the context: llama.cpp rounds a context up to 256 tokens, and trains no K or V projection when a batch is smaller
TRAIN_CTX = 256
_TRAINER_EXE = "llama-finetune.exe" if sys.platform.startswith("win") else "llama-finetune"


def _gg_str(b, v):
    v = v.encode("utf-8") if isinstance(v, str) else v
    b += struct.pack("<Q", len(v)) + v


def _gg_val(b, t, v):
    if t == _GG["STR"]:
        _gg_str(b, v)
    elif t == _GG["ARR"]:
        et, items = v
        b += struct.pack("<IQ", et, len(items))
        if et == _GG["STR"]:
            for x in items:
                _gg_str(b, x)
        elif items:
            b += struct.pack("<%d%s" % (len(items), _GG_FMT[et][1]), *items)
    else:
        b += struct.pack(_GG_FMT[t], v)


def _bytes_to_unicode():
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("\xa1"), ord("\xac") + 1)) + list(range(ord("\xae"), ord("\xff") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {b: chr(c) for b, c in zip(bs, cs)}


# llama.cpp's "gpt-2" pre-tokenizer, in Python's terms (letters: [^\W\d_])
_PRE_SPLIT = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?(?:[^\s\w]|_)+|\s+(?!\S)|\s+")


def _train_bpe(text, vocab_size=TRAIN_VOCAB, progress=None):
    """A byte-level BPE vocabulary learned from the text, as GGUF tokenizer metadata."""
    b2u = _bytes_to_unicode()
    words = Counter(_PRE_SPLIT.findall(text))
    wl = [([b2u[b] for b in w.encode("utf-8")], f) for w, f in words.items()]
    pc = Counter()
    idx = defaultdict(set)
    for wi, (sym, f) in enumerate(wl):
        for a, b in zip(sym, sym[1:]):
            pc[(a, b)] += f
            idx[(a, b)].add(wi)
    merges = []
    want = vocab_size - 256 - 2
    for step in range(want):
        if not pc:
            break
        best, cnt = max(pc.items(), key=lambda kv: kv[1])
        if cnt < 2:
            break
        merges.append(best)
        new = best[0] + best[1]
        for wi in list(idx.get(best, ())):
            sym, f = wl[wi]
            for a, b in zip(sym, sym[1:]):
                pc[(a, b)] -= f
                if pc[(a, b)] <= 0:
                    del pc[(a, b)]
                idx[(a, b)].discard(wi)
            out = []
            i = 0
            while i < len(sym):
                if i < len(sym) - 1 and sym[i] == best[0] and sym[i + 1] == best[1]:
                    out.append(new)
                    i += 2
                else:
                    out.append(sym[i])
                    i += 1
            wl[wi] = (out, f)
            for a, b in zip(out, out[1:]):
                pc[(a, b)] += f
                idx[(a, b)].add(wi)
        if progress and step % 64 == 0:
            progress(step / max(1, want))
    tokens = [b2u[b] for b in range(256)] + [a + b for a, b in merges] + ["<|bos|>", "<|eos|>"]
    types = [1] * (len(tokens) - 2) + [3, 3]
    return [("tokenizer.ggml.model", _GG["STR"], "gpt2"), ("tokenizer.ggml.pre", _GG["STR"], "gpt-2"),
            ("tokenizer.ggml.tokens", _GG["ARR"], (_GG["STR"], [t.encode("utf-8") for t in tokens])),
            ("tokenizer.ggml.token_type", _GG["ARR"], (_GG["I32"], types)),
            ("tokenizer.ggml.merges", _GG["ARR"], (_GG["STR"], [(a + " " + b).encode("utf-8") for a, b in merges])),
            ("tokenizer.ggml.bos_token_id", _GG["U32"], len(tokens) - 2), ("tokenizer.ggml.eos_token_id", _GG["U32"], len(tokens) - 1),
            ("tokenizer.ggml.add_bos_token", _GG["BOOL"], True)]


def _new_model(out_path, tok_kv, size="tiny", name="My first model", ctx=256, seed=None):
    """Writes a model with random weights (F32, as the trainer needs); returns its parameter count."""
    n_layer, n_embd, n_head, n_ff = TRAIN_SIZES[size]
    n_vocab = len([v for (k, t, v) in tok_kv if k == "tokenizer.ggml.tokens"][0][1])
    G = _GG
    kv = [("general.architecture", G["STR"], "llama"), ("general.name", G["STR"], name), ("general.file_type", G["U32"], 0),
          ("llama.context_length", G["U32"], ctx), ("llama.embedding_length", G["U32"], n_embd), ("llama.block_count", G["U32"], n_layer),
          ("llama.feed_forward_length", G["U32"], n_ff), ("llama.attention.head_count", G["U32"], n_head),
          ("llama.attention.head_count_kv", G["U32"], n_head), ("llama.attention.layer_norm_rms_epsilon", G["F32"], 1e-5),
          ("llama.rope.freq_base", G["F32"], 10000.0), ("llama.rope.dimension_count", G["U32"], n_embd // n_head),
          ("llama.vocab_size", G["U32"], n_vocab)] + list(tok_kv)
    deep = 0.02 / math.sqrt(2 * n_layer)
    tensors = [("token_embd.weight", (n_embd, n_vocab), 0.02)]
    for i in range(n_layer):
        q = "blk.%d." % i
        tensors += [(q + "attn_norm.weight", (n_embd,), None), (q + "attn_q.weight", (n_embd, n_embd), 0.02),
                    (q + "attn_k.weight", (n_embd, n_embd), 0.02), (q + "attn_v.weight", (n_embd, n_embd), 0.02),
                    (q + "attn_output.weight", (n_embd, n_embd), deep), (q + "ffn_norm.weight", (n_embd,), None),
                    (q + "ffn_gate.weight", (n_embd, n_ff), 0.02), (q + "ffn_up.weight", (n_embd, n_ff), 0.02),
                    (q + "ffn_down.weight", (n_ff, n_embd), deep)]
    tensors += [("output_norm.weight", (n_embd,), None), ("output.weight", (n_embd, n_vocab), 0.02)]
    head = bytearray(b"GGUF") + struct.pack("<IQQ", 3, len(tensors), len(kv))
    for k, t, v in kv:
        _gg_str(head, k)
        head += struct.pack("<I", t)
        _gg_val(head, t, v)
    offs = []
    off = 0
    for nm, shape, _ in tensors:
        n = 1
        for d in shape:
            n *= d
        offs.append(off)
        off += (n * 4 + 31) // 32 * 32
    for (nm, shape, _), o in zip(tensors, offs):
        _gg_str(head, nm)
        head += struct.pack("<I", len(shape))
        for d in shape:
            head += struct.pack("<Q", d)
        head += struct.pack("<IQ", 0, o)
    rnd = random.Random(seed)
    total = 0
    with open(out_path, "wb") as f:
        f.write(head)
        f.write(b"\0" * ((32 - len(head) % 32) % 32))
        for nm, shape, std in tensors:
            n = 1
            for d in shape:
                n *= d
            total += n
            done = 0
            while done < n:
                k = min(1 << 16, n - done)
                vals = [1.0] * k if std is None else [rnd.gauss(0.0, std) for _ in range(k)]
                f.write(struct.pack("<%df" % k, *vals))
                done += k
            f.write(b"\0" * ((32 - (n * 4) % 32) % 32))
    return total


# ---------------------------------------------------------------- 6.6: a model you have, trained further (app_studio_65.py)
_GGUF_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_WIDENS = {0: "F32", 1: "F16", 8: "Q8_0", 30: "BF16"}   # the forms widened exactly to 32 bits


def _gguf_skip(b, p, t):
    if t in _GGUF_FIXED:
        return p + _GGUF_FIXED[t]
    if t == 8:
        return p + 8 + struct.unpack_from("<Q", b, p)[0]
    if t == 9:
        et, n = struct.unpack_from("<IQ", b, p)
        p += 12
        if et in _GGUF_FIXED:
            return p + _GGUF_FIXED[et] * n
        for _ in range(n):
            p = _gguf_skip(b, p, et)
        return p
    raise ValueError("a setting of a kind this does not know (%d)" % t)


def _gguf_read(path):
    """A GGUF file's settings (raw, in order), its tensors (name, shape, form, place) and where their numbers begin."""
    with open(path, "rb") as f:
        head = f.read(64 << 20)
    if head[:4] != b"GGUF":
        raise ValueError("not a GGUF model file")
    ver, n_t, n_kv = struct.unpack_from("<IQQ", head, 4)
    p, kvs, sets, align = 24, [], {}, 32
    for _ in range(n_kv):
        k0 = p
        n = struct.unpack_from("<Q", head, p)[0]
        key = head[p + 8:p + 8 + n].decode("utf-8", "replace")
        p += 8 + n
        t = struct.unpack_from("<I", head, p)[0]
        v0 = p + 4
        p = _gguf_skip(head, v0, t)
        kvs.append((key, t, head[k0:v0], head[v0:p]))
        if t == 8:
            sets[key] = head[v0 + 8:p].decode("utf-8", "replace")
        elif t in (4, 5, 10):
            sets[key] = struct.unpack_from({4: "<I", 5: "<i", 10: "<Q"}[t], head, v0)[0]
        if key == "general.alignment" and t == 4:
            align = sets[key]
    tensors = []
    for _ in range(n_t):
        n = struct.unpack_from("<Q", head, p)[0]
        name = head[p + 8:p + 8 + n]
        p += 8 + n
        nd = struct.unpack_from("<I", head, p)[0]
        dims = struct.unpack_from("<%dQ" % nd, head, p + 4)
        p += 4 + 8 * nd
        typ, off = struct.unpack_from("<IQ", head, p)
        p += 12
        count = 1
        for d in dims:
            count *= d
        tensors.append((name, dims, typ, off, count))
    return {"version": ver, "kvs": kvs, "settings": sets, "tensors": tensors, "align": align,
            "data": (p + align - 1) // align * align}


def _gguf_summary(path):
    """What a model is, and whether llama.cpp's trainer can train it here: a Llama-shaped model in 32, 16 or 8 bits."""
    try:
        g = _gguf_read(path)
    except Exception as e:
        return {"trainable": False, "why": "Its file could not be read: %s" % e}
    sets = g["settings"]
    arch = sets.get("general.architecture", "")
    params = sum(t[4] for t in g["tensors"])
    forms = sorted({_GGUF_WIDENS.get(t[2], "type %d" % t[2]) for t in g["tensors"]})
    emb = [t for t in g["tensors"] if t[0] == b"token_embd.weight"]
    out = {"arch": arch, "name": sets.get("general.name", ""), "params": params, "forms": forms, "trainable": True, "why": "",
           "vocab": int(emb[0][1][1]) if emb and len(emb[0][1]) > 1 else None}
    if arch != "llama":
        out.update(trainable=False, why="llama.cpp's trainer trains Llama-shaped models; this one is %s." % (arch or "of another shape"))
    elif any(t[2] not in _GGUF_WIDENS for t in g["tensors"]):
        out.update(trainable=False, why="Its numbers are packed smaller than 8 bits, which cannot be widened without loss: choose its 16-bit (F16) or 8-bit (Q8_0) file.")
    return out


def _gguf_to_f32(src, dst, progress=None):
    """The same model with every tensor in 32-bit numbers: exact from F16 and BF16, and from Q8_0 its own values."""
    g = _gguf_read(src)
    align = g["align"]
    out = bytearray(b"GGUF") + struct.pack("<IQQ", g["version"], len(g["tensors"]), len(g["kvs"]))
    for key, t, kraw, vraw in g["kvs"]:
        out += kraw + (struct.pack("<I", 0) if key == "general.file_type" and t == 4 else vraw)
    noff, places = 0, []
    for name, dims, typ, off, count in g["tensors"]:
        out += struct.pack("<Q", len(name)) + name + struct.pack("<I", len(dims)) + struct.pack("<%dQ" % len(dims), *dims)
        out += struct.pack("<IQ", 0, noff)
        places.append(noff)
        noff += (count * 4 + align - 1) // align * align
    out += b"\0" * ((align - len(out) % align) % align)
    total = sum(t[4] for t in g["tensors"]) or 1
    done = 0
    with open(src, "rb") as f, open(dst, "wb") as w:
        w.write(out)
        for name, dims, typ, off, count in g["tensors"]:
            f.seek(g["data"] + off)
            left = count
            while left:
                if typ == 8:   # Q8_0: blocks of 32 numbers, a 16-bit scale and 32 signed bytes
                    k = min(left, 1 << 15)
                    blk = f.read(34 * (k // 32))
                    vals = []
                    for i in range(0, len(blk), 34):
                        d = struct.unpack_from("<e", blk, i)[0]
                        vals.extend([d * q for q in struct.unpack_from("<32b", blk, i + 2)])
                    chunk = struct.pack("<%df" % k, *vals)
                else:
                    k = min(left, 1 << 20)
                    if typ == 0:
                        chunk = f.read(4 * k)
                    elif typ == 1:
                        chunk = struct.pack("<%df" % k, *struct.unpack("<%de" % k, f.read(2 * k)))
                    else:   # BF16: the upper half of a 32-bit number
                        h = f.read(2 * k)
                        chunk = bytearray(4 * k)
                        chunk[2::4] = h[0::2]
                        chunk[3::4] = h[1::2]
                w.write(chunk)
                left -= k
                done += k
                if progress:
                    progress(done / total)
            w.write(b"\0" * ((count * 4 + align - 1) // align * align - count * 4))
    return total


def _train_need_gb(params):
    """The memory a training takes: the weights, their gradients and the optimiser's two moments, 4 bytes each, and room."""
    return round(params * 16 / 1e9 + 0.6, 1)


def _train_bases():
    """The models on this computer that can be trained further, with what each needs."""
    ram = None
    try:
        ram = _read_device_info().get("ramGB")
    except Exception:
        pass
    out = []
    for mid, path in sorted(_MODEL_PATHS.items()):
        if not os.path.isfile(path):
            continue
        info = _gguf_summary(path)
        meta = _MODEL_META.get(mid) or {}
        need = _train_need_gb(info.get("params") or 0)
        out.append({"id": mid, "name": meta.get("name") or info.get("name") or os.path.basename(path), "params": info.get("params"),
                    "forms": info.get("forms"), "trainable": bool(info.get("trainable")), "why": info.get("why", ""),
                    "needGB": need, "ramGB": ram, "fits": ram is None or need <= ram * 0.7, "trained": bool(meta.get("trained"))})
    return {"ok": True, "models": out, "ramGB": ram}


_TRAIN = {"state": "idle"}
_TRAIN_LOCK = threading.Lock()
_TRAIN_PROC = [None]
# the trainer's progress line: "train: [====] data=0000648/0000648 loss=7.50777±0.02780 acc=5.06±0.08% t=00:00:16 ETA=00:00:00"
_TRAIN_LINE = re.compile(r"^(train|val):.*?data=(\d+)/(\d+)\s+loss=([0-9.]+).*?acc=([0-9.]+).*?ETA=(\d+):(\d+):(\d+)")


def _trainer_path():
    d = _engine_dir()
    if d:
        p = os.path.join(d, _TRAINER_EXE)
        if os.path.isfile(p):
            return p
    return None


def _train_available():
    return {"ok": True, "trainer": bool(_trainer_path()), "cores": os.cpu_count() or 1}


def _train_set(**kw):
    with _TRAIN_LOCK:
        _TRAIN.update(kw)


def _train_status():
    with _TRAIN_LOCK:
        st = json.loads(json.dumps(_TRAIN))
    if st.get("state") == "running" and st.get("started"):
        st["elapsed"] = round(time.time() - st["started"])
    return st


def _train_cancel():
    _train_set(cancel=True)
    p = _TRAIN_PROC[0]
    if p is not None and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    return True


def _train_stop_at_exit():
    # the trainer is a separate program: it would go on running after the
    # app closes, so it is stopped with the app
    if _TRAIN.get("state") == "running":
        _train_cancel()


def _train_start(spec):
    spec = spec or {}
    with _TRAIN_LOCK:
        if _TRAIN.get("state") == "running":
            return {"ok": False, "error": "A training is already running."}
    exe = _trainer_path()
    if not exe:
        return {"ok": False, "error": "This copy of the app does not carry llama.cpp's trainer (llama-finetune). The installer from GitHub carries it."}
    text = str(spec.get("text") or "")
    if len(text) < 2000:
        return {"ok": False, "error": "Training needs at least about 2,000 characters of text; more is better (100,000 or more)."}
    text = text[:8 * 1024 * 1024]
    size = spec.get("size") if spec.get("size") in TRAIN_SIZES else "tiny"
    passes = max(1, min(60, int(spec.get("passes") or 8)))
    name = re.sub(r"[\r\n]+", " ", str(spec.get("name") or "My first model"))[:60]
    more = str(spec.get("from_model") or "")
    base = "" if more else str(spec.get("base") or "")
    if base:
        if not os.path.isfile(_MODEL_PATHS.get(base, "")):
            return {"ok": False, "error": "The model to start from is not on this computer."}
        info = _gguf_summary(_MODEL_PATHS[base])
        if not info.get("trainable"):
            return {"ok": False, "error": info.get("why") or "That model cannot be trained here."}
        passes = max(1, min(5, int(spec.get("passes") or 1)))
    if more and not os.path.isfile(_MODEL_PATHS.get(more, "")):
        return {"ok": False, "error": "The model to train further is not on this computer."}
    md = _models_dir()
    if not md:
        return {"ok": False, "error": "The app's data folder is not writable."}
    tid = time.strftime("%Y%m%d-%H%M%S")
    # a training cut short when the app closed leaves its folder; nothing
    # else is running now, so those are cleared
    if (md / "training").is_dir():
        for old in (md / "training").iterdir():
            shutil.rmtree(str(old), ignore_errors=True)
    work = md / "training" / tid
    work.mkdir(parents=True, exist_ok=True)
    (work / "text.txt").write_text(text, encoding="utf-8")
    with _TRAIN_LOCK:
        _TRAIN.clear()
        _TRAIN.update({"state": "running", "phase": "preparing", "id": tid, "name": name, "size": size, "passes": passes,
                       "pass": 0, "frac": 0.0, "loss": None, "val": None, "acc": None, "history": [], "eta": None,
                       "started": time.time(), "chars": len(text), "more": bool(more), "error": "", "cancel": False})
    threading.Thread(target=_train_run, args=(exe, work, size, passes, name, more, base), daemon=True).start()
    return {"ok": True, "id": tid}


def _train_run(exe, work, size, passes, name, more, base=""):
    try:
        start = work / "start.gguf"
        out = work / "trained.gguf"
        if base:
            # 6.6: widened to 32-bit numbers, which the trainer needs; the model it came from stays as it was
            _train_set(phase="widening", nvocab=_gguf_summary(_MODEL_PATHS[base]).get("vocab"))
            params = _gguf_to_f32(_MODEL_PATHS[base], str(start), progress=lambda f: _train_set(frac=0.0, widen=round(f, 2)))
            size = "base"
            lr0, lr1 = TRAIN_LR["base"]
        elif more:
            shutil.copyfile(_MODEL_PATHS[more], start)
            # the trainer starts its optimiser afresh, so it starts gently
            size = (_MODEL_META.get(more) or {}).get("size") or size
            lr0, lr1 = TRAIN_LR_MORE.get(size, TRAIN_LR_MORE["tiny"])
            params = (_MODEL_META.get(more) or {}).get("params")
        else:
            _train_set(phase="vocabulary")
            kv = _train_bpe((work / "text.txt").read_text(encoding="utf-8"), TRAIN_VOCAB, progress=lambda f: _train_set(frac=0.0, vocab=round(f, 2)))
            _train_set(phase="model")
            params = _new_model(str(start), kv, size, name)
            # measured on 60,000 characters of a novel: from 5e-4 the loss
            # jumped back up in the second pass; from 1e-4 (tiny) and 6e-5
            # (small) it fell in every pass and the held-back loss ended lowest
            lr0, lr1 = TRAIN_LR.get(size, TRAIN_LR["tiny"])
        _train_set(phase="training", params=params)
        threads = max(1, (os.cpu_count() or 2) - 1)
        args = [exe, "-m", str(start), "--file", str(work / "text.txt"), "-c", str(TRAIN_CTX), "-b", str(TRAIN_CTX), "-ub", str(TRAIN_CTX),
                "-epochs", str(passes), "-lr", str(lr0), "-lr-min", str(lr1), "-decay-epochs", str(passes),
                "-t", str(threads), "-o", str(out)]
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = 0x08000000  # no console window
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=str(work), **kw)
        _TRAIN_PROC[0] = proc
        tail = []
        buf = b""
        done_pass = 0
        last_train = None
        while True:
            ch = proc.stderr.read(512)
            if not ch:
                break
            buf += ch
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail = (tail + [line])[-12:]
                m = _TRAIN_LINE.match(line)
                if not m:
                    continue
                kind, k, n = m.group(1), int(m.group(2)), max(1, int(m.group(3)))
                loss, acc = float(m.group(4)), float(m.group(5))
                eta = int(m.group(6)) * 3600 + int(m.group(7)) * 60 + int(m.group(8))
                if kind == "train":
                    last_train = loss
                    with _TRAIN_LOCK:
                        _TRAIN.update(loss=loss, acc=acc, frac=min(1.0, (done_pass + k / n) / passes))
                        el = time.time() - _TRAIN["started"]
                        f = _TRAIN["frac"]
                        _TRAIN["eta"] = round(el * (1 - f) / f) if f > 0.02 else None
                elif k >= n:
                    done_pass += 1
                    with _TRAIN_LOCK:
                        _TRAIN["history"].append({"pass": done_pass, "loss": last_train, "val": loss})
                        _TRAIN.update(val=loss, **{"pass": done_pass})
        rc = proc.wait()
        _TRAIN_PROC[0] = None
        if _TRAIN.get("cancel"):
            _train_set(state="cancelled", phase="")
            shutil.rmtree(work, ignore_errors=True)
            return
        if rc != 0 or not out.is_file():
            _train_set(state="error", error="The trainer stopped (code %s): %s" % (rc, " / ".join(tail[-3:])))
            return
        md = _models_dir()
        mid = more or ("trained-" + work.name)
        dest = md / (mid + ".gguf")
        # the engine may still hold the older file open (Windows will not
        # replace an open file), so it lets go first
        if _ENGINE.get("model_id") == mid:
            _engine_stop()
        shutil.move(str(out), str(dest))
        _MODEL_PATHS[mid] = str(dest)
        prev = _MODEL_META.get(mid) or {}
        _MODEL_META[mid] = {"trained": True, "name": name, "size": size, "params": params,
                            "passes": int(prev.get("passes") or 0) + passes, "chars": _TRAIN.get("chars")}
        if base or prev.get("base"):
            _MODEL_META[mid]["base"] = prev.get("base") or (_MODEL_META.get(base) or {}).get("name") or os.path.basename(_MODEL_PATHS.get(base, ""))
        _save_models_index()
        shutil.rmtree(work, ignore_errors=True)
        _train_set(state="done", phase="", frac=1.0, eta=0,
                   model={"id": mid, "fileName": re.sub(r"[^A-Za-z0-9 ._-]", "", name).strip() + ".gguf", "size": os.path.getsize(str(dest)),
                          "passes": _MODEL_META[mid]["passes"], "params": params})
    except Exception as e:
        _TRAIN_PROC[0] = None
        _train_set(state="error", error=str(e))


def _relay(url, method="GET", headers=None, body=""):
    """Carry one web request from Python on the page's behalf, and hand the
    answer back over the JS bridge.

    Why this exists: a page inside a web view asking a local server (Ollama)
    or a cloud provider is routinely refused, by cross-origin rules in one web
    view and by CORS answers in another, and the refusal arrives as a silent
    'nothing happened'. Here the request leaves from Python instead, so the
    installed app's connections simply work, and any failure comes back with
    its real message. Only http addresses are relayed, at the address the
    person typed in Settings; the app stays offline until a connection is
    switched on there.

    R57: two legs. urllib first (its imports are now static, so the frozen app
    cannot lose them); if that leg itself breaks on a Mac, the request goes
    through NSURLSession, the same Foundation networking pywebview already
    carries. Verification stays on for https in both legs.
    """
    try:
        u = str(url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return {"error": "Only plain http or https addresses can be relayed."}
        data = None
        if body not in (None, ""):
            data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        m = str(method or "GET").upper()
        if m not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            return {"error": "That request kind is not relayed."}
        try:
            return _relay_urllib(u, m, headers, data)
        except urllib.error.HTTPError as he:
            # An HTTP error is still an answer: the status and body belong to
            # the page (a 401 says the key is wrong, a 404 the model name).
            try:
                text = he.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            return {"status": int(he.code), "body": text}
        except Exception as ue:
            if sys.platform != "darwin":
                return {"error": "%s: %s" % (type(ue).__name__, ue)}
            # The Mac's own network layer, immune to frozen-interpreter gaps.
            ns = _relay_nsurlsession(u, m, headers, data)
            if "status" in ns:
                return ns
            return {"error": "%s: %s (native fallback also failed: %s)" % (
                type(ue).__name__, ue, ns.get("error", "unknown"))}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


class _LiteWindows:
    """R123: an app (Tabula, Abacus, Imago, Scriba, Fontes) in a window of its own, at Ingenium's own address.

    Asked for by the page's export key (the floating window's arrow). The page hands its work across through
    the app, not through browser storage, since two windows of one app need not share it: open_lite keeps the
    parcel, the new window takes it (lite_take), and the main window lets the app go only once it is taken
    (lite_taken), or calls the move off (lite_cancel). If a run ended badly while such a window was open, the
    next run keeps every app in the main window (why: "safety"), as Carpe Diem does.
    """

    _LITE_APPS = {"tabula": "Tabula", "abacus": "Abacus", "slides": "Imago", "scriba": "Scriba", "fontes": "Fontes"}

    def open_lite(self, payload):
        try:
            p = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
        except Exception:
            return {"ok": False, "why": "payload"}
        if _LITE["blocked"]:
            return {"ok": False, "why": "safety"}
        which = str(p.get("which") or "")
        tok = str(p.get("tok") or "")
        url = str(p.get("url") or "")
        if (which not in self._LITE_APPS or not re.match(r"^[A-Za-z0-9_-]{1,40}$", tok)
                or not re.match(r"^http://127\.0\.0\.1:\d+/", url) or ("#ingapp=" + which) not in url):
            return {"ok": False, "why": "address"}
        with _LITE["lock"]:
            if _LITE["creating"]:
                return {"ok": False, "why": "busy"}
            _LITE["creating"] = True
        # 6.2: a window of its own reads the window page (ingenium_window.html, served beside the page by
        # _start_local_server): the same page without the apps Ingenium carries, the engine's WebAssembly and the kit
        # store, about a quarter of its size, so it opens about four times sooner
        url = re.sub(r"/ingenium\.html#", "/ingenium_window.html#", url, count=1)
        try:
            import webview
            _LITE["parcels"][tok] = json.dumps({"which": which, "handoff": p.get("handoff")})
            win = webview.create_window(
                self._LITE_APPS[which] + " \u00b7 Ingenium", url=url, width=1180, height=840,
                min_size=(MIN_WIDTH, MIN_HEIGHT), background_color=ACCENT_AMETHYST,
                confirm_close=False, text_select=True, js_api=self)
            _LITE["wins"][tok] = win
            try:
                win.events.closed += (lambda *a, t=tok: _lite_closed(t))
            except Exception:
                pass
            _lite_mark(True)
            return {"ok": True}
        except Exception:
            log_problem("open_lite(%r)" % (which,), sys.exc_info())
            _LITE["parcels"].pop(tok, None)
            return {"ok": False, "why": "window"}
        finally:
            with _LITE["lock"]:
                _LITE["creating"] = False

    def lite_take(self, tok):
        parcel = _LITE["parcels"].pop(str(tok), None)
        if parcel is not None:
            _LITE["taken"].add(str(tok))
            # 6.2: it comes forward, so the app never seems to vanish behind the main window
            _lite_forward(_LITE["wins"].get(str(tok)))
        return parcel

    # 6.2: the main window's Desk bar keeps a tab for each window of its own: the tab brings that window forward, and its
    # second key asks the window to send its app back into the main window's desk (lite_cmd "home", read by lite_cmds).
    def lite_focus(self, tok):
        w = _LITE["wins"].get(str(tok))
        if w is None:
            return False
        _lite_forward(w)
        return True

    def lite_cmd(self, tok, cmd):
        tok, cmd = str(tok), str(cmd)
        if cmd not in ("home",) or tok not in _LITE["wins"]:
            return False
        with _LITE["lock"]:
            q = _LITE["cmds"].setdefault(tok, [])
            if cmd not in q:
                q.append(cmd)
        return True

    def lite_cmds(self, tok):
        with _LITE["lock"]:
            q = _LITE["cmds"].pop(str(tok), [])
        return json.dumps(q)

    def main_focus(self):
        _lite_forward(getattr(self, "_main_window", None))
        return True

    def lite_taken(self, tok):
        return str(tok) in _LITE["taken"]

    def lite_cancel(self, tok):
        tok = str(tok)
        if tok in _LITE["taken"]:
            return False
        _LITE["parcels"].pop(tok, None)
        self.close_lite(tok)
        return True

    def close_lite(self, tok):
        w = _LITE["wins"].get(str(tok))
        if w is None:
            return False
        try:
            w.destroy()
        except Exception:
            log_problem("close_lite", sys.exc_info())
        return True

    # A window of its own never writes the desk. What it asks of the main window (to show a page, to take a thought
    # to the chat or to Steps) waits here until the main window takes it in.
    _LITE_POST_MAX = 20000
    _LITE_QUEUE_MAX = 200

    def lite_post(self, item):
        try:
            text = item if isinstance(item, str) else json.dumps(item)
            if len(text) > self._LITE_POST_MAX or not isinstance(json.loads(text), dict):
                return False
        except Exception:
            return False
        with _LITE["lock"]:
            if len(_LITE["queue"]) >= self._LITE_QUEUE_MAX:
                return False
            _LITE["queue"].append(text)
        return True

    def lite_inbox(self):
        with _LITE["lock"]:
            q, _LITE["queue"] = _LITE["queue"], []
        return json.dumps(q)

    # The parts of the desk every window shares (the notes, the reminders, the thought Inbox, day and night, the
    # Clepsydra's settings): each window puts what it changed, each takes in what another changed, and the main
    # window keeps it in the saved desk. Values stay as the page wrote them (JSON text); the app never reads them.
    _LITE_SYNC = ("notes", "reminders", "parked", "theme", "clep")
    _LITE_HUB_MAX = 4 * 1024 * 1024

    def lite_hub_put(self, payload):
        try:
            if not isinstance(payload, str) or len(payload) > self._LITE_HUB_MAX:
                return False
            p = json.loads(payload)
            keys, by, at = p.get("keys"), str(p.get("by") or "")[:40], p.get("at")
            if not isinstance(keys, dict) or not isinstance(at, (int, float)):
                return False
        except Exception:
            return False
        with _LITE["lock"]:
            for k, v in keys.items():
                if k in self._LITE_SYNC and isinstance(v, str):
                    _LITE["seq"] += 1
                    _LITE["hub"][k] = {"v": v, "by": by, "at": int(at), "seq": _LITE["seq"]}
        return True

    def lite_hub_get(self, since=0):
        try:
            since = int(since or 0)
        except Exception:
            since = 0
        with _LITE["lock"]:
            out = {k: e for k, e in _LITE["hub"].items() if e["seq"] > since}
            return json.dumps({"seq": _LITE["seq"], "keys": out})


_LITE = {"wins": {}, "parcels": {}, "taken": set(), "blocked": False, "creating": False, "lock": threading.RLock(),
         "queue": [], "hub": {}, "seq": 0, "cmds": {}}


def _lite_forward(win):
    """6.2: a window brought to the front (shown, restored if folded into the Dock or the taskbar)."""
    if win is None:
        return
    for name in ("restore", "show"):
        try:
            getattr(win, name)()
        except Exception:
            pass


def _lite_flag():
    f = _session_flag()
    return None if f is None else f.with_name("ingenium_lite_windows.flag")


def _lite_mark(on):
    """While any app has a window of its own, a mark beside the session flag says so."""
    f = _lite_flag()
    try:
        if f is None:
            return
        if on:
            f.write_text("1")
        elif f.exists():
            f.unlink()
    except Exception:
        pass


def _lite_closed(tok):
    _LITE["wins"].pop(tok, None)
    _LITE["taken"].discard(tok)
    with _LITE["lock"]:
        _LITE["cmds"].pop(tok, None)
        if len(_LITE["queue"]) < 200:
            _LITE["queue"].append(json.dumps({"kind": "gone", "tok": str(tok)}))
    if not _LITE["wins"]:
        _lite_mark(False)


def _lite_start(prior_bad):
    """At start: a bad ending while windows of their own were open keeps apps in the main window this run."""
    f = _lite_flag()
    try:
        was = bool(f is not None and f.exists())
    except Exception:
        was = False
    _LITE["blocked"] = bool(was and prior_bad >= 1)
    if _LITE["blocked"]:
        log_problem("start: the last run ended badly while an app had a window of its own; apps open in the main window this run")
    _lite_mark(False)


class IngeniumApi(_LiteWindows):
    """JS API exposed to the main window as window.pywebview.api.

    Ingenium page asks for two things: file saves (engine bakes, table
    exports) answered with the machine's own save dialog, and - only when
    INGENIUM_NATIVE_WINDOWS=1 is set - a separate native window for Tabula or
    Abacus. The default on every platform is a single window: a second web
    view in the same process is the most likely cause of instability on some
    machines, and the app draws its own floating windows in-page, so nothing
    is lost except the risk.
    """

    APP_TITLES = {"tabula": "Tabula", "abacus": "Abacus"}
    APP_COLORS = {"tabula": "#2A2622", "abacus": "#1E3D2C"}

    def __init__(self, app_url):
        import threading
        self._app_url = app_url
        self._main_window = None
        self._wins = {}
        # Creating two native windows at once is a real way to destabilise the
        # platform web view. One creation at a time, and never a duplicate.
        self._lock = threading.RLock()
        self._creating = set()

    def _alive(self, which):
        w = self._wins.get(which)
        if w is None:
            return False
        try:
            _ = w.title
            return True
        except Exception:
            self._wins.pop(which, None)
            return False

    def save_file(self, name, mime, data_b64):
        return save_bytes(None, name, mime, data_b64)

    def device_info(self):
        return _read_device_info()

    def relay(self, url, method="GET", headers=None, body=""):
        return _relay(url, method, headers, body)

    def relay_key(self):
        return _RELAY_SECRET

    def take_screenshot(self):
        return _take_screenshot()

    def read_clipboard(self):
        return _clipboard_read()

    # R78: training a small model from zero
    def train_available(self):
        return _train_available()

    def train_bases(self):
        return _train_bases()

    def train_start(self, spec):
        return _train_start(spec)

    def train_status(self):
        return _train_status()

    def train_cancel(self):
        return _train_cancel()

    def write_clipboard(self, text):
        return _clipboard_write(text)

    def put_engine_part(self, part, data_b64):
        """Store one engine part at runtime so the loopback server can serve it
        same-origin. The page calls this after fetching the engine, so a
        fetched-but-not-baked engine loads via import("/_engine/part/js")
        instead of a blob URL that WKWebView refuses."""
        import base64
        try:
            raw = base64.b64decode(str(data_b64))
            mime = "application/wasm" if part in ("wasm", "cwasm") else "text/javascript"
            _ENGINE_RUNTIME[part] = (raw, mime)
            return True
        except Exception:
            log_problem("put_engine_part(%r): decode failed" % (part,), sys.exc_info())
            return False

    # R58: the shell keeps model files on disk. The web view's storage quota
    # could not hold them; beside the app's own data there is no quota at all.
    def put_model_part(self, model_id, data_b64, first):
        return _put_model_part(str(model_id), data_b64, bool(first))

    def register_model_file(self, model_id, path):
        return _register_model_file(str(model_id), str(path))

    def delete_model_file(self, model_id):
        return _delete_model_file(str(model_id))

    def list_ollama_models(self):
        return _ollama_library()

    # R64: model files chosen with this computer's own dialog are linked where
    # they are, as Ollama's are (see _pick_model_files below).
    def pick_model_files(self):
        return _pick_model_files(self._main_window)

    # R65: the native engine, and the page file for the build kits
    def engine_info(self):
        return _engine_info()

    def engine_load(self, model_id, n_ctx=8192, mmproj_id=None):
        return _engine_start(model_id, n_ctx, mmproj_id)

    def engine_status(self):
        return _engine_status()

    def engine_unload(self):
        _engine_stop()
        return True

    def builtin_model(self):
        return _builtin_model()

    # R70: every built-in model, and the lineup's installs
    def builtin_models(self):
        return _builtin_models()

    def install_model(self, spec):
        return _install_model(spec)

    def install_status(self, key):
        return _install_status(key)

    def install_cancel(self, key):
        return _install_cancel(key)

    def installed_models(self):
        return _installed_models()

    # R97: a model's picture reader
    def install_projector(self, spec):
        return _install_projector(spec)

    def projector_for(self, model_id):
        return _mmproj_for(str(model_id))

    # R74: lending this computer's models to the person's other devices
    def share_status(self):
        return _share_status()

    def share_set(self, on):
        return _share_set(bool(on))

    def share_renew_code(self):
        return _share_renew_code()

    def read_app_page(self, offset=0, size=4194304):
        return _read_app_page(offset, size)

    # R61: the whole desk, kept on disk (see _save_state above).
    def load_state(self):
        return _load_state()

    def save_state(self, raw):
        return _save_state(raw)

    # R96: earlier desks, and finding the other computer
    def desk_list(self):
        return _desk_list()

    def desk_read(self, did):
        return _desk_read(did)

    def desk_keep_now(self, reason="before-restore"):
        return _desk_keep(reason or "kept")

    def desk_keep_text(self, raw, reason="before-adopt"):
        return _desk_keep(reason or "kept", raw if isinstance(raw, str) else str(raw or ""))

    def share_find(self):
        return _share_find()

    # R97: when one computer cannot reach the other, the other one calls
    def share_callins_set(self, on):
        return _callin_set(bool(on))

    def share_callout_add(self, addr, code):
        return _callout_add(addr, code)

    def share_callout_remove(self, addr):
        return _callout_remove(addr)

    def share_firewall_allow(self):
        return _share_firewall_allow()

    # R81: big texts, kept apart from the desk (see _big_put above).
    def big_put(self, bid, text):
        return _big_put(bid, text)

    def big_get(self, bid):
        return _big_get(bid)

    def big_del(self, bid):
        return _big_del(bid)

    def big_gc(self, live):
        return _big_gc(live)

    def close_app(self, which):
        """Close a native instrument window."""
        if which not in self.APP_TITLES:
            return False
        with self._lock:
            w = self._wins.get(which)
        if w is None:
            return False
        try:
            w.destroy()
        except Exception:
            log_problem("close_app(%r): destroy failed" % (which,), sys.exc_info())
        finally:
            with self._lock:
                self._wins.pop(which, None)
        return True

    def focus_app(self, which):
        if which not in self.APP_TITLES or not self._alive(which):
            return False
        w = self._wins[which]
        try:
            w.restore()
        except Exception:
            pass
        try:
            w.show()
        except Exception:
            pass
        return True

    def open_app(self, which):
        """Open (or focus) a separate native window for the instrument."""
        if which not in self.APP_TITLES:
            return False
        with self._lock:
            if self._alive(which):
                return self.focus_app(which)
            if which in self._creating:
                log_problem("open_app(%r): already opening, ignored" % (which,))
                return False
            self._creating.add(which)
        try:
            import webview
            api = self
            win = webview.create_window(
                self.APP_TITLES[which],
                url=self._app_url + "#ingenium=" + which,
                width=1050 if which == "tabula" else 340,
                height=760 if which == "tabula" else 500,
                min_size=(MIN_WIDTH, MIN_HEIGHT),
                background_color=self.APP_COLORS[which],
                confirm_close=False,
                text_select=True,
            )
            self._wins[which] = win
            try:
                win.events.closed += (lambda *a, w=which: self._wins.pop(w, None))
            except Exception:
                pass
            return True
        except Exception:
            log_problem("open_app(%r)" % (which,))
            self._wins.pop(which, None)
            return False
        finally:
            with self._lock:
                self._creating.discard(which)


class IngeniumApiSingle(_LiteWindows):
    """The API offered by default: no second native window, just file saves."""

    def __init__(self, app_url):
        self._app_url = app_url
        self._main_window = None

    def save_file(self, name, mime, data_b64):
        return save_bytes(None, name, mime, data_b64)

    def device_info(self):
        return _read_device_info()

    def relay(self, url, method="GET", headers=None, body=""):
        return _relay(url, method, headers, body)

    def relay_key(self):
        return _RELAY_SECRET

    def take_screenshot(self):
        return _take_screenshot()

    def read_clipboard(self):
        return _clipboard_read()

    # R78: training a small model from zero
    def train_available(self):
        return _train_available()

    def train_bases(self):
        return _train_bases()

    def train_start(self, spec):
        return _train_start(spec)

    def train_status(self):
        return _train_status()

    def train_cancel(self):
        return _train_cancel()

    def write_clipboard(self, text):
        return _clipboard_write(text)

    def put_engine_part(self, part, data_b64):
        """Store one engine part at runtime so the loopback server can serve it
        same-origin (same as IngeniumApi.put_engine_part)."""
        import base64
        try:
            raw = base64.b64decode(str(data_b64))
            mime = "application/wasm" if part in ("wasm", "cwasm") else "text/javascript"
            _ENGINE_RUNTIME[part] = (raw, mime)
            return True
        except Exception:
            log_problem("put_engine_part(%r): decode failed" % (part,), sys.exc_info())
            return False

    # R58: model files on disk, kept by the shell (see IngeniumApi).
    def put_model_part(self, model_id, data_b64, first):
        return _put_model_part(str(model_id), data_b64, bool(first))

    def register_model_file(self, model_id, path):
        return _register_model_file(str(model_id), str(path))

    def delete_model_file(self, model_id):
        return _delete_model_file(str(model_id))

    def list_ollama_models(self):
        return _ollama_library()

    def pick_model_files(self):
        return _pick_model_files(self._main_window)

    # R65: the native engine, and the page file for the build kits
    def engine_info(self):
        return _engine_info()

    def engine_load(self, model_id, n_ctx=8192, mmproj_id=None):
        return _engine_start(model_id, n_ctx, mmproj_id)

    def engine_status(self):
        return _engine_status()

    def engine_unload(self):
        _engine_stop()
        return True

    def builtin_model(self):
        return _builtin_model()

    # R70: every built-in model, and the lineup's installs
    def builtin_models(self):
        return _builtin_models()

    def install_model(self, spec):
        return _install_model(spec)

    def install_status(self, key):
        return _install_status(key)

    def install_cancel(self, key):
        return _install_cancel(key)

    def installed_models(self):
        return _installed_models()

    # R97: a model's picture reader
    def install_projector(self, spec):
        return _install_projector(spec)

    def projector_for(self, model_id):
        return _mmproj_for(str(model_id))

    # R74: lending this computer's models to the person's other devices
    def share_status(self):
        return _share_status()

    def share_set(self, on):
        return _share_set(bool(on))

    def share_renew_code(self):
        return _share_renew_code()

    def read_app_page(self, offset=0, size=4194304):
        return _read_app_page(offset, size)

    # R61: the whole desk, kept on disk.
    def load_state(self):
        return _load_state()

    def save_state(self, raw):
        return _save_state(raw)

    # R96: earlier desks, and finding the other computer
    def desk_list(self):
        return _desk_list()

    def desk_read(self, did):
        return _desk_read(did)

    def desk_keep_now(self, reason="before-restore"):
        return _desk_keep(reason or "kept")

    def desk_keep_text(self, raw, reason="before-adopt"):
        return _desk_keep(reason or "kept", raw if isinstance(raw, str) else str(raw or ""))

    def share_find(self):
        return _share_find()

    # R97: when one computer cannot reach the other, the other one calls
    def share_callins_set(self, on):
        return _callin_set(bool(on))

    def share_callout_add(self, addr, code):
        return _callout_add(addr, code)

    def share_callout_remove(self, addr):
        return _callout_remove(addr)

    def share_firewall_allow(self):
        return _share_firewall_allow()

    # R81: big texts, kept apart from the desk (see _big_put above).
    def big_put(self, bid, text):
        return _big_put(bid, text)

    def big_get(self, bid):
        return _big_get(bid)

    def big_del(self, bid):
        return _big_del(bid)

    def big_gc(self, live):
        return _big_gc(live)


def _read_html_text(path):
    """Read the app page as text, or return None if it cannot be read."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        log_problem("could not read app page for inline load", sys.exc_info())
        return None


def _engine_slot(html_path, slot_id):
    """(opening tag, text) of one baked engine slot in the app page, or
    (None, None). The bake escapes </script as <\\/script; undo that exactly
    once, and only that."""
    try:
        with open(html_path, "r", encoding="utf-8") as fh:
            html = fh.read()
    except Exception:
        return None, None
    import re as _re
    m = _re.search(r'(<script id="%s"[^>]*>)([\s\S]*?)</script>' % _re.escape(slot_id),
                   html, flags=_re.I)
    if not m:
        return None, None
    return m.group(1), m.group(2).replace("<\\/script", "</script")


def _engine_slot_text(html_path, slot_id):
    """The text of one baked engine slot, or None."""
    return _engine_slot(html_path, slot_id)[1]


def _engine_answer(html_path, part, cache):
    """(bytes, mime) for one engine route, or (b"0", "text/plain") when absent.
    Checks baked slots first, then runtime parts the page wrote via
    put_engine_part (so a fetched-but-not-baked engine also loads same-origin)."""
    if part == "has":
        js = _engine_slot_text(html_path, "ingenium-engine-js")
        wasm = _engine_slot_text(html_path, "ingenium-engine-wasm")
        cwasm = _engine_slot_text(html_path, "ingenium-engine-compat-wasm")
        baked = bool(js and (wasm or cwasm))
        runtime = bool(_ENGINE_RUNTIME.get("js") and
                       (_ENGINE_RUNTIME.get("wasm") or _ENGINE_RUNTIME.get("cwasm")))
        return b"1" if (baked or runtime) else b"0", "text/plain"
    # baked parts first
    if part not in cache:
        cache[part] = "empty"
    if cache[part] != "empty":
        return cache[part]
    # runtime parts (written by put_engine_part at fetch time)
    rt = _ENGINE_RUNTIME.get(part)
    if rt:
        return rt
    return b"", "text/plain"


def _engine_load_parts(html_path, cache):
    """Decode and cache the four engine parts as (bytes, mime). Blob-served
    engines forced WKWebView's awkward paths; this serves them as plain files
    from the app's own address, which every web view handles the ordinary way."""
    import base64
    plan = {
        "js": ("ingenium-engine-js", None, "text/javascript"),
        "wasm": ("ingenium-engine-wasm", "b64", "application/wasm"),
        "cwasm": ("ingenium-engine-compat-wasm", "b64", "application/wasm"),
        "cworker.js": ("ingenium-engine-compat-worker", None, "text/javascript"),
    }
    for part, (slot, kind, mime) in plan.items():
        if part in cache:
            continue
        tag, txt = _engine_slot(html_path, slot)
        if txt is None or len(txt.strip()) < 100:
            continue
        try:
            if kind == "b64":
                data = base64.b64decode("".join(txt.split()))
                # R64: the bake stores the wasm gzip-compressed (slot type "...-gz")
                if tag and '-gz"' in tag:
                    import gzip
                    data = gzip.decompress(data)
            else:
                data = txt.encode("utf-8")
            cache[part] = (data, mime)
        except Exception:
            log_problem("engine route: part %s would not decode" % part)


_LOCAL_SERVER = None  # kept alive for the app's lifetime
_ENGINE_RUNTIME = {}  # engine parts the page writes at runtime (not baked)

# R58: model files on DISK, kept by the shell. The web view's IndexedDB cannot
# hold multi-gigabyte models (the writes were refused, which is why cards read
# "kept for this session only" and vanished after restart). The shell writes
# the chosen file beside its own data and serves it back on the loopback
# address. It survives every restart and asks nothing of the web view's quota.
_MODEL_PATHS = {}  # id -> absolute path of a stored or registered model file
_MODEL_EXTERNAL = set()  # ids that point at files we did not copy (Ollama's)
_MODEL_META = {}  # R70: id -> details of a model installed from Ingenium's lineup


def _models_dir():
    """Where model copies live. Beside the app's other data, never on display."""
    try:
        home = pathlib.Path.home()
        if sys.platform == "darwin":
            d = home / "Library" / "Application Support" / APP_FOLDER / "models"
        elif sys.platform.startswith("win"):
            d = pathlib.Path(os.environ.get("APPDATA", str(home))) / APP_FOLDER / "models"
        else:
            d = home / ".local" / "share" / APP_FOLDER.lower() / "models"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _models_index_path():
    d = _models_dir()
    return (d / "index.json") if d else None


def _load_models_index():
    ip = _models_index_path()
    if not ip or not ip.is_file():
        return
    try:
        idx = json.loads(ip.read_text(encoding="utf-8"))
        for mid, rec in idx.items():
            p = rec.get("path", "")
            if p and os.path.isfile(p):
                _MODEL_PATHS[mid] = p
                if rec.get("external"):
                    _MODEL_EXTERNAL.add(mid)
                if isinstance(rec.get("meta"), dict):
                    _MODEL_META[mid] = rec["meta"]
    except Exception:
        pass


def _save_models_index():
    ip = _models_index_path()
    if not ip:
        return
    try:
        idx = {}
        for mid, p in _MODEL_PATHS.items():
            idx[mid] = {"path": p, "external": mid in _MODEL_EXTERNAL}
            if mid in _MODEL_META:
                idx[mid]["meta"] = _MODEL_META[mid]
        ip.write_text(json.dumps(idx), encoding="utf-8")
    except Exception:
        pass


def _model_path_for(mid):
    if mid in _MODEL_PATHS:
        return _MODEL_PATHS[mid]
    _load_models_index()
    return _MODEL_PATHS.get(mid)


def _put_model_part(mid, data_b64, first):
    """Append one chunk of a model file the page is storing. `first` starts a
    fresh file. Chunks arrive base64 over the bridge; writing is raw bytes on
    disk, so a 2 GB model costs no more memory than one chunk."""
    d = _models_dir()
    if d is None:
        return False
    try:
        raw = base64.b64decode(str(data_b64))
    except Exception:
        return False
    p = d / _safe_id(mid)
    try:
        with open(str(p), "wb" if first else "ab") as fh:
            fh.write(raw)
        _MODEL_PATHS[mid] = str(p)
        # written to the index on every part too: a crash mid-copy cannot
        # orphan the file
        _MODEL_EXTERNAL.discard(mid)
        if first or not _models_index_path() or not _models_index_path().is_file():
            _save_models_index()
        return True
    except Exception:
        log_problem("put_model_part(%r) failed" % (mid,), sys.exc_info())
        return False


def _safe_id(mid):
    s = "".join(ch for ch in str(mid) if ch.isalnum() or ch in "-_.") or "model"
    return s


def _register_model_file(mid, path):
    """Point an id at a model file that already exists on this machine (an
    Ollama blob). We never copy it and never delete it; we only read it."""
    rp = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(rp):
        return False
    _MODEL_PATHS[mid] = rp
    _MODEL_EXTERNAL.add(mid)
    _save_models_index()
    return True


def _pick_model_files(win):
    """R64: let the person choose model files with this computer's own dialog.
    The files are then linked where they are, as Ollama's are: nothing is
    copied (so any size is ready at once), and removing one from Ingenium
    never touches the file itself. Each row says whether the file is a GGUF
    model; anything else is refused with the reason."""
    try:
        import webview
        if win is None and getattr(webview, "windows", None):
            win = webview.windows[0]
        if win is None:
            return []
        kinds = getattr(webview, "FileDialog", None)
        dialog = kinds.OPEN if kinds is not None else webview.OPEN_DIALOG
        # No file-type filter: the dialogs differ in how they read one, and
        # every chosen file is checked below in any case.
        paths = win.create_file_dialog(dialog, allow_multiple=True) or ()
        if isinstance(paths, str):
            paths = (paths,)
        out = []
        for p in paths:
            name = os.path.basename(str(p))
            try:
                size = os.path.getsize(p)
                with open(p, "rb") as fh:
                    magic = fh.read(4)
            except OSError:
                out.append({"name": name, "ok": False, "reason": "it could not be read"})
                continue
            if magic != b"GGUF":
                out.append({"name": name, "ok": False,
                            "reason": "not a GGUF model file (other formats need converting to GGUF first)"})
                continue
            out.append({"path": os.path.abspath(str(p)), "name": name, "size": size, "ok": True})
        return out
    except Exception:
        log_problem("pick_model_files failed", sys.exc_info())
        return []


# ---- R65: the native engine (llama.cpp's own server, built for this computer) ----
# The page used to run models only with llama.cpp compiled to WebAssembly: slow,
# above all in Safari and the Mac window, and unable to open model files much
# over 2 to 3 GB. The desktop app now carries llama.cpp built for this computer
# (llama-server, in engine/<platform>/), and starts it on a loopback port with
# the model the page chooses. The page streams replies from it directly: the
# server answers the app's own page across ports. Nothing listens beyond this
# machine, every request must carry this session's private key (so no web
# page open in a browser can use it), and the server stops when the app closes.
_ENGINE = {"proc": None, "port": None, "key": None, "model_id": None, "args": None,
           "state": "off", "error": "", "note": "", "log": [], "started": 0.0, "gpu": ""}
_ENGINE_LOCK = threading.RLock()
_ENGINE_EXE = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
_ENGINE_SECRET = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
_ENGINE_DEVICES = {"list": None, "busy": False}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _engine_platform():
    import platform as _pl
    m = (_pl.machine() or "").lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    if sys.platform == "darwin":
        return "macos-" + arch
    if sys.platform.startswith("win"):
        return "windows-" + arch
    return "linux-" + arch


def _app_roots():
    """Where this app's own files can be: inside a built app, beside its
    program, or beside this script."""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    roots = [getattr(sys, "_MEIPASS", None), exe_dir, os.path.join(exe_dir, "..", "Resources"),
             os.path.join(exe_dir, "..", "Frameworks"), os.path.dirname(os.path.abspath(__file__))]
    out = []
    for r in roots:
        if r and os.path.normpath(r) not in out:
            out.append(os.path.normpath(r))
    return out


_ENGINE_DIR_FOUND = []


def _engine_dir():
    """The folder holding llama-server for this computer, or None. A built Mac
    app carries it as a folder (engine/<platform>/); the one-file Windows app
    carries it packed (engine/<platform>.zip) and unpacks it once into its data
    folder; run as a script, it sits beside the script."""
    if _ENGINE_DIR_FOUND and os.path.isfile(os.path.join(_ENGINE_DIR_FOUND[0], _ENGINE_EXE)):
        return _ENGINE_DIR_FOUND[0]
    plat = _engine_platform()
    found = None
    for root in _app_roots():
        d = os.path.join(root, "engine", plat)
        if os.path.isfile(os.path.join(d, _ENGINE_EXE)):
            found = d
            break
    if not found:
        for root in _app_roots():
            z = os.path.join(root, "engine", plat + ".zip")
            if os.path.isfile(z):
                found = _engine_unpack(z, plat)
                if found:
                    break
    if found:
        del _ENGINE_DIR_FOUND[:]
        _ENGINE_DIR_FOUND.append(found)
    return found


def _engine_unpack(zpath, plat):
    """Unpack the packed engine once into this app's data folder and use it
    from there on: the one-file app's own temporary folder is new each run,
    and the engine is about 100 MB of files."""
    import hashlib
    import zipfile
    try:
        st = os.stat(zpath)
        h = hashlib.sha1()
        with open(zpath, "rb") as fh:
            h.update(fh.read(1 << 20))
        h.update(str(st.st_size).encode())
        tag = h.hexdigest()[:12]
        home = os.path.expanduser("~")
        if sys.platform.startswith("win"):
            base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        elif sys.platform == "darwin":
            base = os.path.join(home, "Library", "Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        parent = os.path.join(base, APP_FOLDER, "engine")
        target = os.path.join(parent, "%s-%s" % (plat, tag))
        if os.path.isfile(os.path.join(target, ".complete")) and os.path.isfile(os.path.join(target, _ENGINE_EXE)):
            return target
        os.makedirs(parent, exist_ok=True)
        work = target + ".part%d" % os.getpid()
        shutil.rmtree(work, ignore_errors=True)
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.endswith("/") or name.startswith("/") or ".." in name.split("/"):
                    continue
                dest = os.path.join(work, *name.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not sys.platform.startswith("win"):
                    os.chmod(dest, mode)
        with open(os.path.join(work, ".complete"), "w") as fh:
            fh.write(tag)
        shutil.rmtree(target, ignore_errors=True)
        os.replace(work, target)
        # earlier engines of this app are no longer needed (one in use by
        # another open window simply stays until next time)
        for old in os.listdir(parent):
            if old != os.path.basename(target) and old.startswith(plat + "-"):
                shutil.rmtree(os.path.join(parent, old), ignore_errors=True)
        return target
    except Exception:
        log_problem("engine: unpacking %s failed" % zpath, sys.exc_info())
        return None


def _engine_exe(d):
    return os.path.join(d, _ENGINE_EXE)


def _engine_env(d):
    env = dict(os.environ)
    env["LLAMA_API_KEY"] = _ENGINE_SECRET   # in the environment, not on the command line
    if sys.platform.startswith("linux"):
        env["LD_LIBRARY_PATH"] = d + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def _engine_popen_kw(d):
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
              cwd=d, env=_engine_env(d))
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW: no console flashes up
    else:
        kw["start_new_session"] = True
    return kw


def _engine_list_devices(d):
    """What the engine itself can use besides the processor, in its own words
    (llama-server --list-devices): the graphics chips its build can drive."""
    out = []
    try:
        r = subprocess.run([_engine_exe(d), "--list-devices"], timeout=40, **_engine_popen_kw(d))
        text = r.stdout.decode("utf-8", "replace")
        seen = False
        for line in text.splitlines():
            if line.strip().lower().startswith("available devices"):
                seen = True
                continue
            m = re.match(r"^\s+([A-Za-z]+\d*):\s*(.+?)\s*(?:\((\d+)\s*MiB,\s*(\d+)\s*MiB free\))?\s*$", line)
            # graphics chips only: a maths library (BLAS) is listed too, and is not one
            if seen and m and re.match(r"(?i)(vulkan|cuda|rocm|hip|metal|mtl|sycl|opencl|musa)", m.group(1)):
                out.append({"id": m.group(1), "name": m.group(2).strip(),
                            "mib": int(m.group(3)) if m.group(3) else None,
                            "free_mib": int(m.group(4)) if m.group(4) else None})
    except Exception:
        log_problem("engine: listing devices failed", sys.exc_info())
    return out


def _engine_devices(d):
    """Cached; listed once in the background so the page never waits on it."""
    with _ENGINE_LOCK:
        if _ENGINE_DEVICES["list"] is not None or _ENGINE_DEVICES["busy"]:
            return _ENGINE_DEVICES["list"]
        _ENGINE_DEVICES["busy"] = True

    def work():
        found = _engine_list_devices(d)
        with _ENGINE_LOCK:
            _ENGINE_DEVICES.update(list=found, busy=False)
    threading.Thread(target=work, daemon=True).start()
    return None


def _engine_gpu(d):
    """The graphics chip the engine can use, or "" for the processor alone."""
    devs = _engine_devices(d) if d else []
    if devs:
        return devs[0]["name"]
    if devs is None and d and (_engine_platform() == "macos-arm64" or
                               os.path.isfile(os.path.join(d, "ggml-vulkan.dll"))):
        return "Metal" if _engine_platform() == "macos-arm64" else "Vulkan"
    return ""


def _mac_too_old():
    """R74: the cloud-built engine needs macOS 13.3 or newer. On an older
    macOS it cannot start, so say why in plain words instead of failing
    later; the page's own engine still works there. Returns the version
    text when this Mac is too old, otherwise ""."""
    if sys.platform != "darwin":
        return ""
    try:
        import platform
        v = platform.mac_ver()[0] or ""
        parts = [int(x) for x in re.findall(r"\d+", v)[:2]] + [0, 0]
        if v and (parts[0], parts[1]) < (13, 3) and parts[0] >= 10:
            return v
    except Exception:
        pass
    return ""


def _engine_info():
    d = _engine_dir()
    old = _mac_too_old()
    if d and old:
        log_problem("engine: this Mac runs macOS %s; the engine needs 13.3 or newer" % old)
        d = None
    info = {"available": bool(d), "platform": _engine_platform(), "gpu": "", "devices": []}
    if old:
        info["reason"] = ("This Mac runs macOS %s. The app's fast engine needs macOS 13.3 or newer, so models run "
                          "in the slower engine inside the page. Updating macOS (free, in System Settings, General, "
                          "Software Update) makes the fast engine work." % old)
    if d:
        info["version"] = "llama.cpp"
        # beside the engine, or (inside a Mac app) among the app's resources
        for vp in [os.path.join(d, "VERSION.txt")] + [os.path.join(r, "engine", _engine_platform(), "VERSION.txt") for r in _app_roots()]:
            if os.path.isfile(vp):
                try:
                    with open(vp, encoding="utf-8") as fh:
                        info["version"] = fh.read().strip() or "llama.cpp"
                    break
                except Exception:
                    pass
        devs = _engine_devices(d)
        info["devices"] = devs or []
        info["gpu"] = _engine_gpu(d)
        info["threads"] = os.cpu_count() or 0
    with _ENGINE_LOCK:
        info.update(state=_ENGINE["state"], model_id=_ENGINE["model_id"], port=_ENGINE["port"])
    info["device"] = _read_device_info()
    return info


def _engine_free_port(first=17990):
    import socket
    for port in list(range(first, first + 20)) + [0]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise OSError("no free port on 127.0.0.1")


def _engine_reader(proc):
    """Keep the last lines the engine prints, for the page and the crash log."""
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            with _ENGINE_LOCK:
                if _ENGINE["proc"] is proc:
                    _ENGINE["log"].append(line)
                    del _ENGINE["log"][:-300]
    except Exception:
        pass


def _engine_stop():
    with _ENGINE_LOCK:
        p = _ENGINE["proc"]
        _ENGINE.update(proc=None, port=None, key=None, model_id=None, args=None, state="off",
                       error="", note="", gpu="")
    if p is None:
        return
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


# The engine's reasons, in plain words. The first pattern found in its last
# lines wins; anything else is passed on in the engine's own words.
_ENGINE_REASONS = [
    (r"invalid magic|not a gguf|failed to read magic",
     "This file is not a GGUF model file."),
    (r"unknown model architecture: '([^']+)'",
     "This engine does not know this model's design ('{0}'). A newer engine build is needed for it."),
    (r"out of memory|failed to allocate|unable to allocate|cannot allocate|bad_alloc|ErrorOutOfDeviceMemory",
     "There was not enough memory for this model at this Memory setting. Try a smaller Memory in Controls, or a smaller model."),
    (r"illegal instruction|SIGILL|0xc000001d",
     "This computer's processor lacks an instruction the engine was built for."),
    (r"tensor .* data is not within the file bounds|file is truncated|unexpectedly reached end of file",
     "The model file is incomplete (it may not have finished downloading)."),
    (r"failed to load model|error loading model",
     "The engine could not open this model file."),
]


def _engine_error_from_log(lines):
    text = "\n".join(lines)
    for pat, say in _ENGINE_REASONS:
        m = re.search(pat, text, re.I)
        if m:
            return say.format(*m.groups())
    for line in reversed(lines):
        low = line.lower()
        if "error" in low or "failed" in low or "unable" in low:
            return re.sub(r"^\S*\s*(?:[EW]\s+)?", "", line).strip()[:300] or line[:300]
    return ""


# 6.2: past this much memory the engine keeps it in eight bits (half the room); cleared for the session when an engine
# refuses it, so every later start goes without
_KVQ_AT = 32768
_KVQ_OK = [True]


def _engine_kvq(n_ctx):
    try:
        return _KVQ_OK[0] and int(n_ctx) > _KVQ_AT
    except Exception:
        return False


def _engine_launch(path, n_ctx, mmp, use_gpu, drafter=None):
    """Start llama-server. Returns (proc, port) or raises."""
    d = _engine_dir()
    exe = _engine_exe(d)
    if not sys.platform.startswith("win") and not os.access(exe, os.X_OK):
        try:
            os.chmod(exe, 0o755)
        except Exception:
            pass
    port = _engine_free_port()
    args = [exe, "-m", path, "--host", "127.0.0.1", "--port", str(port), "-c", str(n_ctx),
            "-np", "1", "--jinja", "--no-webui", "--cache-reuse", "256"]
    # 6.2: a long memory in eight bits, with the flash attention it needs
    if _engine_kvq(n_ctx):
        args += ["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0"]
    # With a graphics chip the engine fits as many layers on it as its memory
    # holds (its own --fit, on by default); without one, none are sent there.
    if not use_gpu:
        args += ["-ngl", "0"]
    if mmp:
        args += ["--mmproj", mmp]
        if not use_gpu:
            args += ["--no-mmproj-offload"]
    # R73: a model's speed-up file (its maker's small draft model, such as
    # Liquid AI's DSpark for LFM2.5) lets the engine guess several words ahead
    # and check them in one step: the same replies, sooner
    if drafter and not mmp:
        args += ["-md", drafter]
        if not use_gpu:
            args += ["-ngld", "0"]
    proc = subprocess.Popen(args, **_engine_popen_kw(d))
    threading.Thread(target=_engine_reader, args=(proc,), daemon=True).start()
    return proc, port, args


# 6.3 (app_ctx_63.py): the longest memory a model may be given, when it was trained for it
_ING_CTX_MAX = 1048576


def _engine_start(model_id, n_ctx=8192, mmproj_id=None, gpu=True):
    """Start the engine with a model the shell knows (linked in place or kept
    in the app's folder). Returns at once; _engine_status follows the load."""
    d = _engine_dir()
    if not d:
        return {"ok": False, "error": "This copy of the app carries no native engine."}
    path = _model_path_for(str(model_id))
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "That model's file is no longer where it was. Add it again on the Models page."}
    mmp = _model_path_for(str(mmproj_id)) if mmproj_id else None
    if mmproj_id and not (mmp and os.path.isfile(mmp)):
        mmp = None
    try:
        n_ctx = max(512, min(int(n_ctx or 8192), _ING_CTX_MAX))
    except Exception:
        n_ctx = 8192
    gpu_name = _engine_gpu(d)
    use_gpu = bool(gpu) and bool(gpu_name)
    # R73: measured in the cloud builds, a speed-up file made replies slower
    # where the model runs on the processor alone (about a quarter to a third
    # slower on GitHub's Windows and Intel Mac machines), so it is used only
    # when the engine runs on a graphics chip
    drafter = None if (mmp or not use_gpu) else _drafter_for(str(model_id))
    key = (path, n_ctx, mmp, drafter)
    with _ENGINE_LOCK:
        p = _ENGINE["proc"]
        if p is not None and p.poll() is None and _ENGINE["key"] == key:
            return {"ok": True, "port": _ENGINE["port"], "reused": True}
    _engine_stop()
    try:
        proc, port, args = _engine_launch(path, n_ctx, mmp, use_gpu, drafter)
    except Exception as e:
        log_problem("engine: could not start", sys.exc_info())
        return {"ok": False, "error": "The engine could not start: %s" % e}
    with _ENGINE_LOCK:
        _ENGINE.update(proc=proc, port=port, key=key, model_id=str(model_id), args=(path, n_ctx, mmp, drafter),
                       state="loading", error="", note="", log=[], started=time.time(),
                       gpu=gpu_name if use_gpu else "", draft=bool(drafter), kvq=_engine_kvq(n_ctx))
    return {"ok": True, "port": port}


def _engine_status():
    with _ENGINE_LOCK:
        p, port, state = _ENGINE["proc"], _ENGINE["port"], _ENGINE["state"]
        model_id, started, gpu = _ENGINE["model_id"], _ENGINE["started"], _ENGINE["gpu"]
        tail, note = list(_ENGINE["log"][-12:]), _ENGINE["note"]
        whole = list(_ENGINE["log"])
    if p is None:
        with _ENGINE_LOCK:
            return {"state": _ENGINE["state"], "error": _ENGINE["error"]}
    code = p.poll()
    if code is not None:
        err = _engine_error_from_log(whole) or ("The engine stopped (code %s)." % code)
        log_problem("engine stopped: %s\n  %s" % (err, "\n  ".join(tail)))
        # 6.2: an engine that cannot keep its memory in eight bits (or lacks flash attention) starts again without it,
        # at the same memory; every later start this session goes without
        if state == "loading":
            with _ENGINE_LOCK:
                args = _ENGINE["args"] if _ENGINE["proc"] is p else None
                kvq = bool(_ENGINE.get("kvq"))
            if args and kvq:
                _KVQ_OK[0] = False
                try:
                    proc, port2, _a = _engine_launch(args[0], args[1], args[2], bool(gpu), args[3] if len(args) > 3 else None)
                    with _ENGINE_LOCK:
                        if _ENGINE["proc"] is p:
                            _ENGINE.update(proc=proc, port=port2, state="loading", log=[], kvq=False,
                                           note="This computer's engine could not keep a long memory in eight bits, so it keeps it in full (it needs more room).",
                                           started=time.time())
                    return {"state": "loading", "port": port2, "model_id": model_id, "log": [],
                            "seconds": 0.0, "note": _ENGINE["note"]}
                except Exception:
                    log_problem("engine: the retry without the eight-bit memory could not start", sys.exc_info())
        # R73: a speed-up file that the engine cannot use is no reason to give
        # up either: load the model again without it.
        if state == "loading":
            with _ENGINE_LOCK:
                args = _ENGINE["args"] if _ENGINE["proc"] is p else None
            if args and len(args) > 3 and args[3]:
                try:
                    proc, port2, _a = _engine_launch(args[0], args[1], args[2], bool(gpu), None)
                    with _ENGINE_LOCK:
                        if _ENGINE["proc"] is p:
                            _ENGINE.update(proc=proc, port=port2, state="loading", log=[], draft=False,
                                           args=(args[0], args[1], args[2], None),
                                           note="The model's speed-up file could not be used, so it runs without it.",
                                           started=time.time())
                    return {"state": "loading", "port": port2, "model_id": model_id, "log": [],
                            "seconds": 0.0, "note": _ENGINE["note"]}
                except Exception:
                    log_problem("engine: the retry without the speed-up file could not start", sys.exc_info())
        # A graphics chip that cannot take the model is no reason to give up:
        # load it again on the processor alone, as the iPhone and iPad app does.
        if state == "loading" and gpu:
            with _ENGINE_LOCK:
                args = _ENGINE["args"] if _ENGINE["proc"] is p else None
            if args:
                try:
                    proc, port2, _a = _engine_launch(args[0], args[1], args[2], False, args[3] if len(args) > 3 else None)
                    with _ENGINE_LOCK:
                        if _ENGINE["proc"] is p:
                            _ENGINE.update(proc=proc, port=port2, state="loading", log=[], gpu="",
                                           note=("The graphics chip lacks the memory for this model at this Memory setting, so it runs on the processor instead (slower)."
                                                 if "not enough memory" in err else
                                                 "The graphics chip could not load this model, so it runs on the processor instead (slower)."),
                                           started=time.time())
                    return {"state": "loading", "port": port2, "model_id": model_id, "log": [],
                            "seconds": 0.0, "note": _ENGINE["note"]}
                except Exception:
                    log_problem("engine: the processor-only retry could not start", sys.exc_info())
        with _ENGINE_LOCK:
            if _ENGINE["proc"] is p:
                _ENGINE.update(proc=None, port=None, key=None, args=None, state="error", error=err, gpu="")
        return {"state": "error", "error": err, "log": tail}
    if state == "loading":
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/health" % port)
            with _NO_PROXY.open(req, timeout=1.5) as r:
                if r.status == 200:
                    state = "ready"
        except Exception:
            pass
        if state == "ready":
            with _ENGINE_LOCK:
                if _ENGINE["proc"] is p:
                    _ENGINE["state"] = "ready"
    out = {"state": state, "port": port, "model_id": model_id, "log": tail,
           "seconds": round(time.time() - started, 1), "gpu": gpu, "note": note, "draft": bool(_ENGINE.get("draft"))}
    if state == "ready":
        out["key"] = _ENGINE_SECRET
    return out


_BUILTIN_LINEUP = {}


def _drafter_for(mid):
    """R73: the speed-up file installed for this model's lineup entry (stored
    as that entry's key with -dspark), when it is on disk; otherwise None."""
    try:
        _load_models_index()
        if mid.startswith("builtin-") and mid not in _BUILTIN_LINEUP:
            _builtin_models()
        key = _BUILTIN_LINEUP.get(mid) or (_MODEL_META.get(mid) or {}).get("lineup") or ""
        if not key or key.endswith("-dspark"):
            return None
        for dmid, meta in list(_MODEL_META.items()):
            if meta.get("lineup") == key + "-dspark":
                p = _MODEL_PATHS.get(dmid)
                if p and os.path.isfile(p):
                    return p
    except Exception:
        log_problem("engine: looking for a speed-up file failed", sys.exc_info())
    return None


def _builtin_models():
    """The models this app was built with: models/builtin.json lists them (R70;
    an app from before carried one) beside the files themselves (inside the Mac
    app; in the models folder beside the Windows app). Each is linked where it
    is: never copied, never deleted. A file of the wrong size (an unfinished
    copy) is left out."""
    for root in _app_roots():
        mj = os.path.join(root, "models", "builtin.json")
        if not os.path.isfile(mj):
            continue
        out = []
        try:
            with open(mj, encoding="utf-8") as fh:
                meta = json.load(fh)
            entries = meta.get("models") if isinstance(meta.get("models"), list) else [meta]
            for e in entries:
                fname = os.path.basename(str(e.get("file") or ""))
                path = os.path.join(os.path.dirname(mj), fname)
                if not fname or not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                if e.get("size") and int(e["size"]) != size:
                    log_problem("built-in model: %s is %d bytes, %s expected; left out" % (fname, size, e["size"]))
                    continue
                mid = "builtin-" + _safe_id(fname)
                _MODEL_PATHS[mid] = path
                _MODEL_EXTERNAL.add(mid)
                if e.get("lineup"):
                    _BUILTIN_LINEUP[mid] = str(e["lineup"])
                out.append({"id": mid, "name": str(e.get("name") or fname), "file": fname, "size": size,
                            "why": str(e.get("why") or ""), "licence": str(e.get("licence") or ""),
                            "lineup": str(e.get("lineup") or "")})
        except Exception:
            log_problem("built-in models: %s is unreadable" % mj, sys.exc_info())
        if out:
            return out
    return []


def _builtin_model():
    """The first built-in model (what an app from before R70 offered)."""
    found = _builtin_models()
    return found[0] if found else None


# ---- R70: installing the models of Ingenium's lineup ----
# The page asks for a model of its lineup by name, address, size and SHA-256.
# The file comes straight from Hugging Face into the app's own models folder,
# carries on from where a broken download stopped, can be stopped, and is kept
# only when its SHA-256 matches.
_INSTALLS = {}
_INSTALLS_LOCK = threading.Lock()


def _install_check(spec):
    try:
        key = str(spec.get("key") or "")
        url = str(spec.get("url") or "")
        sha = str(spec.get("sha256") or "").lower()
        fname = str(spec.get("file") or "")
        size = int(spec.get("size") or 0)
    except Exception:
        return None
    if not (key and url.startswith("https://huggingface.co/") and re.match(r"^[0-9a-f]{64}$", sha)
            and re.match(r"^[A-Za-z0-9._-]{1,200}\.gguf$", fname) and size > 0):
        return None
    tiers = spec.get("tiers") if isinstance(spec.get("tiers"), list) else []
    return {"key": key, "url": url, "sha256": sha, "file": fname, "size": size,
            "name": str(spec.get("name") or fname), "tiers": [str(t) for t in tiers][:8]}


def _install_model(spec):
    sp = _install_check(spec if isinstance(spec, dict) else {})
    if sp is None:
        return {"ok": False, "error": "The request did not name a model of Ingenium's lineup properly."}
    with _INSTALLS_LOCK:
        job = _INSTALLS.get(sp["key"])
        if job and job["state"] == "running":
            return {"ok": True, "already": True}
        job = {"state": "running", "received": 0, "total": sp["size"], "phase": "start", "error": "", "id": None,
               "stop": threading.Event()}
        _INSTALLS[sp["key"]] = job
    threading.Thread(target=_install_worker, args=(job, sp), daemon=True).start()
    return {"ok": True}


# ---- R97: a model's picture reader (its projector, "mmproj") ----
# Her report: "All of the local models inside of Ingenium kept on asking for a projector." Models of the lineup
# that can see (Qwen3.5) now fetch their picture reader with one press. Its maker lists each file's SHA-256 on
# Hugging Face; the app reads it there over HTTPS, then installs the file the way it installs a model (carried on
# where it stopped, and kept only when the fingerprint matches).
def _hf_get_json(url):
    ctx = ssl.create_default_context(cafile=_CERT_FILE) if _CERT_FILE else ssl.create_default_context()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Ingenium"}), timeout=30, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if sys.platform == "darwin" and _is_cert_error(e) and os.path.isfile("/usr/bin/curl"):
            out = subprocess.run(["/usr/bin/curl", "-sSL", "--fail", "-A", "Ingenium", url], capture_output=True, timeout=60)
            if out.returncode == 0:
                return json.loads(out.stdout.decode("utf-8"))
        raise


def _projector_resolve(repos, prefer):
    """The picture reader to fetch: the first repository that lists one, the
    preferred kind (F16) first. Its size and SHA-256 come from that listing."""
    order = dict((str(x).lower(), i) for i, x in enumerate(prefer or []))
    last = ""
    for repo in repos or []:
        repo = str(repo)
        if not re.match(r"^[A-Za-z0-9_.-]{1,96}/[A-Za-z0-9_.-]{1,96}$", repo):
            continue
        try:
            listing = _hf_get_json("https://huggingface.co/api/models/%s/tree/main" % repo)
        except Exception as e:
            last = _relay_reason(e)
            continue
        files = [f for f in (listing or []) if isinstance(f, dict) and f.get("type") == "file"
                 and "mmproj" in str(f.get("path") or "").lower() and str(f.get("path")).lower().endswith(".gguf")]
        if not files:
            continue
        files.sort(key=lambda f: (order.get(str(f.get("path")).lower(), 99), int(f.get("size") or 0)))
        f = files[0]
        lfs = f.get("lfs") or {}
        sha = str(lfs.get("oid") or lfs.get("sha256") or "").lower()
        size = int(lfs.get("size") or f.get("size") or 0)
        if re.match(r"^[0-9a-f]{64}$", sha) and size > 0:
            return {"repo": repo, "file": f["path"], "sha256": sha, "size": size}
    return {"error": last or "its maker lists no picture reader for it"}


def _install_projector(spec):
    spec = spec if isinstance(spec, dict) else {}
    key = str(spec.get("key") or "")
    if not re.match(r"^[a-z0-9._-]{2,60}$", key):
        return {"ok": False, "error": "The request did not name a model of Ingenium's lineup properly."}
    found = _projector_resolve(spec.get("repos") or [], spec.get("prefer") or [])
    if found.get("error"):
        return {"ok": False, "error": "Its picture reader could not be found (%s)." % found["error"]}
    if "/" in found["file"]:
        return {"ok": False, "error": "Its picture reader is kept in a folder the app does not read from."}
    r = _install_model({"key": key + "-mmproj", "url": "https://huggingface.co/%s/resolve/main/%s" % (found["repo"], found["file"]),
                        "sha256": found["sha256"], "file": found["file"], "size": found["size"],
                        "name": "%s picture reader" % (spec.get("name") or key), "tiers": []})
    return dict(r, key=key + "-mmproj", size=found["size"], file=found["file"], repo=found["repo"])


def _install_status(key):
    with _INSTALLS_LOCK:
        job = _INSTALLS.get(str(key))
        if not job:
            return {"state": "none"}
        return {k: job[k] for k in ("state", "received", "total", "phase", "error", "id")}


def _install_cancel(key):
    with _INSTALLS_LOCK:
        job = _INSTALLS.get(str(key))
    if job and job["state"] == "running":
        job["stop"].set()
    return {"ok": True}


def _installed_models():
    """The models installed from the lineup that are still on disk."""
    _load_models_index()
    out = []
    for mid, meta in list(_MODEL_META.items()):
        p = _MODEL_PATHS.get(mid)
        if p and os.path.isfile(p):
            lk = meta.get("lineup") or ""
            role = "drafter" if lk.endswith("-dspark") else ("projector" if lk.endswith("-mmproj") else "model")
            out.append({"id": mid, "name": meta.get("name") or os.path.basename(p), "file": meta.get("file") or os.path.basename(p),
                        "size": os.path.getsize(p), "lineup": lk, "role": role})
    return out


class _InstallStopped(Exception):
    pass


def _is_cert_error(e):
    r = getattr(e, "reason", None)
    return "certificate" in str(r if r is not None else e).lower()


def _curl_fetch(url, part, job, say, exe="/usr/bin/curl"):
    """R84: the Mac's own curl, which trusts the system's certificates,
    carries a download that Python's own check refused. It carries on from
    where the part file stops; the caller checks the fingerprint after."""
    if not os.path.isfile(exe):
        return False
    args = [exe, "-L", "--fail", "--silent", "--show-error", "--retry", "3", "-A", "Ingenium",
            "-C", "-", "-o", str(part), url]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while p.poll() is None:
        if job["stop"].is_set():
            p.kill()
            raise _InstallStopped()
        try:
            say(received=os.path.getsize(str(part)), phase="download")
        except Exception:
            pass
        time.sleep(0.4)
    err = (p.stderr.read() if p.stderr else b"").decode("utf-8", "replace").strip()
    if p.returncode != 0:
        raise RuntimeError("the download stopped (%s)" % (err[-240:] or ("curl gave %d" % p.returncode)))
    return True


def _install_worker(job, sp):
    def say(**kw):
        with _INSTALLS_LOCK:
            job.update(kw)
    try:
        mid = "lineup-" + _safe_id(sp["key"])
        d = _models_dir()
        if d is None:
            raise RuntimeError("the app's models folder could not be made")
        dest = d / ("l" + sp["sha256"][:12] + ".gguf")
        if dest.is_file() and dest.stat().st_size == sp["size"] and _MODEL_META.get(mid, {}).get("sha256") == sp["sha256"]:
            _MODEL_PATHS[mid] = str(dest)
            say(state="done", id=mid, received=sp["size"], phase="verify")
            return
        part = d / (sp["sha256"][:16] + ".dl.part")
        have = part.stat().st_size if part.is_file() else 0
        if have > sp["size"]:
            part.unlink()
            have = 0
        free = shutil.disk_usage(str(d)).free
        if free < sp["size"] - have + 64 * 1024 * 1024:
            raise RuntimeError("there is not enough free space on this computer (%.1f GB needed, %.1f GB free)"
                               % ((sp["size"] - have) / 1e9, free / 1e9))
        h = hashlib.sha256()
        if have:
            say(phase="resume", received=have)
            done = 0
            with open(str(part), "rb") as fh:
                while done < have:
                    chunk = fh.read(min(4 << 20, have - done))
                    if not chunk:
                        break
                    h.update(chunk)
                    done += len(chunk)
                    if job["stop"].is_set():
                        raise _InstallStopped()
            if done != have:
                part.unlink()
                have = 0
                h = hashlib.sha256()
        ctx = ssl.create_default_context(cafile=_CERT_FILE) if _CERT_FILE else ssl.create_default_context()
        last_err = ""
        cert_fail = False
        for attempt in range(1, 7):
            if have >= sp["size"]:
                break
            if job["stop"].is_set():
                raise _InstallStopped()
            try:
                req = urllib.request.Request(sp["url"], headers={"User-Agent": "Ingenium"})
                if have:
                    req.add_header("Range", "bytes=%d-" % have)
                with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                    if r.status == 200 and have:   # the whole file again: start over
                        have = 0
                        h = hashlib.sha256()
                    elif r.status == 206:
                        m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range") or "")
                        if not m or int(m.group(1)) != have:
                            raise IOError("the server sent the wrong part of the file")
                    with open(str(part), "r+b" if (have and part.is_file()) else "wb") as out:
                        out.seek(have)
                        out.truncate()
                        last = 0.0
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            if have + len(chunk) > sp["size"]:
                                raise IOError("the file is larger than expected")
                            out.write(chunk)
                            h.update(chunk)
                            have += len(chunk)
                            if job["stop"].is_set():
                                raise _InstallStopped()
                            now = time.time()
                            if now - last > 0.3:
                                last = now
                                say(received=have, phase="download")
            except _InstallStopped:
                raise
            except urllib.error.HTTPError as e:
                last_err = "the server answered %d" % e.code
                if e.code in (401, 403, 404):
                    break
            except Exception as e:
                last_err = _relay_reason(e) if "_relay_reason" in globals() else str(e)
                if _is_cert_error(e):
                    cert_fail = True
                    break
            if have < sp["size"]:
                if job["stop"].wait(min(16, 2 ** attempt)):
                    raise _InstallStopped()
        if cert_fail and have < sp["size"] and sys.platform == "darwin":
            log_problem("install of %s: Python refused the certificate (%s); the Mac's own curl carries it"
                        % (sp.get("key"), last_err))
            if _curl_fetch(sp["url"], part, job, say):
                have = part.stat().st_size if part.is_file() else 0
                say(phase="verify", received=have)
                h = hashlib.sha256()
                with open(str(part), "rb") as fh:
                    while True:
                        chunk = fh.read(4 << 20)
                        if not chunk:
                            break
                        h.update(chunk)
                        if job["stop"].is_set():
                            raise _InstallStopped()
        if have != sp["size"]:
            raise RuntimeError("the download stopped part way (%s). Install again to carry on from where it stopped."
                               % (last_err or "no reason given"))
        say(phase="verify", received=have)
        if h.hexdigest() != sp["sha256"]:
            part.unlink()
            raise RuntimeError("the downloaded file does not match its SHA-256 fingerprint, so it was deleted")
        os.replace(str(part), str(dest))
        _MODEL_PATHS[mid] = str(dest)
        _MODEL_EXTERNAL.discard(mid)
        _MODEL_META[mid] = {"lineup": sp["key"], "name": sp["name"], "file": sp["file"], "sha256": sp["sha256"],
                            "tiers": sp["tiers"]}
        _save_models_index()
        say(state="done", id=mid)
    except _InstallStopped:
        say(state="cancelled", error="Stopped.")
    except Exception as e:
        log_problem("install of %s failed" % sp.get("key"), sys.exc_info())
        say(state="error", error=str(e) or e.__class__.__name__)


# ---------------------------------------------------------------------------
# R74: sharing this computer's models with the person's other devices.
#
# The Mac mini (or any computer running this app) can lend its models to the
# person's other devices on the same home network: the MacBook, the Windows
# laptops, the Fire tablet, the iPhones and iPads. They ask through a small
# gateway that speaks the common OpenAI chat format; the gateway checks the
# pairing code, loads the model asked for into this computer's own engine,
# and streams the reply back. Nothing leaves the home network, and nothing is
# shared until the person switches sharing on in Settings.
#
# * The gateway listens on the network only while sharing is on, on port 17860
#   (or the next free one), and answers only requests that carry the pairing
#   code. After eight wrong codes from one address in ten minutes, that
#   address waits ten minutes.
# * One model runs at a time: a request for another model loads that one.
#   The app's own chat notices and loads its model again when it is next used.
# * While sharing is on, the computer is kept from going to sleep by itself
#   (the screen may still turn off), so it stays reachable from another room.
# ---------------------------------------------------------------------------
_SHARE = {"server": None, "port": None, "error": "", "fails": {}, "busy": 0, "last": {}, "awake": None, "wanted": False, "lend": False}
_SHARE_LOCK = threading.Lock()
_SHARE_PORT = 17860
_SHARE_CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _share_cfg_path():
    d = _models_dir()
    return (d.parent / "share.json") if d is not None else None


def _share_cfg():
    p = _share_cfg_path()
    try:
        with open(str(p), encoding="utf-8") as fh:
            c = json.load(fh)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _share_cfg_save(c):
    p = _share_cfg_path()
    if p is None:
        return
    try:
        tmp = str(p) + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(c, fh)
        os.replace(tmp, str(p))
    except Exception:
        log_problem("share: could not save its settings", sys.exc_info())


def _share_new_code():
    import secrets
    raw = "".join(secrets.choice(_SHARE_CODE_LETTERS) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def _share_code():
    c = _share_cfg()
    code = str(c.get("code") or "")
    if not re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code):
        code = _share_new_code()
        c["code"] = code
        _share_cfg_save(c)
    return code


def _share_norm(code):
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def _share_computer_name():
    import socket
    name = ""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
            name = (r.stdout or "").strip()
        except Exception:
            name = ""
    if not name:
        name = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "").split(".")[0]
    return name


def _share_addresses(port):
    """How the other devices can reach this computer: its own name on the
    home network (stable, where the device understands .local names), then
    its address (works everywhere, but the router may change it)."""
    import socket
    out = []
    name = _share_computer_name()
    if name:
        out.append("%s.local:%d" % (name, port))
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.255.255", 1))   # no packet is sent; this only picks the home network's interface
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except Exception:
        pass
    for ip in ips:
        if ip and not ip.startswith("127.") and ("%s:%d" % (ip, port)) not in out:
            out.append("%s:%d" % (ip, port))
    return out


def _share_models():
    """The models this computer can lend: its built-in ones, the ones it
    installed and the ones linked in place (never a speed-up file)."""
    _load_models_index()
    out, seen = [], set()
    try:
        for b in _builtin_models():
            out.append({"id": b["id"], "name": b.get("name") or b["file"], "file": b["file"], "size": b.get("size") or 0,
                        "lineup": b.get("lineup") or _BUILTIN_LINEUP.get(b["id"], "")})
            seen.add(b["id"])
    except Exception:
        pass
    for m in _installed_models():
        if m.get("role") in ("drafter", "projector") or m["id"] in seen:
            continue
        out.append({"id": m["id"], "name": m["name"], "file": m["file"], "size": m["size"], "lineup": m.get("lineup") or "",
                    "vision": bool(_mmproj_for(m["id"]))})
        seen.add(m["id"])
    for mid, p in list(_MODEL_PATHS.items()):
        if mid in seen or mid == "selftest-drafter" or not p or not os.path.isfile(p) or not p.lower().endswith(".gguf"):
            continue
        lk = (_MODEL_META.get(mid) or {}).get("lineup", "")
        if lk.endswith("-dspark") or lk.endswith("-mmproj") or "mmproj" in os.path.basename(p).lower():
            continue
        out.append({"id": mid, "name": re.sub(r"\.gguf$", "", os.path.basename(p), flags=re.I), "file": os.path.basename(p),
                    "size": os.path.getsize(p), "lineup": (_MODEL_META.get(mid) or {}).get("lineup", "")})
        seen.add(mid)
    return out


def _share_keep_awake(on):
    """While sharing, the computer does not go to sleep by itself."""
    old = _SHARE.get("awake")
    if on and old is None:
        if sys.platform == "darwin":
            try:
                _SHARE["awake"] = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                log_problem("share: caffeinate could not start", sys.exc_info())
        elif sys.platform.startswith("win"):
            stop = threading.Event()

            def hold():
                try:
                    import ctypes
                    es_continuous, es_system = 0x80000000, 0x00000001
                    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system)
                    stop.wait()
                    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)
                except Exception:
                    log_problem("share: could not keep Windows awake", sys.exc_info())
            threading.Thread(target=hold, daemon=True).start()
            _SHARE["awake"] = stop
    elif not on and old is not None:
        try:
            if hasattr(old, "terminate"):
                old.terminate()
            else:
                old.set()
        except Exception:
            pass
        _SHARE["awake"] = None


def _share_wait_ready(mid, timeout=1200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = _engine_status()
        if st.get("state") == "ready" and str(st.get("model_id")) == mid:
            return st
        if st.get("state") in ("error", "off"):
            raise RuntimeError(st.get("error") or "the engine stopped")
        time.sleep(0.4)
    raise RuntimeError("the model was still loading after 20 minutes")


def _share_handler():
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "Ingenium"
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # a page opened in a browser asks before it reaches a home-network address
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, msg):
            self._json(code, {"error": {"message": msg, "code": code}})

        def _allowed(self):
            ip = self.client_address[0]
            now = time.time()
            with _SHARE_LOCK:
                fails = [t for t in _SHARE["fails"].get(ip, []) if now - t < 600]
                _SHARE["fails"][ip] = fails
                if len(fails) >= 8:
                    self._err(429, "Too many wrong pairing codes from this device. Wait ten minutes, then try again.")
                    return False
            got = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1).strip()
            import hmac as _hmac
            if not _hmac.compare_digest(_share_norm(got), _share_norm(_share_code())):
                with _SHARE_LOCK:
                    _SHARE["fails"].setdefault(ip, []).append(now)
                self._err(401, "The pairing code is not right. It is shown on the sharing computer, in Settings.")
                return False
            with _SHARE_LOCK:
                _SHARE["last"][ip] = now
            return True

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/ingenium/hello":
                lend = bool(_SHARE.get("lend"))
                return self._json(200, {"app": "Ingenium", "name": APP_NAME, "computer": _share_computer_name(), "needs_code": True,
                                        "lends": lend, "models": len(_share_models()) if lend else 0, "callins": True})
            if not self._allowed():
                return
            if path == "/ingenium/callins":
                return self._json(200, {"callins": _callin_list()})
            if path.startswith("/ingenium/via/"):
                return _callin_via_get(self, path)
            if not _SHARE.get("lend"):
                return self._err(403, "This computer is not lending its models. Switch sharing on there, in Settings, Your models on every device.")
            if path in ("/v1/models", "/models"):
                ms = _share_models()
                return self._json(200, {"object": "list", "data": [dict(m, object="model", owned_by=_share_computer_name() or "ingenium")
                                                                   for m in ms]})
            if path == "/ingenium/info":
                info = _engine_info()
                return self._json(200, {"app": APP_NAME, "computer": _share_computer_name(), "models": len(_share_models()),
                                        "engine": info.get("version") or "", "available": bool(info.get("available")),
                                        "platform": info.get("platform") or "", "gpu": info.get("gpu") or "",
                                        "loaded": _ENGINE.get("model_id") if _ENGINE.get("state") == "ready" else None})
            return self._err(404, "not found")

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if not self._allowed():
                return
            # R97: a computer that this one cannot reach calls in, and carries its models over that call
            if path == "/ingenium/callin":
                return _callin_poll(self)
            if path.startswith("/ingenium/callin/reply/"):
                return _callin_reply(self, path.rsplit("/", 1)[-1])
            if path.startswith("/ingenium/via/"):
                return _callin_via_post(self, path)
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._err(404, "not found")
            if not _SHARE.get("lend"):
                return self._err(403, "This computer is not lending its models. Switch sharing on there, in Settings, Your models on every device.")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 64 << 20)).decode("utf-8") or "{}")
            except Exception:
                return self._err(400, "the request was not readable")
            _share_answer(body, _HandlerSink(self))
    return H


class _HandlerSink(object):
    """Where an answer goes when the device asked this computer directly."""

    def __init__(self, h):
        self.h = h

    def error(self, code, msg):
        self.h._err(code, msg)

    def head(self, code, ctype, model_id=None):
        self.h.send_response(code)
        self.h._cors()
        self.h.send_header("Content-Type", ctype or "application/json")
        self.h.send_header("Cache-Control", "no-cache")
        if model_id:
            self.h.send_header("X-Ingenium-Model", model_id)
        self.h.end_headers()

    def write(self, chunk):
        self.h.wfile.write(chunk)
        self.h.wfile.flush()

    def end(self):
        pass


def _share_answer(body, sink):
    """R97: one request in the common chat format, answered by this computer's
    own engine and streamed to `sink` (a device that asked directly, or one this
    computer calls). It loads the model asked for when another one is running."""
    want = str(body.pop("model", "") or "")
    ctx = body.pop("ingenium_ctx", None)
    ms = _share_models()
    pick = None
    for m in ms:
        if want and want in (m["id"], m["name"], m["file"], m.get("lineup")):
            pick = m
            break
    if pick is None:
        with _ENGINE_LOCK:
            cur = _ENGINE.get("model_id") if _ENGINE.get("state") == "ready" else None
        pick = next((m for m in ms if m["id"] == cur), None) if not want else None
    if pick is None:
        return sink.error(404, "This computer has no model called %r. The model list shows the ones it has." % want)
    with _SHARE_LOCK:
        _SHARE["busy"] += 1
    try:
        has_pic = any(isinstance(m, dict) and isinstance(m.get("content"), list) and
                      any(isinstance(c, dict) and c.get("type") in ("image_url", "input_image") for c in m["content"])
                      for m in (body.get("messages") or []))
        proj = _mmproj_for(pick["id"]) if has_pic else None
        with _ENGINE_LOCK:
            same = _ENGINE.get("model_id") == pick["id"] and _ENGINE.get("proc") is not None
            args = _ENGINE.get("args")
            if same and proj and not (args and len(args) > 2 and args[2]):
                same = False   # running without its picture reader
        if not same:
            r = _engine_start(pick["id"], int(ctx) if ctx else 8192, proj)
            if not r.get("ok"):
                return sink.error(503, r.get("error") or "the engine could not start")
        try:
            st = _share_wait_ready(pick["id"])
        except Exception as e:
            return sink.error(503, "The model could not be loaded on the sharing computer: %s" % e)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % st["port"], data=data,
                                     headers={"content-type": "application/json", "authorization": "Bearer " + _ENGINE_SECRET})
        try:
            up = _NO_PROXY.open(req, timeout=1800)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:2000]
            sink.head(e.code, "application/json")
            sink.write(txt.encode("utf-8"))
            return sink.end()
        with up:
            sink.head(200, up.headers.get("Content-Type") or "application/json", pick["id"])
            while True:
                chunk = up.read1(65536) if hasattr(up, "read1") else up.read(4096)
                if not chunk:
                    break
                sink.write(chunk)
        sink.end()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass   # the other device stopped the reply
    except Exception as e:
        log_problem("share: a request failed", sys.exc_info())
        try:
            sink.error(500, str(e))
        except Exception:
            pass
    finally:
        with _SHARE_LOCK:
            _SHARE["busy"] -= 1


def _share_start():
    with _SHARE_LOCK:
        if _SHARE["server"] is not None:
            return True
    _share_code()
    srv, err = None, ""

    class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = not sys.platform.startswith("win")

    for port in range(_SHARE_PORT, _SHARE_PORT + 10):
        try:
            srv = _Srv(("0.0.0.0", port), _share_handler())
            break
        except OSError as e:
            err = str(e)
    if srv is None:
        _SHARE["error"] = "No network port near %d was free (%s)." % (_SHARE_PORT, err)
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with _SHARE_LOCK:
        _SHARE.update(server=srv, port=srv.server_address[1], error="")
    return True


def _share_stop():
    with _SHARE_LOCK:
        srv, _SHARE["server"], _SHARE["port"] = _SHARE["server"], None, None
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass


def _share_status():
    with _SHARE_LOCK:
        port, busy, err = _SHARE["port"], _SHARE["busy"], _SHARE["error"]
        now = time.time()
        recent = sorted([(t, ip) for ip, t in _SHARE["last"].items() if now - t < 3600], reverse=True)
    running = port is not None
    cfg = _share_cfg()
    lend = running and bool(cfg.get("on"))
    return {"ok": True, "on": lend, "running": running, "callins_on": bool(cfg.get("callins")), "port": port,
            "code": _share_code(), "addresses": _share_addresses(port) if running else [],
            "computer": _share_computer_name(), "busy": busy, "error": err,
            "recent": [{"address": ip, "minutes_ago": int((now - t) // 60)} for t, ip in recent[:6]],
            "models": len(_share_models()) if lend else 0, "awake": _SHARE.get("awake") is not None,
            "callins": _callin_list(), "callouts": _callout_list(), "firewall": _share_firewall()}


def _share_set(on):
    c = _share_cfg()
    c["on"] = bool(on)
    _share_cfg_save(c)
    _share_apply()
    return _share_status()


def _share_apply():
    """R97: the gateway runs while this computer lends its models, or while it
    lets the person's other computers call it (to lend it theirs)."""
    c = _share_cfg()
    _SHARE["lend"] = bool(c.get("on"))
    if c.get("on") or c.get("callins"):
        _share_start()
    else:
        _share_stop()
    # only lending keeps the computer awake; a laptop that borrows sleeps as it likes
    _share_keep_awake(bool(c.get("on")) and _SHARE["server"] is not None)
    _callout_apply()


def _share_renew_code():
    c = _share_cfg()
    c["code"] = _share_new_code()
    _share_cfg_save(c)
    with _SHARE_LOCK:
        _SHARE["fails"].clear()
    return _share_status()


def _share_autostart():
    """Sharing that was on when the app closed is on again when it opens, and so
    are the calls this computer makes to lend its models to another."""
    try:
        _share_apply()
    except Exception:
        log_problem("share: could not start at launch", sys.exc_info())


# ---------------------------------------------------------------------------
# R97: when one computer cannot reach the other, the other one calls.
#
# On the owner's network the Mac mini could use the MacBook's models but the
# MacBook could never reach the Mac mini. Either side of that is a gate macOS
# keeps: the lending Mac's firewall refuses calls that come in, or the asking
# Mac is not allowed to start calls to the home network (Local Network, in
# Privacy & Security). Both leave the other direction open, so the lending
# computer now calls the asking one and keeps that call open:
#
# * The asking computer lets its other computers call it (Settings, Your models
#   on every device). Its gateway then runs without lending anything itself.
# * The lending computer is told the asking one's address and pairing code. It
#   calls in, says which models it has, and waits; when the asking computer's
#   chat sends a request, it travels back down that call, is answered by the
#   lending computer's own engine, and the reply streams back word by word.
# * The asking computer's page talks only to its own gateway on this computer
#   (127.0.0.1), which needs no permission at all.
# ---------------------------------------------------------------------------
import queue as _queue

_CALLIN = {}        # lender id -> {"computer", "models", "last", "jobs": Queue, "addr"}
_CALLIN_JOBS = {}   # job id -> {"out": Queue, "lid", "made"}
_CALLIN_LOCK = threading.Lock()
_CALLOUT = {}       # address -> {"addr", "code", "computer", "state", "error", "last", "stop": Event}
_CALLOUT_LOCK = threading.Lock()


def _share_lender_id():
    c = _share_cfg()
    lid = str(c.get("id") or "")
    if not re.fullmatch(r"[a-z0-9]{12}", lid):
        import secrets
        lid = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(12))
        c["id"] = lid
        _share_cfg_save(c)
    return lid


def _callin_list():
    now = time.time()
    with _CALLIN_LOCK:
        return [{"id": lid, "computer": c.get("computer") or "", "models": c.get("models") or [],
                 "address": c.get("addr") or "", "seconds_ago": int(now - c.get("last", 0))}
                for lid, c in _CALLIN.items() if now - c.get("last", 0) < 120]


def _callin_set(on):
    c = _share_cfg()
    c["callins"] = bool(on)
    _share_cfg_save(c)
    _share_apply()
    return _share_status()


def _callin_poll(h):
    """A lending computer calls in: it says what it has, then waits (up to 25
    seconds) for a request from this computer's chat."""
    try:
        n = int(h.headers.get("Content-Length") or 0)
        info = json.loads(h.rfile.read(min(n, 4 << 20)).decode("utf-8") or "{}")
    except Exception:
        return h._err(400, "the call was not readable")
    lid = str(info.get("id") or "")
    if not re.fullmatch(r"[a-z0-9]{12}", lid):
        return h._err(400, "the call carried no name")
    models = [m for m in (info.get("models") or []) if isinstance(m, dict) and m.get("name")][:200]
    with _CALLIN_LOCK:
        c = _CALLIN.get(lid)
        if c is None:
            c = _CALLIN[lid] = {"jobs": _queue.Queue()}
        c.update(computer=str(info.get("computer") or "")[:80], models=models, last=time.time(), addr=h.client_address[0])
        jobs = c["jobs"]
    try:
        job = jobs.get(timeout=25)
    except _queue.Empty:
        job = None
    with _CALLIN_LOCK:
        if lid in _CALLIN:
            _CALLIN[lid]["last"] = time.time()
    return h._json(200, {"job": job})


def _callin_read_chunked(rfile):
    """The body of a reply sent in chunks, piece by piece, as it arrives."""
    while True:
        line = rfile.readline(1024)
        if not line:
            return
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            rfile.readline(1024)
            return
        data = rfile.read(size)
        rfile.readline(8)
        yield data


def _callin_reply(h, jid):
    with _CALLIN_LOCK:
        job = _CALLIN_JOBS.get(jid)
    if job is None:
        return h._err(404, "that request is no longer waiting")
    out = job["out"]
    try:
        code = int(h.headers.get("X-Ingenium-Status") or 200)
    except Exception:
        code = 200
    out.put(("head", code, h.headers.get("Content-Type") or "application/json", h.headers.get("X-Ingenium-Model") or ""))
    try:
        if (h.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            for piece in _callin_read_chunked(h.rfile):
                out.put(("data", piece))
        else:
            n = int(h.headers.get("Content-Length") or 0)
            if n:
                out.put(("data", h.rfile.read(n)))
    except Exception:
        log_problem("call-in: a reply broke off", sys.exc_info())
    finally:
        out.put(("end",))
    return h._json(200, {"ok": True})


def _callin_via_parse(path):
    m = re.match(r"^/ingenium/via/([a-z0-9]{12})(/.*)?$", path)
    return (m.group(1), m.group(2) or "") if m else (None, None)


def _callin_via_get(h, path):
    lid, rest = _callin_via_parse(path)
    with _CALLIN_LOCK:
        c = _CALLIN.get(lid) if lid else None
        fresh = c is not None and time.time() - c.get("last", 0) < 120
        models = list(c.get("models") or []) if c else []
        who = (c or {}).get("computer") or "the other computer"
    if not fresh:
        return h._err(503, "%s has not called in for a while. Check that Ingenium is open there." % who)
    if rest.rstrip("/") in ("/v1/models", "/models"):
        return h._json(200, {"object": "list", "data": [dict(m, object="model", owned_by=who) for m in models]})
    return h._err(404, "not found")


def _callin_via_post(h, path):
    """This computer's chat asks a computer that called in: the request goes
    down that call and the answer streams back."""
    lid, rest = _callin_via_parse(path)
    if rest.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
        return h._err(404, "not found")
    with _CALLIN_LOCK:
        c = _CALLIN.get(lid) if lid else None
        fresh = c is not None and time.time() - c.get("last", 0) < 120
        who = (c or {}).get("computer") or "the other computer"
    if not fresh:
        return h._err(503, "%s has not called in for a while. Check that Ingenium is open there and still lends to this computer." % who)
    try:
        n = int(h.headers.get("Content-Length") or 0)
        body = json.loads(h.rfile.read(min(n, 64 << 20)).decode("utf-8") or "{}")
    except Exception:
        return h._err(400, "the request was not readable")
    import secrets
    jid = secrets.token_hex(8)
    out = _queue.Queue()
    with _CALLIN_LOCK:
        _CALLIN_JOBS[jid] = {"out": out, "lid": lid, "made": time.time()}
    c["jobs"].put({"id": jid, "body": body})
    try:
        try:
            first = out.get(timeout=45)
        except _queue.Empty:
            return h._err(504, "%s did not pick the request up. Check that Ingenium is open there." % who)
        if first[0] != "head":
            return h._err(502, "%s sent no answer." % who)
        h.send_response(first[1])
        h._cors()
        h.send_header("Content-Type", first[2])
        h.send_header("Cache-Control", "no-cache")
        if first[3]:
            h.send_header("X-Ingenium-Model", first[3])
        h.end_headers()
        while True:
            try:
                item = out.get(timeout=1800)
            except _queue.Empty:
                break
            if item[0] == "end":
                break
            h.wfile.write(item[1])
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        with _CALLIN_LOCK:
            _CALLIN_JOBS.pop(jid, None)


class _CallSink(object):
    """Where an answer goes when this computer lends by calling: back up the
    call, in chunks, to the computer that asked."""

    def __init__(self, addr, code, jid):
        self.addr, self.code, self.jid = addr, code, jid
        self.conn = None

    def _open(self, code, ctype, model_id=None):
        import http.client
        host, _, port = self.addr.rpartition(":")
        self.conn = http.client.HTTPConnection(host, int(port), timeout=1800)
        self.conn.putrequest("POST", "/ingenium/callin/reply/" + self.jid)
        self.conn.putheader("Authorization", "Bearer " + self.code)
        self.conn.putheader("Transfer-Encoding", "chunked")
        self.conn.putheader("X-Ingenium-Status", str(code))
        self.conn.putheader("Content-Type", ctype or "application/json")
        if model_id:
            self.conn.putheader("X-Ingenium-Model", model_id)
        self.conn.endheaders()

    def head(self, code, ctype, model_id=None):
        self._open(code, ctype, model_id)

    def write(self, chunk):
        if chunk:
            self.conn.send(("%x\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")

    def end(self):
        if self.conn is not None:
            try:
                self.conn.send(b"0\r\n\r\n")
                self.conn.getresponse().read()
            finally:
                self.conn.close()
                self.conn = None

    def error(self, code, msg):
        self.head(code, "application/json")
        self.write(json.dumps({"error": {"message": msg, "code": code}}).encode("utf-8"))
        self.end()


def _callout_job(entry, job):
    try:
        _share_answer(dict(job.get("body") or {}), _CallSink(entry["addr"], entry["code"], str(job.get("id") or "")))
    except Exception:
        log_problem("call-out: a request could not be answered", sys.exc_info())


def _callout_loop(entry):
    """Calls the asking computer, again and again, for as long as this computer
    lends to it. A missed call is tried again after a short wait."""
    wait = 3
    while not entry["stop"].is_set():
        try:
            if not _SHARE.get("lend"):
                entry.update(state="paused", error="Sharing is off on this computer.")
                entry["stop"].wait(5)
                continue
            body = json.dumps({"id": _share_lender_id(), "computer": _share_computer_name(),
                               "models": _share_models()}).encode("utf-8")
            req = urllib.request.Request("http://%s/ingenium/callin" % entry["addr"], data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + entry["code"]})
            with _NO_PROXY.open(req, timeout=40) as r:
                got = json.loads(r.read().decode("utf-8") or "{}")
            entry.update(state="linked", error="", last=time.time())
            wait = 3
            job = got.get("job")
            if job:
                threading.Thread(target=_callout_job, args=(entry, job), daemon=True).start()
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = (json.loads(e.read().decode("utf-8", "replace")).get("error") or {}).get("message") or ""
            except Exception:
                pass
            entry.update(state="refused", error=msg or ("It answered %d." % e.code))
            entry["stop"].wait(60 if e.code in (401, 429) else 15)
        except Exception as e:
            entry.update(state="waiting", error=_relay_reason(e))
            entry["stop"].wait(wait)
            wait = min(wait * 2, 30)


def _callout_apply():
    """Starts the calls kept in the settings, and stops those no longer kept."""
    want = {}
    for x in _share_cfg().get("callouts") or []:
        if isinstance(x, dict) and x.get("addr") and x.get("code"):
            want[str(x["addr"])] = x
    with _CALLOUT_LOCK:
        for addr in list(_CALLOUT):
            if addr not in want or _CALLOUT[addr]["code"] != want[addr]["code"]:
                _CALLOUT.pop(addr)["stop"].set()
        for addr, x in want.items():
            if addr in _CALLOUT:
                continue
            e = {"addr": addr, "code": str(x["code"]), "computer": str(x.get("computer") or ""), "state": "starting",
                 "error": "", "last": 0.0, "stop": threading.Event()}
            _CALLOUT[addr] = e
            threading.Thread(target=_callout_loop, args=(e,), daemon=True).start()


def _callout_list():
    now = time.time()
    with _CALLOUT_LOCK:
        return [{"address": e["addr"], "computer": e["computer"], "state": e["state"], "error": e["error"],
                 "seconds_ago": int(now - e["last"]) if e["last"] else None} for e in _CALLOUT.values()]


def _callout_add(addr, code):
    """Lend to a computer that cannot reach this one: check it answers, then keep calling it."""
    a = re.sub(r"^https?://", "", str(addr or "").strip()).split("/", 1)[0]
    if not a:
        return {"ok": False, "error": "Type the address that the other computer shows."}
    if not re.search(r":\d+$", a):
        a += ":%d" % _SHARE_PORT
    code = str(code or "").upper().strip()
    if len(_share_norm(code)) != 8:
        return {"ok": False, "error": "Type the eight-letter pairing code that the other computer shows."}
    try:
        with _NO_PROXY.open("http://%s/ingenium/hello" % a, timeout=8) as r:
            hello = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        return {"ok": False, "error": _relay_reason(e)}
    if hello.get("app") != "Ingenium":
        return {"ok": False, "error": "Something answered at that address, but it is not Ingenium."}
    if not hello.get("callins"):
        return {"ok": False, "error": "That copy of Ingenium is older and cannot be called. Update it first."}
    try:
        req = urllib.request.Request("http://%s/ingenium/callins" % a, headers={"Authorization": "Bearer " + code})
        with _NO_PROXY.open(req, timeout=8) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "error": "The pairing code is not right. It is the code the other computer shows."}
        return {"ok": False, "error": "The other computer answered %d." % e.code}
    except Exception as e:
        return {"ok": False, "error": _relay_reason(e)}
    c = _share_cfg()
    keep = [x for x in (c.get("callouts") or []) if isinstance(x, dict) and x.get("addr") != a]
    keep.append({"addr": a, "code": code, "computer": str(hello.get("computer") or "")})
    c["callouts"] = keep
    if not c.get("on"):
        c["on"] = True   # lending to it is sharing
    _share_cfg_save(c)
    _share_apply()
    return dict(_share_status(), ok=True)


def _callout_remove(addr):
    c = _share_cfg()
    c["callouts"] = [x for x in (c.get("callouts") or []) if isinstance(x, dict) and x.get("addr") != addr]
    _share_cfg_save(c)
    _callout_apply()
    return _share_status()


# ---- R97: the Mac's own firewall, which can refuse calls that come in ----
_FW = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def _app_exe_for_firewall():
    exe = os.path.realpath(sys.executable or "")
    return exe if os.path.isfile(exe) else ""


_FW_SEEN = {"at": 0.0, "v": None}


def _share_firewall(fresh=False):
    """Whether macOS's firewall lets other devices reach this app. None
    elsewhere, or when it cannot be read. Read at most every half minute."""
    if sys.platform != "darwin" or not os.path.exists(_FW):
        return None
    if not fresh and time.time() - _FW_SEEN["at"] < 30:
        return _FW_SEEN["v"]
    _FW_SEEN.update(at=time.time(), v=_share_firewall_read())
    return _FW_SEEN["v"]


def _share_firewall_read():
    try:
        def ask(*a):
            r = subprocess.run([_FW] + list(a), capture_output=True, text=True, timeout=6)
            return ((r.stdout or "") + (r.stderr or "")).strip()
        g = ask("--getglobalstate").lower()
        on = ("enabled" in g) or ("state = 1" in g) or ("state = 2" in g)
        if not on:
            return {"on": False, "blocked": False, "block_all": False}
        block_all = "enabled" in ask("--getblockall").lower()
        exe = _app_exe_for_firewall()
        app = ask("--getappblocked", exe).lower() if exe else ""
        # "... is permitted", "... is blocked", or "... is not part of the firewall" (then macOS asks once, and a
        # missed or refused question leaves calls from outside refused)
        blocked = block_all or ("is blocked" in app) or ("not part of the firewall" in app)
        return {"on": True, "blocked": bool(blocked), "block_all": block_all, "app": app[:200]}
    except Exception:
        return None


def _share_firewall_allow():
    """Asks for the Mac's administrator password once, then lets Ingenium
    accept calls from the person's other devices."""
    if sys.platform != "darwin" or not os.path.exists(_FW):
        return {"ok": False, "error": "There is no Mac firewall to change here."}
    exe = _app_exe_for_firewall()
    if not exe:
        return {"ok": False, "error": "The app's own program could not be found."}
    q = exe.replace("\\", "\\\\").replace('"', '\\"').replace("'", "'\\''")
    cmd = "%s --add '%s' ; %s --unblockapp '%s'" % (_FW, q, _FW, q)
    try:
        r = subprocess.run(["osascript", "-e", 'do shell script "%s" with administrator privileges' % cmd.replace('"', '\\"')],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "").strip() or "The firewall was not changed."}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    _share_firewall(fresh=True)
    return dict(_share_status(), ok=True)


def _mmproj_for(mid):
    """The vision projector kept for a model (its lineup entry with -mmproj), when it is on disk."""
    try:
        own = _model_path_for(str(mid) + "-mmproj")
        if own and os.path.isfile(own):
            return str(mid) + "-mmproj"
        _load_models_index()
        if mid.startswith("builtin-") and mid not in _BUILTIN_LINEUP:
            _builtin_models()
        key = _BUILTIN_LINEUP.get(mid) or (_MODEL_META.get(mid) or {}).get("lineup") or ""
        if not key or key.endswith("-mmproj") or key.endswith("-dspark"):
            return None
        for pmid, meta in list(_MODEL_META.items()):
            if meta.get("lineup") == key + "-mmproj":
                p = _MODEL_PATHS.get(pmid)
                if p and os.path.isfile(p):
                    return pmid
    except Exception:
        log_problem("engine: looking for a projector failed", sys.exc_info())
    return None


def _selftest_speedup(b, dpath):
    """Ask the built-in model one coding question without its speed-up file,
    with it, then without it again; say how fast each was and how many
    guesses were taken. The faster of the two runs without the file is the
    baseline, so a machine that warms up as it goes does not flatter the
    file."""
    out = {"file": os.path.basename(dpath)}
    key = b.get("lineup") or ""
    if not key:
        return {"error": "the built-in model names no lineup entry"}
    dmid = "selftest-drafter"
    q = "Write a Python function that reverses the words of a sentence, with a docstring and two doctests."

    def ask(label):
        r = _engine_start(b["id"], 4096)
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        t1 = time.time()
        st = {}
        while time.time() - t1 < 600:
            st = _engine_status()
            if st.get("state") != "loading":
                break
            time.sleep(0.5)
        if st.get("state") != "ready":
            raise RuntimeError(st.get("error") or ("engine state: %s" % st.get("state")))
        body = json.dumps({"messages": [{"role": "user", "content": q}], "max_tokens": 300, "temperature": 0,
                           "chat_template_kwargs": {"enable_thinking": False}}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % st["port"], data=body,
                                     headers={"content-type": "application/json", "authorization": "Bearer " + _ENGINE_SECRET})
        with _NO_PROXY.open(req, timeout=600) as rr:
            ans = json.loads(rr.read().decode("utf-8"))
        tm = ans.get("timings") or {}
        out[label] = {"tokens": tm.get("predicted_n"), "per_second": tm.get("predicted_per_second"),
                      "drafted": tm.get("draft_n"), "accepted": tm.get("draft_n_accepted"),
                      "draft_in_use": bool(st.get("draft")), "note": st.get("note") or "",
                      "text": str(ans["choices"][0]["message"].get("content") or "")[:80]}
    try:
        ask("without")
        _MODEL_PATHS[dmid] = os.path.abspath(dpath)
        _MODEL_META[dmid] = {"lineup": key + "-dspark", "name": "speed-up (test)", "file": os.path.basename(dpath)}
        ask("with")
        _MODEL_META.pop(dmid, None)
        _MODEL_PATHS.pop(dmid, None)
        ask("again")
        w, wo, wa = out["with"], out["without"], out["again"]
        out["used"] = bool(w.get("draft_in_use") and (w.get("drafted") or 0) > 0)
        out["same_reply"] = w.get("text") == wo.get("text")
        base = max(wo.get("per_second") or 0, wa.get("per_second") or 0)
        if wo.get("per_second") and wa.get("per_second"):
            # how far two runs without the file differ: the test's own noise
            out["noise_percent"] = round(abs(wa["per_second"] / wo["per_second"] - 1) * 100)
        if w.get("per_second") and base:
            out["baseline_per_second"] = base
            out["faster_by_percent"] = round((w["per_second"] / base - 1) * 100)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        _MODEL_META.pop(dmid, None)
        _MODEL_PATHS.pop(dmid, None)
    return out


def _selftest_share(b):
    """R74: the gateway that lends this computer's models: reached at its home
    network address, it must refuse a wrong pairing code, list the models,
    and stream an answer from the model asked for."""
    out = {"ok": False}
    try:
        _SHARE["lend"] = True   # R97: the gateway lends only when lending is on; this test lends
        if not _share_start():
            out["error"] = _SHARE["error"] or "the gateway could not start"
            return out
        port, code = _SHARE["port"], _share_code()
        ips = [a for a in _share_addresses(port) if re.match(r"\d+\.\d+\.\d+\.\d+:", a)]

        def call(base, path, body=None, key=None):
            h = {"content-type": "application/json"}
            if key:
                h["authorization"] = "Bearer " + key
            data = json.dumps(body).encode("utf-8") if body is not None else None
            return _NO_PROXY.open(urllib.request.Request("http://%s%s" % (base, path), data=data, headers=h), timeout=600)
        base = None
        for t in ips[:1] + ["127.0.0.1:%d" % port]:
            try:
                with call(t, "/ingenium/hello") as r:
                    if r.status == 200:
                        base = t
                        break
            except Exception as e:
                out.setdefault("tried", []).append("%s: %s" % (t, e))
        if not base:
            out["error"] = "the gateway did not answer"
            return out
        out["address"] = base
        out["over_network"] = not base.startswith("127.")
        try:
            call(base, "/v1/models", key="WRNG-CODE").close()
            out["refuses_wrong_code"] = False
        except urllib.error.HTTPError as e:
            out["refuses_wrong_code"] = e.code == 401
        with call(base, "/v1/models", key=code.lower()) as r:
            models = json.loads(r.read().decode("utf-8")).get("data") or []
        out["models"] = [m.get("name") for m in models]
        t0, first, text = time.time(), None, ""
        body = {"model": b["id"], "stream": True, "max_tokens": 32, "temperature": 0,
                "messages": [{"role": "user", "content": "What is the capital of France? Answer in one word."}],
                "chat_template_kwargs": {"enable_thinking": False}}
        with call(base, "/v1/chat/completions", body, key=code) as r:
            buf = b""
            while True:
                chunk = r.read1(4096) if hasattr(r, "read1") else r.read(1024)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:") or line[5:].strip() in (b"", b"[DONE]"):
                        continue
                    try:
                        d = json.loads(line[5:].decode("utf-8"))
                    except Exception:
                        continue
                    delta = ((d.get("choices") or [{}])[0] or {}).get("delta") or {}
                    if delta.get("content"):
                        if first is None:
                            first = time.time() - t0
                        text += delta["content"]
        out["reply"] = text.strip()[:100]
        out["first_word_seconds"] = round(first or 0, 2)
        out["seconds"] = round(time.time() - t0, 2)
        out["ok"] = bool(out.get("refuses_wrong_code") and "paris" in text.lower() and b["name"] in out["models"])
        if not out["ok"]:
            out["error"] = ("a wrong code was not refused" if not out.get("refuses_wrong_code") else
                            "the model list lacks %s" % b["name"] if b["name"] not in out["models"] else
                            "the answer did not name Paris")
    except Exception as e:
        out.update(ok=False, error="%s: %s" % (type(e).__name__, e))
    finally:
        _share_stop()
    return out


def _selftest(argv):
    """For the cloud build, with no window: start the engine this app carries
    with a model, ask it one question, check that the engine refuses a request
    without the session key, write what happened to a file, and exit 0 only
    when all of it worked."""
    def arg(name):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    model, out = arg("--selftest-model"), arg("--selftest-out")
    res = {"ok": False, "app": APP_NAME}
    t0 = time.time()
    try:
        info = _engine_info()
        res["engine"] = dict((k, info.get(k)) for k in ("available", "platform", "version", "threads"))
        if not info.get("available"):
            raise RuntimeError("this app carries no engine")
        if model == "none":
            # an app built without a model: the engine must still unpack and run
            d = _engine_dir()
            r0 = subprocess.run([_engine_exe(d), "--version"], timeout=120, **_engine_popen_kw(d))
            res["version_output"] = r0.stdout.decode("utf-8", "replace").strip()[-300:]
            res["ok"] = r0.returncode == 0 and "version" in res["version_output"]
            res["locked"] = None
            if not res["ok"]:
                res["error"] = "the engine did not run (code %s)" % r0.returncode
            raise StopIteration
        if model == "builtin":
            b = _builtin_model()
            if not b:
                raise RuntimeError("this app carries no built-in model")
            res["builtin"] = b
            mid = b["id"]
        else:
            mid = "selftest"
            _MODEL_PATHS[mid] = os.path.abspath(model)
        for _ in range(120):   # the device list, for the record
            with _ENGINE_LOCK:
                if not _ENGINE_DEVICES["busy"]:
                    break
            time.sleep(0.5)
        res["devices"] = _ENGINE_DEVICES["list"]
        r = _engine_start(mid, 4096)
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        st = {}
        while time.time() - t0 < 900:
            st = _engine_status()
            if st.get("state") != "loading":
                break
            time.sleep(0.5)
        if st.get("state") != "ready":
            raise RuntimeError(st.get("error") or ("engine state: %s" % st.get("state")))
        res["load_seconds"] = round(time.time() - t0, 1)
        res["note"] = st.get("note") or ""
        url = "http://127.0.0.1:%d/v1/chat/completions" % st["port"]
        body = json.dumps({"messages": [{"role": "user", "content": "What is the capital of France? Answer in one word."}],
                           "max_tokens": 400, "temperature": 0,
                           "chat_template_kwargs": {"enable_thinking": False}}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json",
                                                              "authorization": "Bearer " + _ENGINE_SECRET})
        with _NO_PROXY.open(req, timeout=600) as r2:
            ans = json.loads(r2.read().decode("utf-8"))
        text = str(ans["choices"][0]["message"].get("content") or "")
        tm = ans.get("timings") or {}
        res["reply"] = text.strip()[:300]
        res["timings"] = dict((k, tm.get(k)) for k in ("prompt_n", "predicted_n", "predicted_per_second"))
        try:
            _NO_PROXY.open(urllib.request.Request(url, data=body, headers={"content-type": "application/json"}), timeout=60)
            res["locked"] = False
        except urllib.error.HTTPError as e:
            res["locked"] = e.code == 401
        res["ok"] = ("paris" in text.lower()) and res["locked"]
        if not res["ok"]:
            res["error"] = "the reply did not name Paris" if res["locked"] else "the engine answered without the session key"
        # R70: an app with a second built-in model (Scriba) has it think: its
        # thoughts are what the page's thinking bar shows
        many = _builtin_models() if model == "builtin" else []
        if res["ok"] and len(many) > 1:
            sc = many[1]
            r = _engine_start(sc["id"], 4096)
            if not r.get("ok"):
                raise RuntimeError(r.get("error"))
            t1 = time.time()
            st = {}
            while time.time() - t1 < 600:
                st = _engine_status()
                if st.get("state") != "loading":
                    break
                time.sleep(0.5)
            if st.get("state") != "ready":
                raise RuntimeError(st.get("error") or ("engine state: %s" % st.get("state")))
            url = "http://127.0.0.1:%d/v1/chat/completions" % st["port"]
            body = json.dumps({"messages": [{"role": "user", "content": "Is 91 a prime number?"}], "max_tokens": 48,
                               "temperature": 0, "chat_template_kwargs": {"enable_thinking": True}}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"content-type": "application/json",
                                                                  "authorization": "Bearer " + _ENGINE_SECRET})
            with _NO_PROXY.open(req, timeout=600) as r3:
                msg = json.loads(r3.read().decode("utf-8"))["choices"][0]["message"]
            think = str(msg.get("reasoning_content") or "")
            if not think and "<think>" in str(msg.get("content") or ""):
                think = str(msg.get("content")).split("<think>", 1)[1]
            res["think"] = {"model": sc["name"], "chars": len(think), "text": think.strip()[:120]}
            if not think.strip():
                res["ok"] = False
                res["error"] = "the thinking model (%s) gave no thinking" % sc["name"]
        # R73: the first built-in model's speed-up file, when the build hands
        # one over: the same question with and without it. For the record
        # only: an app whose engine cannot use the file loads without it.
        dpath = arg("--selftest-drafter")
        if res["ok"] and model == "builtin" and dpath and os.path.isfile(dpath):
            res["speedup"] = _selftest_speedup(res["builtin"], dpath)
        # R74: lending the models to the person's other devices
        if res["ok"] and model == "builtin" and "--selftest-share" in argv:
            res["share"] = _selftest_share(res["builtin"])
            if not res["share"].get("ok"):
                res["ok"] = False
                res["error"] = "sharing with other devices: %s" % res["share"].get("error")
    except StopIteration:
        pass
    except Exception as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        with _ENGINE_LOCK:
            res["log"] = list(_ENGINE["log"][-40:])
        _engine_stop()
    res["seconds"] = round(time.time() - t0, 1)
    text = json.dumps(res, indent=1)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    try:
        print(text)
    except Exception:
        pass
    return 0 if res["ok"] else 1


_APP_HTML_PATH = None


def _read_app_page(offset=0, size=4194304):
    """The app's own page file, in pieces (base64), so the page can put itself
    into the build kits it makes."""
    p = _APP_HTML_PATH
    try:
        total = os.path.getsize(p)
        with open(p, "rb") as fh:
            fh.seek(max(0, int(offset)))
            data = fh.read(max(0, min(int(size), 8388608)))
        return {"b64": base64.b64encode(data).decode("ascii"), "total": total}
    except Exception:
        log_problem("read_app_page failed", sys.exc_info())
        return {"b64": "", "total": 0}


def _delete_model_file(mid):
    p = _MODEL_PATHS.pop(mid, None) or _model_path_for(mid)
    _MODEL_PATHS.pop(mid, None)
    _MODEL_META.pop(mid, None)
    was_external = mid in _MODEL_EXTERNAL
    _MODEL_EXTERNAL.discard(mid)
    _save_models_index()
    if not p or was_external:
        return True
    try:
        d = _models_dir()
        # only ever delete files that live inside our own models folder
        if d and os.path.commonpath([os.path.realpath(p), str(d)]) == str(d):
            os.remove(p)
    except Exception:
        pass
    return True


def _ollama_library():
    """Read Ollama's model library on this machine: manifests map a name:tag to
    a weights blob. Returns [{name, path, size}]. Read-only; nothing is moved,
    copied or changed."""
    home = str(pathlib.Path.home())
    base = os.path.join(home, ".ollama", "models")
    man_root = os.path.join(base, "manifests")
    blob_root = os.path.join(base, "blobs")
    out = []
    if not os.path.isdir(man_root):
        return out
    for root, _dirs, files in os.walk(man_root):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    man = json.load(fh)
            except Exception:
                continue
            layers = man.get("layers") or []
            weights = None
            for L in layers:
                dg = str(L.get("digest", ""))
                mt = str(L.get("mediaType", ""))
                if dg.startswith("sha256:") and mt.endswith("image.model"):
                    weights = L
                    break
            if not weights:
                continue
            hexd = weights["digest"].split(":", 1)[1]
            bpath = os.path.join(blob_root, "sha256-" + hexd)
            if not os.path.isfile(bpath):
                continue
            rel = os.path.relpath(p, man_root).replace(os.sep, "/")
            parts = rel.split("/")
            body = parts[2:] if len(parts) > 3 else parts[1:]
            if len(body) >= 2:
                disp = "/".join(body[:-1]) + ":" + body[-1]
            else:
                disp = body[-1] if body else fn
            try:
                size = int(weights.get("size") or os.path.getsize(bpath))
            except Exception:
                size = os.path.getsize(bpath)
            out.append({"name": disp, "path": bpath, "size": size})
    out.sort(key=lambda r: r["name"])
    return out


_load_models_index()


# R61: the desk itself lives on disk too. The web view's storage turned out to
# be a sieve on some profiles (ephemeral store, new origin per launch), and the
# desk "reset every time the app opened" was exactly that. From now on the page
# writes its whole state through to the shell beside the app's own data, and
# reads it back when its own view of the world comes up empty or older. The
# web view's storage can never again be the only place the desk exists.
def _desk_path():
    d = _models_dir()
    if d is None:
        return None
    return d.parent / "desk.json"


def _load_state():
    try:
        p = _desk_path()
        if p and p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _save_state(raw):
    p = _desk_path()
    if p is None:
        return False
    txt = raw if isinstance(raw, str) else str(raw)
    if len(txt) > 64 * 1024 * 1024:
        return False
    try:
        _desk_before_save(txt)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.replace(tmp, str(p))
        return True
    except Exception:
        log_problem("save_state failed", sys.exc_info())
        return False


# ---------------------------------------------------------------------------
# R96: the desk keeps its history (see stage 8 of the shell patches).
# ---------------------------------------------------------------------------
def _desk_backup_dir():
    p = _desk_path()
    if p is None:
        return None
    d = p.parent / "desk-backups"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return d


def _desk_counts(txt):
    """Conversations and messages in a desk, counted without parsing a large file."""
    return len(re.findall(r'"messages"\s*:', txt)), len(re.findall(r'"role"\s*:\s*"(?:me|bot)"', txt))


def _desk_prune(d, keep=40):
    try:
        files = sorted(d.glob("desk-*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _desk_keep(reason, txt=None):
    d = _desk_backup_dir()
    p = _desk_path()
    if d is None or p is None:
        return ""
    try:
        if txt is None:
            if not p.is_file():
                return ""
            txt = p.read_text(encoding="utf-8")
        if len(txt) < 60:
            return ""
        now = time.time()
        name = "desk-%s%03d-%s.json" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), int(now * 1000) % 1000,
                                       re.sub(r"[^a-z-]", "", reason)[:24] or "kept")
        while (d / name).exists():
            name = name[:-5] + "x.json"
        tmp = d / (name + ".part")
        tmp.write_text(txt, encoding="utf-8")
        os.replace(str(tmp), str(d / name))
        _desk_prune(d)
        return name
    except Exception:
        log_problem("desk: a copy could not be kept", sys.exc_info())
        return ""


def _desk_before_save(new_txt):
    """Called just before desk.json is replaced: keep the day's first desk, and a desk about to shrink sharply."""
    p = _desk_path()
    if p is None or not p.is_file():
        return
    try:
        old = p.read_text(encoding="utf-8")
    except Exception:
        return
    if len(old) < 60:
        return
    d = _desk_backup_dir()
    if d is None:
        return
    try:
        today = time.strftime("%Y%m%d")
        if not list(d.glob("desk-%s-*-daily.json" % today)):
            _desk_keep("daily", old)
        oc, om = _desk_counts(old)
        nc, nm = _desk_counts(new_txt)
        if om > 0 and (nm * 2 < om or len(new_txt) * 5 < len(old) * 2):
            _desk_keep("before-shrink", old)
    except Exception:
        log_problem("desk: the history check failed", sys.exc_info())


def _desk_titles(txt):
    try:
        if len(txt) > 40 * 1024 * 1024:
            return []
        st = json.loads(txt)
        chats = st.get("chats") or []
        out = []
        for c in chats[-8:][::-1]:
            t = str((c or {}).get("title") or "").strip()
            if t:
                out.append(t[:80])
        return out[:6]
    except Exception:
        return []


_DESK_KEYS = ("ingenium_v4", "the_foundry_v4", "the_foundry_v3", "the_foundry_v2")


def _desk_webkit_files():
    """On a Mac, the web view's own stores that may still hold an earlier desk."""
    out = []
    if sys.platform != "darwin":
        return out
    roots = [pathlib.Path.home() / "Library" / "WebKit"]
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(root)):
            if dirpath.count(os.sep) - str(root).count(os.sep) > 9:
                dirnames[:] = []
                continue
            for f in filenames:
                if f == "localstorage.sqlite3" or f.endswith(".localstorage"):
                    out.append(os.path.join(dirpath, f))
            if len(out) > 400:
                return out
    return out


def _desk_webkit_read(path):
    import sqlite3
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True, timeout=2)
    except Exception:
        return None
    try:
        for k in _DESK_KEYS:
            try:
                row = con.execute("SELECT value FROM ItemTable WHERE key=?", (k,)).fetchone()
            except Exception:
                return None
            if not row:
                continue
            v = row[0]
            if isinstance(v, (bytes, bytearray)):
                txt = None
                for enc in ("utf-16-le", "utf-8"):
                    try:
                        t = bytes(v).decode(enc)
                        if t.lstrip().startswith("{"):
                            txt = t
                            break
                    except Exception:
                        continue
                if txt:
                    return txt
            elif isinstance(v, str) and v.lstrip().startswith("{"):
                return v
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def _desk_when(txt, fallback):
    m = re.search(r'"__deskAt"\s*:\s*(\d{3,14})', txt)
    return int(m.group(1)) if m else fallback


def _desk_list():
    """Earlier desks on this computer: the kept copies and the web view's earlier stores, newest first."""
    out = []
    d = _desk_backup_dir()
    if d is not None:
        for f in d.glob("desk-*.json"):
            try:
                txt = f.read_text(encoding="utf-8")
                c, m = _desk_counts(txt)
                out.append({"id": "b:" + f.name, "kind": "kept", "reason": f.name.split("-", 3)[-1][:-5],
                            "when": _desk_when(txt, int(f.stat().st_mtime * 1000)), "size": len(txt),
                            "chats": c, "messages": m, "titles": _desk_titles(txt)})
            except Exception:
                pass
    for path in _desk_webkit_files():
        txt = _desk_webkit_read(path)
        if not txt or len(txt) < 60:
            continue
        c, m = _desk_counts(txt)
        try:
            mt = int(os.stat(path).st_mtime * 1000)
        except Exception:
            mt = 0
        out.append({"id": "w:" + path, "kind": "web view", "reason": "an earlier address", "when": _desk_when(txt, mt),
                    "size": len(txt), "chats": c, "messages": m, "titles": _desk_titles(txt)})
    out.sort(key=lambda x: x.get("when", 0), reverse=True)
    return out[:60]


def _desk_read(did):
    did = str(did or "")
    try:
        if did.startswith("b:"):
            d = _desk_backup_dir()
            name = os.path.basename(did[2:])
            if d is None or not re.fullmatch(r"desk-[0-9A-Za-z_.-]+\.json", name):
                return ""
            return (d / name).read_text(encoding="utf-8")
        if did.startswith("w:"):
            path = did[2:]
            if path not in _desk_webkit_files():
                return ""
            return _desk_webkit_read(path) or ""
    except Exception:
        log_problem("desk: an earlier desk could not be read", sys.exc_info())
    return ""


# ---------------------------------------------------------------------------
# R96: finding the other computer on the home network.
# ---------------------------------------------------------------------------
def _share_find():
    import socket
    from concurrent.futures import ThreadPoolExecutor
    me = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.255.255", 1))
            me = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        me = ""
    if not re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)", me or ""):
        return {"found": [], "me": me, "error": "NONET"}
    base = me.rsplit(".", 1)[0] + "."
    blocked = [0]
    reached = [0]

    def probe(ip):
        hits = []
        for port in (17860, 17861, 17862):
            k = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            k.settimeout(0.45)
            try:
                r = k.connect_ex((ip, port))
            except Exception:
                r = -1
            finally:
                try:
                    k.close()
                except Exception:
                    pass
            if r in (65, 51, 64):
                blocked[0] += 1
                return hits
            if r == 61 or r == 111 or r == 10061:
                reached[0] += 1
            if r == 0:
                reached[0] += 1
                try:
                    with urllib.request.urlopen("http://%s:%d/ingenium/hello" % (ip, port), timeout=2.5) as resp:
                        h = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                    if h.get("app") == "Ingenium":
                        hits.append({"address": "%s:%d" % (ip, port), "computer": h.get("computer") or "",
                                     "models": h.get("models") or 0, "lends": bool(h.get("lends", True)),
                                     "callins": bool(h.get("callins"))})
                except Exception:
                    pass
        return hits

    ips = [base + str(i) for i in range(1, 255) if base + str(i) != me]
    found = []
    with ThreadPoolExecutor(max_workers=64) as ex:
        for hits in ex.map(probe, ips):
            found.extend(hits)
    err = ""
    if not found and blocked[0] > 50 and reached[0] == 0:
        err = "LOCALNET"
    return {"found": found, "me": me, "error": err}


# R81: big texts (a whole app pasted or attached, a page kept for the
# preview) live apart from the desk, one file each, in a folder beside it.
# The page keeps them in its own store as well; this copy is the one that
# survives a web view whose storage forgets.
_BIG_ID = re.compile(r"^big_[a-z0-9]{7,40}$")


def _big_dir():
    p = _desk_path()
    if p is None:
        return None
    d = p.parent / "big"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return d


def _big_file(bid):
    if not isinstance(bid, str) or not _BIG_ID.match(bid):
        return None
    d = _big_dir()
    return None if d is None else d / (bid + ".txt")


def _big_put(bid, text):
    f = _big_file(bid)
    if f is None:
        return {"ok": False, "error": "no place to keep it"}
    txt = text if isinstance(text, str) else str(text)
    if len(txt) > 64 * 1024 * 1024:
        return {"ok": False, "error": "too large to keep on disk"}
    try:
        tmp = str(f) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(txt)
        os.replace(tmp, str(f))
        return {"ok": True}
    except Exception:
        log_problem("big_put failed", sys.exc_info())
        return {"ok": False, "error": "it could not be written"}


def _big_get(bid):
    f = _big_file(bid)
    try:
        if f is not None and f.is_file():
            return {"ok": True, "text": f.read_text(encoding="utf-8")}
    except Exception:
        log_problem("big_get failed", sys.exc_info())
    return {"ok": False, "error": "not kept here"}


def _big_del(bid):
    f = _big_file(bid)
    try:
        if f is not None and f.is_file():
            f.unlink()
        return True
    except Exception:
        return False


def _big_gc(live):
    """Let go of what the desk no longer names, ten minutes after it was
    written at the earliest (a text being sent is never let go)."""
    d = _big_dir()
    if d is None:
        return 0
    keep = set(x for x in (live or []) if isinstance(x, str))
    now = time.time()
    gone = 0
    try:
        for f in d.glob("big_*.txt"):
            if f.stem in keep:
                continue
            try:
                if now - f.stat().st_mtime > 600:
                    f.unlink()
                    gone += 1
            except Exception:
                pass
    except Exception:
        pass
    return gone


# 6.2: the page a window of its own reads. A window of its own shows one app and the side keys; it never keeps the apps
# Ingenium carries in the library (r120 skips it), never runs a model in the page and never makes kits, so the blocks
# that hold those (about three quarters of the page) are left out of it. Each block is removed whole, so the page's own
# checks see it absent, as in a copy that never carried it.
_WINDOW_DROP = ("ing-builtin-cd", "ing-builtin-forge", "ing-builtin-inkwell", "ingenium-kit-bundle",
                "ingenium-engine-wasm", "ingenium-engine-compat-wasm", "ing-eyes")
_WINDOW_CACHE = {"key": None, "data": None}


def _window_page(html_path):
    try:
        st = os.stat(html_path)
        key = (st.st_mtime_ns, st.st_size)
        if _WINDOW_CACHE["key"] == key:
            return _WINDOW_CACHE["data"]
        text = open(html_path, "rb").read()
        for ident in _WINDOW_DROP:
            text = re.sub(rb'<script\b[^>]*\bid="' + ident.encode() + rb'"[^>]*>[^<]*</script>', b"", text, count=1)
        _WINDOW_CACHE["key"], _WINDOW_CACHE["data"] = key, text
        return text
    except Exception:
        log_problem("window page", sys.exc_info())
        return None


# 6.2: the page as the window first reads it: what is carried for later stays on this side until the page asks for it
_QUICK_DEFER = ("ing-builtin-cd", "ing-builtin-forge", "ing-builtin-inkwell", "ingenium-kit-bundle",
                "ingenium-engine-wasm", "ingenium-engine-compat-wasm", "ing-eyes")
_QUICK_CACHE = {"key": None, "page": None, "parts": {}}


def _quick_page(html_path):
    """(page, parts): the page with the carried blocks left empty, each naming where it is, and the blocks themselves."""
    try:
        st = os.stat(html_path)
        key = (st.st_mtime_ns, st.st_size)
        if _QUICK_CACHE["key"] == key:
            return _QUICK_CACHE["page"], _QUICK_CACHE["parts"]
        text = open(html_path, "rb").read()
        parts = {}
        for ident in _QUICK_DEFER:
            m = re.search(rb'(<script\b[^>]*\bid="' + re.escape(ident.encode()) + rb'"[^>]*)>([^<]*)</script>', text)
            if not m or len(m.group(2)) < 1024:
                continue
            parts[ident] = m.group(2)
            tag = m.group(1) + b' data-defer="/_payload/' + ident.encode() + b'" data-defer-bytes="' + str(len(m.group(2))).encode() + b'">'
            text = text[:m.start()] + tag + b"</script>" + text[m.end():]
        _QUICK_CACHE.update(key=key, page=text, parts=parts)
        return text, parts
    except Exception:
        log_problem("quick page", sys.exc_info())
        return None, {}


def _start_local_server(directory, html_name=None):
    """Serve `directory` on 127.0.0.1, quietly, in a daemon thread. Returns the
    port, or raises. Loopback only: nothing is exposed off this machine, and
    binding to loopback does not raise a firewall prompt.

    It also serves the baked engine itself at /_engine/*, so the page can load
    the engine as same-origin files instead of blob URLs (the blob path is what
    WKWebView kept refusing; ordinary files on the app's own address load
    the ordinary way), and stored model files at /_models/<id> with Range
    support so the page can sniff a header without lifting the whole file.

    R58: the port is stable (17953, or the next free one near it). It HAD to
    become stable: the previous build asked the OS for any free port, so every
    launch was a new storage origin and the app forgot everything it kept,
    which is the "reset every time I open it" you saw. One fixed address means
    the desk remembers from now on.
    """
    html_path = os.path.join(directory, html_name) if html_name else None
    engine_cache = {}

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # R98: the web view must never keep this page. The address is the same for
        # every build (it has to be: the desk lives with it), and SimpleHTTP sends a
        # Last-Modified with no cache rule, so WebKit (and WebView2) kept the page and
        # served an installed 3.9 the 3.6 it had seen before, even after the app was
        # deleted and installed again: the web view's cache lives outside the app.
        def send_header(self, keyword, value):
            if keyword.lower() in ("last-modified", "etag"):
                return
            return http.server.SimpleHTTPRequestHandler.send_header(self, keyword, value)

        def send_response(self, *a, **k):
            self._ing_nocache = False
            return http.server.SimpleHTTPRequestHandler.send_response(self, *a, **k)

        def end_headers(self):
            if not getattr(self, "_ing_nocache", False):
                self._ing_nocache = True
                http.server.SimpleHTTPRequestHandler.send_header(self, "Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                http.server.SimpleHTTPRequestHandler.send_header(self, "Pragma", "no-cache")
                http.server.SimpleHTTPRequestHandler.send_header(self, "Expires", "0")
            return http.server.SimpleHTTPRequestHandler.end_headers(self)

        # 6.2 (app_guard_62.py): the app's own address answers only a request that names it, as the app's window does
        def _ing_host_ok(self):
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").strip().lower()
            if host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port):
                return True
            body = b"This address answers only Ingenium's own window."
            try:
                self.send_response(421)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass
            return False

        def do_HEAD(self):
            if not self._ing_host_ok():
                return
            return http.server.SimpleHTTPRequestHandler.do_HEAD(self)

        def do_POST(self):
            if not self._ing_host_ok():
                return
            if self.path.split("?", 1)[0] == "/_relay":
                return _relay_stream(self)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._ing_host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path == "/_engine/has" or path.startswith("/_engine/part/"):
                data = b"0"
                mime = "text/plain"
                try:
                    if html_path:
                        if path == "/_engine/has":
                            data, mime = _engine_answer(html_path, "has", engine_cache)
                        else:
                            part = path.rsplit("/", 1)[-1]
                            _engine_load_parts(html_path, engine_cache)
                            res = _engine_answer(html_path, part, engine_cache)
                            if res is not None:
                                data, mime = res
                            if not data:
                                self.send_response(404)
                                self.send_header("Content-Type", "text/plain")
                                self.end_headers()
                                return
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    log_problem("engine route failed", sys.exc_info())
                return
            if path == "/ingenium_window.html" and html_path:
                data = _window_page(html_path)
                if data is None:
                    return http.server.SimpleHTTPRequestHandler.do_GET(self)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass
                return
            if path.startswith("/_models/"):
                self._serve_model(path.rsplit("/", 1)[-1])
                return
            # 6.2: the window's own load of the page reads the quick page; a part is given when the page asks for it
            if html_path and path.startswith("/_payload/"):
                page, parts = _quick_page(html_path)
                data = parts.get(path.rsplit("/", 1)[-1])
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=us-ascii")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass
                return
            if (html_path and path == "/" + os.path.basename(html_path) and "full=1" not in self.path
                    and (self.headers.get("Sec-Fetch-Dest") or "").lower() == "document"):
                page, parts = _quick_page(html_path)
                if page is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    try:
                        self.wfile.write(page)
                    except Exception:
                        pass
                    return
            return http.server.SimpleHTTPRequestHandler.do_GET(self)

        def _serve_model(self, mid):
            p = _model_path_for(mid)
            if not p or not os.path.isfile(p):
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                return
            try:
                total = os.path.getsize(p)
                rng = self.headers.get("Range", "")
                start, end = 0, None
                partial = False
                if rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    try:
                        start = int(a) if a else 0
                        end = int(b) if b else None
                    except ValueError:
                        start, end = 0, None
                    if end is None or end >= total:
                        end = total - 1
                    partial = start > 0 or end < total - 1
                if not partial and end is None:
                    end = total - 1
                count = end - start + 1
                fh = open(p, "rb")
                fh.seek(start)
                if partial:
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(count))
                self.end_headers()
                # Stream in slices: the whole model never enters Python memory.
                remaining = count
                while remaining > 0:
                    piece = fh.read(min(8 * 1024 * 1024, remaining))
                    if not piece:
                        break
                    self.wfile.write(piece)
                    remaining -= len(piece)
                fh.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log_problem("model route failed", sys.exc_info())

    handler = functools.partial(_Quiet, directory=directory)
    global _LOCAL_SERVER
    httpd = None
    for port in list(range(17953, 17964)) + [0]:
        try:
            box = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
            httpd = box
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError("no loopback port was free")
    if httpd.server_address[1] != 17953:
        log_problem("mac: preferred loopback port 17953 was busy; using %d" % httpd.server_address[1])
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _LOCAL_SERVER = httpd
    return httpd.server_address[1]


def _page_mark(path):
    """A short mark that changes with every build of the page: a hash of the whole file
    (about 14 MB; some tens of milliseconds, once, at start)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for piece in iter(lambda: fh.read(1 << 20), b""):
                h.update(piece)
        return h.hexdigest()[:12]
    except Exception:
        return str(int(time.time()))


_WINDOW_ASKED = []  # the size the main window was opened at


def _window_file():
    """window.json, beside the web view's stored session"""
    try:
        d = storage_dir()
        return os.path.join(os.path.dirname(d), "window.json") if d else None
    except Exception:
        return None


def _window_start(webview):
    """The main window's first size: as it was last left; else most of the screen it opens on."""
    try:
        p = _window_file()
        if p and os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                o = json.load(fh)
            w, h = int(o.get("w", 0)), int(o.get("h", 0))
            if w >= MIN_WIDTH and h >= MIN_HEIGHT:
                try:  # a size left on a larger display still fits this one
                    sc = webview.screens[0]
                    w, h = min(w, int(sc.width)), min(h, int(sc.height))
                except Exception:
                    pass
                return w, h
    except Exception:
        log_problem("window: the size it was left at could not be read")
    try:
        sc = webview.screens[0]
        return min(int(sc.width), max(DEFAULT_WIDTH, int(sc.width * 0.86))), min(int(sc.height), max(DEFAULT_HEIGHT, int(sc.height * 0.86)))
    except Exception:
        return DEFAULT_WIDTH, DEFAULT_HEIGHT


def _window_keep(window, start=None):
    """Keeps the window's size in window.json, a moment after it last changed.

    The size is kept as it is asked for at the next start. On a Mac the size a window
    reports takes in its title bar, which the size it is opened at does not, so the
    difference seen as it first shows is taken off; else it would grow at every start."""
    st = {"t": None, "dw": 0, "dh": 0}

    def measure():
        try:
            if not start or not window.events.shown.wait(20):
                return
            time.sleep(0.6)
            dw, dh = int(window.width) - int(start[0]), int(window.height) - int(start[1])
            st["dw"], st["dh"] = (dw if 0 <= dw <= 80 else 0), (dh if 0 <= dh <= 80 else 0)
        except Exception:
            pass
    threading.Thread(target=measure, daemon=True).start()

    def save():
        try:
            p = _window_file()
            w, h = int(window.width) - st["dw"], int(window.height) - st["dh"]
            if p and w >= MIN_WIDTH and h >= MIN_HEIGHT:
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump({"w": w, "h": h}, fh)
        except Exception:
            pass

    def on_resized(*_a, **_k):
        if st["t"]:
            st["t"].cancel()
        st["t"] = threading.Timer(0.8, save)
        st["t"].daemon = True
        st["t"].start()
    try:
        window.events.resized += on_resized
    except Exception:
        log_problem("window: this web view reports no resizing; its size is not remembered")


def _make_window(webview, api, app_url, app_html_path):
    """The main window, in one place so its creation can be guarded.

    On macOS the page is served from a loopback http address rather than a file
    address or inline text. The Cocoa web view renders a file address as a blank
    window, and inline text gives the page no real origin, which blocks the
    engine's worker, browser storage, and outbound requests to a local Ollama or
    a cloud provider. A real http origin fixes all of these. If the local server
    cannot start, the code falls back to inline text (which at least renders),
    then to the file address, so nothing regresses.

    R65: Windows and Linux use the local address too (they used the file
    address). One real origin everywhere gives every platform the same
    paths: models linked in place and read from the disk, the build kits
    made from the page itself, and the native engine. Work saved before is
    restored from the copy the app keeps on disk.
    """
    start_w, start_h = _window_start(webview)
    _WINDOW_ASKED[:] = [start_w, start_h]
    common = dict(
        width=start_w,
        height=start_h,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        background_color=ACCENT_AMETHYST,
        confirm_close=False,
        text_select=True,
        js_api=api,
    )
    try:
        directory = os.path.dirname(os.path.abspath(app_html_path))
        name = os.path.basename(app_html_path)
        port = _start_local_server(directory, name)
        # R98: the address carries this build's mark, so a web view that kept an
        # earlier page (before the page was sent as never to be kept) cannot use it
        local_url = "http://127.0.0.1:%d/%s?b=%s" % (port, name, _page_mark(app_html_path))
        if _GFX_SOFT[0]:
            local_url += "&softgpu=1"
        log_problem("serving the page at %s (real origin)" % local_url)
        return webview.create_window(WINDOW_TITLE, url=local_url, **common)
    except Exception:
        log_problem("local server failed; using inline HTML", sys.exc_info())
    html = _read_html_text(app_html_path)
    if html is not None:
        log_problem("loading the page inline")
        return webview.create_window(WINDOW_TITLE, html=html, **common)
    log_problem("page unreadable; falling back to a file address")
    return webview.create_window(WINDOW_TITLE, url=app_url, **common)


def _session_flag():
    """Where the breadcrumb lives. Beside the log, so one folder holds both."""
    try:
        base = pathlib.Path.home() / "Library" / "Application Support" / APP_FOLDER
        base.mkdir(parents=True, exist_ok=True)
        return base / "ingenium_session.flag"
    except Exception:
        try:
            return pathlib.Path.home() / ".ingenium_session.flag"
        except Exception:
            return None


def _begin_session():
    """Return how many previous runs ended without cleaning up after themselves."""
    f = _session_flag()
    if f is None:
        return 0
    prior = 0
    try:
        if f.exists():
            prior = int((f.read_text().strip() or "1"))
    except Exception:
        prior = 1
    try:
        f.write_text(str(prior + 1))
    except Exception:
        pass
    return prior


def _end_session():
    """A clean exit removes the breadcrumb, so only bad endings accumulate."""
    f = _session_flag()
    try:
        if f is not None and f.exists():
            f.unlink()
    except Exception:
        pass
    _lite_mark(False)


_RP_MONITOR = None


def _open_panel_via_js(window, x, y):
    """Runs on its own thread: evaluate_js may wait on the main queue, and the
    monitor callback must never hold the run loop hostage. A background thread
    leaves the main queue free to answer it."""
    js = ("(function(){try{"
          "if(window.__ingEmRecent&&window.__ingEmRecent()){return 'skip';}"
          "if(typeof window.__ingEmFromNative==='function'){window.__ingEmFromNative(%r,%r);return 'open';}"
          "return 'absent';}catch(e){return 'err:'+(e&&e.message);}})()"
          % (x, y))
    try:
        result = window.evaluate_js(js)
    except BaseException:
        return
    if result not in (None, "skip", "open"):
        write_diag(["right-press channel answered: %r" % (result,)])


def _platform_tag():
    """Short platform label for log lines ("mac", "win", "linux")."""
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def _install_right_press_monitor(window):
    """macOS only. See the secondary click at the operating system and open the
    page's own edit panel from there.

    Two lessons from the first attempt are folded in here. The monitor used to
    be installed only from the page-loaded event, which this pywebview backend
    may never fire; it is now installed at run loop start instead. And the
    click handler used to evaluate JavaScript synchronously inside the event,
    which can hold Cocoa's run loop hostage; the script now runs on its own
    thread and the press itself is NOT swallowed, so a future web view that
    does deliver the event simply lets the page win and this path steps aside
    (the page stamps every panel it opens for exactly that check)."""
    global _RP_MONITOR
    if _RP_MONITOR is not None or sys.platform != "darwin":
        return
    try:
        import AppKit

        mask = None
        for name_a, name_b in (("NSEventMaskRightMouseDown", "NSRightMouseDownMask"),
                               ("NSEventMaskLeftMouseDown", "NSLeftMouseDownMask")):
            bit = getattr(AppKit, name_a, None)
            if bit is None:
                bit = getattr(AppKit, name_b, 0)
            mask = bit if mask is None else (mask | bit)
        if not mask:
            log_problem("right-press monitor: no event masks found in AppKit")
            return
        type_right = getattr(AppKit, "NSEventTypeRightMouseDown", getattr(AppKit, "NSRightMouseDown", 3))
        type_left = getattr(AppKit, "NSEventTypeLeftMouseDown", getattr(AppKit, "NSLeftMouseDown", 1))
        flag_ctrl = getattr(AppKit, "NSEventModifierFlagControl", getattr(AppKit, "NSControlKeyMask", 1 << 18))

        def _handler(ev):
            try:
                t = ev.type()
                secondary = (t == type_right) or (t == type_left and bool(ev.modifierFlags() & flag_ctrl))
                if not secondary:
                    return ev
                win = ev.window()
                if win is None:
                    return ev
                # Only our own main window: save dialogs and panels keep their
                # native behaviour untouched.
                try:
                    if str(win.title() or "") != WINDOW_TITLE:
                        return ev
                except Exception:
                    return ev
                pt = ev.locationInWindow()
                try:
                    ch = float(win.contentView().frame().size.height)
                    cw = float(win.contentView().frame().size.width)
                except Exception:
                    return ev
                x, y = float(pt.x), float(pt.y)
                # The title bar is the window's, not the page's: its menu stays native.
                if x < 0 or y >= ch or x >= cw or y < 0:
                    return ev
                cy = ch - y  # AppKit y is measured from the bottom; CSS y from the top
                write_diag(["right press seen by the shell at %d,%d (page space %d,%d)" % (x, y, x, cy)])
                threading.Thread(target=_open_panel_via_js, args=(window, x, cy), daemon=True).start()
                return ev  # never swallowed: if the page heard it too, the page wins
            except BaseException:
                return ev

        _RP_MONITOR = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, _handler)
        if _RP_MONITOR is None:
            log_problem("right-press monitor: AppKit refused the monitor")
        else:
            write_diag(["right-press monitor installed (AppKit, main window only)"])
            log_problem("right-press monitor installed (AppKit, main window only)")
    except Exception:
        log_problem("right-press monitor failed", sys.exc_info())


def _post_start(window):
    """Kept for compatibility with builds that pass it to webview.start."""
    _install_right_press_monitor(window)


def _start_monitor_thread(window):
    """Install the right-press monitor without depending on any pywebview
    vintage. The crash log from the owner's Windows machine showed a pywebview
    old enough to have no events API and no private_mode keyword, and pywebview
    that old also rejects the func/args positionals R60 passed to start(),
    which would stop the app opening entirely. So: no positional arguments to
    start() anywhere, and the monitor installs via AppHelper.callAfter (the
    pyobjc helper pywebview itself carries) from a plain daemon thread. If
    AppHelper is absent the direct call is attempted, which on modern macOS
    works from a background thread for a local monitor."""
    if sys.platform != "darwin":
        return

    def _boot():
        try:
            time.sleep(1.2)  # let the window settle so evaluate_js has a page
        except Exception:
            pass
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_install_right_press_monitor, window)
            return
        except Exception:
            pass
        try:
            _install_right_press_monitor(window)
        except Exception:
            log_problem("right-press monitor thread failed", sys.exc_info())

    try:
        threading.Thread(target=_boot, daemon=True).start()
    except Exception:
        pass


import pathlib


def show_seal_in_dock(picture):
    """macOS 26 sets an app's own icon in a grey square when it is not Apple's rounded square, and shrinks it.
    While the app runs, the Dock shows the picture the app gives it, so the round seal is given here, at its full size.
    Anywhere else (Windows, an older Mac) nothing changes."""
    if sys.platform != "darwin":
        return
    try:
        pic = resource_path(picture)
        if not os.path.isfile(pic):
            return
        from AppKit import NSApplication, NSImage
        img = NSImage.alloc().initWithContentsOfFile_(pic)
        if img is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        try:
            log_problem("dock seal", sys.exc_info())
        except Exception:
            pass


def give_bundle_its_seal(picture="ingenium.icns"):
    """R102: macOS 26 puts the icon an app ships with in a grey rounded square and shrinks it, unless that icon is
    Apple's rounded square; a custom icon (what Finder's Get Info sets) it leaves as it is, round. So on macOS 26
    and later the app gives its own bundle the round seal as its custom icon, once for each copy of the app (a
    new build arrives without one), after macOS has already let it open. The Desktop, the Applications folder and
    the Dock then show the seal at its full size, as Carpe Diem and the Forge do. Nothing changes on Windows or on
    an older Mac, and nothing is tried when the app cannot write to its own folder."""
    if sys.platform != "darwin":
        return
    try:
        if int(os.uname().release.split(".")[0]) < 25:   # Darwin 25 is macOS 26
            return
        bundle = os.path.abspath(sys.executable)
        while bundle and bundle != os.path.dirname(bundle) and not bundle.endswith(".app"):
            bundle = os.path.dirname(bundle)
        if not bundle.endswith(".app") or not os.path.isdir(bundle):
            return
        pic = resource_path(picture)
        if not os.path.isfile(pic):
            pic = resource_path("ingenium.png")
        if not os.path.isfile(pic):
            return
        from AppKit import NSImage, NSWorkspace
        img = NSImage.alloc().initWithContentsOfFile_(pic)
        if img is None:
            return
        if os.access(bundle, os.W_OK) and not os.path.exists(os.path.join(bundle, "Icon\r")):
            ok = NSWorkspace.sharedWorkspace().setIcon_forFile_options_(img, bundle, 0)
            log_problem("mac: the seal was %s as this copy's own icon (%s)" % ("given" if ok else "refused", bundle))
        _seal_shortcuts(bundle, img)
    except Exception:
        try:
            log_problem("mac: custom icon", sys.exc_info())
        except Exception:
            pass


def _seal_shortcuts(bundle, img):
    """6.1: the shortcuts to this app on the Desktop (Finder aliases, symbolic links) and copies of it there get the round
    seal as their own icon too. The owner's words (20 October 2026): "It looks fine on the applications list on the mac
    and on the Dock but the shortcut on the desktop has the grey look." A shortcut is a file of its own, which macOS 26
    draws with the app's shipped icon in its grey square; a custom icon on the shortcut itself is left as it is, round.
    Each one is given the seal once (seal_shortcuts.json, beside the stored session, remembers which)."""
    from AppKit import NSWorkspace
    from Foundation import NSURL, NSBundle
    ws = NSWorkspace.sharedWorkspace()
    real = os.path.realpath(bundle)
    try:
        mine = NSBundle.bundleWithPath_(bundle).bundleIdentifier()
    except Exception:
        mine = None
    rec_path = None
    try:
        d = storage_dir()
        rec_path = os.path.join(os.path.dirname(d), "seal_shortcuts.json") if d else None
    except Exception:
        rec_path = None
    rec = {}
    try:
        if rec_path and os.path.isfile(rec_path):
            with open(rec_path, encoding="utf-8") as fh:
                rec = json.load(fh) or {}
    except Exception:
        rec = {}
    desk = os.path.expanduser("~/Desktop")
    if not os.path.isdir(desk):
        return
    changed = False
    for name in sorted(os.listdir(desk)):
        p = os.path.join(desk, name)
        try:
            ours = False
            if os.path.islink(p):
                ours = os.path.realpath(p) == real
            elif name.endswith(".app") and os.path.isdir(p):
                ours = os.path.realpath(p) == real
                if not ours and mine:
                    b = NSBundle.bundleWithPath_(p)
                    ours = bool(b is not None and b.bundleIdentifier() == mine)
                if ours and os.path.exists(os.path.join(p, "Icon\r")):
                    continue
            elif os.path.isfile(p):
                url = NSURL.fileURLWithPath_(p)
                res = NSURL.URLByResolvingAliasFileAtURL_options_error_(url, 1 << 8, None)
                to = res[0] if isinstance(res, tuple) else res
                if to is not None and to.path() and os.path.realpath(str(to.path())) != os.path.realpath(p):
                    ours = os.path.realpath(str(to.path())) == real
            if not ours:
                continue
            st = os.lstat(p)
            if rec.get(p) == [st.st_ino, int(st.st_mtime)]:
                continue
            if ws.setIcon_forFile_options_(img, p, 0):
                st = os.lstat(p)
                rec[p] = [st.st_ino, int(st.st_mtime)]
                changed = True
                try:
                    ws.noteFileSystemChanged_(p)
                except Exception:
                    pass
                log_problem("mac: the seal was given to the shortcut " + p)
        except Exception:
            log_problem("mac: shortcut icon " + p, sys.exc_info())
    if changed and rec_path:
        try:
            with open(rec_path, "w", encoding="utf-8") as fh:
                json.dump(rec, fh)
        except Exception:
            pass


# 6.4 (app_win_64.py): on Windows, one copy at a time, and safe graphics after a start that did not finish, as the Forge does
SINGLE_INSTANCE_NAME = "Local\\" + APP_FOLDER + ".windows.single"
_single_instance = []
SAFE_GRAPHICS_ARGS = "--disable-gpu-compositing --disable-features=CalculateNativeWinOcclusion"
_GFX_SOFT = [False]


def _bring_window_forward(wait_s=30.0):
    """Find the open copy's window by its title (only pywebview's own window class, or one of its message boxes, counts)
    and bring it forward: restored when minimised, shown as it is otherwise. The first copy may still be making its window,
    so this looks again for up to wait_s seconds."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowExW.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL

    def find():
        after = None
        for _ in range(64):
            hwnd = user32.FindWindowExW(None, after, None, WINDOW_TITLE)
            if not hwnd:
                return None
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            if name.value.startswith("WindowsForms") or name.value == "#32770":
                return hwnd
            after = hwnd
        return None

    end = time.monotonic() + wait_s
    hwnd = find()
    while not hwnd and time.monotonic() < end:
        time.sleep(0.25)
        hwnd = find()
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)
    user32.SetForegroundWindow(hwnd)
    return True


def _another_copy_is_open():
    """True when this app is already open in this sign-in: its window is brought forward and this copy ends at once,
    before the session mark is read or written. A named mutex; the first copy keeps its handle for its whole life, and
    Windows lets go of it when the process ends, so a crash never leaves the name behind. Windows only; anything that goes
    wrong here opens the app as usual."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_NAME)
        err = ctypes.get_last_error()
        if not handle:
            log_problem("single copy: no guard on this PC (error %d); opening as usual" % err)
            return False
        if err != 183:   # ERROR_ALREADY_EXISTS
            _single_instance.append(handle)
            return False
        try:
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle(handle)
        except Exception:
            pass
    except Exception as exc:
        log_problem("single copy: no guard on this PC (%s); opening as usual" % (exc,))
        return False
    try:
        if _bring_window_forward():
            log_problem("start: already open; brought its window forward")
        else:
            log_problem("start: already open, but its window has not appeared yet")
            tell_user("%s is still opening.\n\nIts window will appear in a moment." % APP_NAME)
    except Exception as exc:
        log_problem("start: already open; its window could not be brought forward (%s)" % (exc,))
    return True


def _safe_graphics_file():
    """The note that keeps safe graphics on for this PC: beside the stored work (not inside it, which a fresh start moves
    aside), so an update or a new install keeps it."""
    d = storage_dir()
    d = os.path.dirname(d.rstrip("\\/")) if d else os.path.expanduser("~")
    return os.path.join(d, APP_FOLDER.lower() + "_safe_graphics.txt")


def _graphics_chips():
    """The names of this PC's graphics chips, as Windows lists them (the display adapters' class key), or [] when they
    cannot be read."""
    names = []
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(32):
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        names.append(str(winreg.QueryValueEx(k, "DriverDesc")[0]))
                except OSError:
                    continue
    except Exception:
        pass
    return names


def _prepare_windows_graphics(prior_bad):
    """Choose the arguments Edge WebView2 starts with, on Windows only; True when safe graphics are used. Full graphics on a
    PC that has never had a run end badly; safe graphics from the first start that finds an unfinished run, remembered in
    the note so every later start uses them; on AMD Radeon graphics, safe from the first start. Every start writes one line
    saying which it used. Must run before the web view is made: that is when the arguments are read."""
    if not sys.platform.startswith("win"):
        return False
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
    path = _safe_graphics_file()
    remembered = os.path.isfile(path)
    chips = _graphics_chips()
    amd = [c for c in chips if re.search(r"radeon|\bamd\b|\bati\b", c, re.I)]
    if not remembered and prior_bad < 1 and not amd:
        if "--disable-gpu-compositing" in existing:
            log_problem("graphics: safe, set from outside the app (%s)" % existing)
            return True
        log_problem("graphics: full (the graphics chip helps draw the window; %s)" % (", ".join(chips) or "chip not named"))
        return False
    if remembered:
        why = "safe graphics are on for this PC, after a run that did not finish. To try full graphics again, close %s and delete %s" % (APP_NAME, path)
    else:
        why = (("AMD Radeon graphics (%s): safe graphics from the first start" % ", ".join(amd)) if amd and prior_bad < 1
               else "safe graphics switched on for this PC by themselves, because the last run did not finish")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("Safe graphics are on for %s on this PC.\n\n"
                         "%s switched them on by itself on %s: %s.\n"
                         "With safe graphics the window is drawn without the graphics chip's help, a little less smooth and safe.\n\n"
                         "To try full graphics again: close %s, delete this file, and open %s.\n"
                         "If its window ever closes by itself again, safe graphics switch back on by themselves.\n"
                         % (APP_NAME, APP_NAME, time.strftime("%d %B %Y at %H:%M"), why, APP_NAME, APP_NAME))
        except Exception as exc:
            log_problem("graphics: safe graphics could not be remembered on this PC (%s)" % (exc,))
    args = (existing + " " + SAFE_GRAPHICS_ARGS).strip() if "--disable-gpu-compositing" not in existing else existing
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = args
    log_problem("graphics: %s (%s)" % (why, args))
    return True


def _guard_bridge_frames():
    """6.7 (app_frames_67.py): on a Mac, answer a call from the page only when it comes from the main frame of the app's own
    address, never from a frame inside it (pywebview's message handler is defined in every frame and does not check)."""
    if sys.platform != "darwin":
        return
    try:
        import objc
        from webview.platforms import cocoa as _pc
        base = _pc.BrowserView.JSBridge
        if getattr(base, "_ing_guarded", False):
            return

        class IngeniumFrameGuardedJSBridge(base):
            def userContentController_didReceiveScriptMessage_(self, controller, message):
                try:
                    info = message.frameInfo()
                    if info is None or not info.isMainFrame():
                        return
                    origin = info.securityOrigin()
                    host = str(origin.host() or "") if origin is not None else ""
                    proto = str(origin.protocol() or "").lower() if origin is not None else ""
                    # the app's own address; or the app's own fallbacks (the page given inline, or as a file), never a website
                    if not (host in ("127.0.0.1", "localhost") or proto in ("file", "about") or (proto == "" and host == "")):
                        return
                except Exception:
                    return
                objc.super(IngeniumFrameGuardedJSBridge, self).userContentController_didReceiveScriptMessage_(controller, message)

        IngeniumFrameGuardedJSBridge._ing_guarded = True
        _pc.BrowserView.JSBridge = IngeniumFrameGuardedJSBridge
    except Exception:
        log_problem("guard of the page's calls to the app could not be set", sys.exc_info())


def main():
    global _APP_HTML_PATH
    if _another_copy_is_open():
        return
    app_html = find_app_html()
    _APP_HTML_PATH = app_html
    try:
        import atexit
        atexit.register(_engine_stop)
        atexit.register(_share_stop)
        atexit.register(_train_stop_at_exit)
    except Exception:
        pass
    _share_autostart()
    if not app_html:
        tell_user(
            "Could not find the Ingenium app file, ingenium.html.\n\n"
            "If you are running from the source folder, keep ingenium.html\n"
            "beside this program. The portable version that needs no folder is\n"
            "dist/Ingenium.app, made by the Build step. It carries the whole\n"
            "app inside and opens from anywhere you move it."
        )
        sys.exit(1)

    log_problem("start: locating the window component")
    try:
        import webview  # pywebview
    except BaseException:
        # Not ImportError alone. On Windows pywebview loads pythonnet, and a
        # runtime that will not start raises RuntimeError, OSError or worse.
        # This was a crash before the window ever appeared.
        log_problem("import webview failed", sys.exc_info())
        # The app never falls back to a browser window. The point of the
        # installed app is its own window; opening the HTML silently behind
        # someone's back was exactly the behaviour that read as broken.
        _missing = isinstance(sys.exc_info()[1], ImportError)
        if _missing:
            tell_user(friendly_missing_dependency_message())
        else:
            tell_user(
                "The component that draws Ingenium's window would not start on\n"
                "this machine. The usual cause is an old or incomplete copy of\n"
                "pywebview; the Build step installs the right one in a private\n"
                "folder, so running the Build again is the clean fix.\n\n"
                "Details were written to:\n  %s" % crash_log_path())
        sys.exit(1)

    # Create the native window pointing at the local HTML file. Because the file
    # is loaded from disk, everything runs offline with no server involved.
    # background_color tints the native window behind the page. It is what shows
    # during load, so the window reads as Ingenium amethyst rather than grey.
    try:
        from pathlib import Path
        app_url = Path(app_html).as_uri()
    except Exception:
        app_url = app_html

    # Recorded once at startup so a crash report says what it was running on.
    try:
        import platform
        log_problem("start: python %s on %s %s | pywebview %s" % (
            platform.python_version(), platform.system(), platform.release(),
            getattr(webview, "__version__", "unknown")))
    except Exception:
        pass

    _prior_bad = _begin_session()
    _lite_start(_prior_bad)
    try:
        _GFX_SOFT[0] = _prepare_windows_graphics(_prior_bad)
    except Exception:
        log_problem("graphics: could not be chosen; full graphics", sys.exc_info())
    if _prior_bad >= 2:
        # Two runs in a row ended badly. The stored session is the usual
        # suspect: a poisoned save used to survive every reinstall (it lives in
        # the profile, never beside the app) and wedge every later start, which
        # is the "crash permanently" the owner described. The fix is a fresh
        # start: the stored web data is moved aside, not deleted, so nothing is
        # lost beyond recall, and the page then boots empty and well. The page
        # also quarantines a corrupt save on its own and keeps a recovery shelf
        # of earlier moments, so routine work is carried by gentler means; this
        # step is only for two hard crashes in a row.
        try:
            store = storage_dir()
            if store and os.path.isdir(store):
                import shutil
                aside = (store.rstrip(os.sep) + ".crashed-%d" % _prior_bad)
                if os.path.exists(aside):
                    shutil.rmtree(aside)
                os.rename(store, aside)
                log_problem("start: %d runs ended badly; stored session moved aside to %s; starting fresh"
                            % (_prior_bad, aside))
        except Exception:
            log_problem("start: fresh-start move failed", sys.exc_info())
    if not os.environ.get("INGENIUM_NATIVE_WINDOWS"):
        api = IngeniumApiSingle(app_url)
        log_problem("%s: one window, the app draws its own floating windows" % _platform_tag())
    else:
        api = IngeniumApi(app_url)
    try:
        window = _make_window(webview, api, app_url, app_html)
    except BaseException:
        # Creating the window can fail on its own, before start() is reached.
        # There is no browser fallback: the window IS the app here.
        log_problem("create_window failed", sys.exc_info())
        tell_user(
            "%s could not open its window on this machine.\n\n"
            "On Windows the usual cause is a missing Edge WebView2 runtime (see\n"
            "WINDOWS_README.txt). On a Mac it is a Python built without the\n"
            "frameworks a window needs; the python.org build has them.\n\n"
            "Details were written to:\n  %s" % (APP_NAME, crash_log_path()))
        sys.exit(1)
    try:
        api._main_window = window
    except Exception:
        pass
    _window_keep(window, _WINDOW_ASKED or None)

    # ---- load watchdog -----------------------------------------------------
    # A window whose page never paints used to sit amethyst and silent for
    # ever, indistinguishable from "doing nothing". Now a watcher checks, a
    # few seconds in, that the page loaded AND that its script ran to the end
    # (the page raises window.__INGENIUM_BOOTED on its last line). If either
    # has not happened it first feeds the bundled page to the window inline,
    # which bypasses any file-reading fault; if even that does not take, it
    # writes exactly what it found to ingenium_diag.txt and says so on screen.
    _boot_state = {"loaded": False}
    try:
        def _on_loaded(*_a, **_k):
            _boot_state["loaded"] = True
            log_problem("load: page reported loaded")
            _install_right_press_monitor(window)
        window.events.loaded += _on_loaded
    except Exception:
        log_problem("load: backend exposes no loaded event; watchdog still runs")

    def _probe(w):
        def ev(js):
            try:
                return w.evaluate_js(js)
            except BaseException:
                return "!"+repr(sys.exc_info()[1])
        return {"readyState": ev("document.readyState"),
                "bodyChildren": ev("document.body ? document.body.children.length : -1"),
                "booted": ev("window.__INGENIUM_BOOTED ? 1 : 0"),
                "lastError": ev("String(window.__INGENIUM_LAST_ERROR || '')"),
                "htmlBytes": ev("document.documentElement ? document.documentElement.outerHTML.length : -1"),
                "title": ev("String(document.title)"),
                "href": ev("String(location.href)"),
                "userAgent": ev("navigator.userAgent"),
                "loadedEvent": _boot_state["loaded"]}

    def _page_ok(pr):
        # Backends differ on whether a JS number arrives as int or float, so
        # coerce; a boot flag may also arrive as a real boolean.
        try:
            kids = float(pr.get("bodyChildren"))
        except (TypeError, ValueError):
            kids = -1.0
        return kids > 0 and pr.get("booted") in (1, 1.0, True)

    def _probe_lines(pr):
        return ["%s: %r" % (k, pr[k]) for k in sorted(pr)]

    def _can_inline(text):
        # The Windows web view caps inline text near two megabytes, which a
        # baked engine exceeds, so inline recovery there is skipped for a large
        # page. macOS has no such cap for loadHTMLString.
        if text is None:
            return False
        if sys.platform == "darwin":
            return True
        return len(text) < 1800000

    def _speak_blank():
        log_problem("load: page did not start; recorded quietly (no dialog)")

    def _watchdog(win, html_path):
        time.sleep(8)
        # R61: probe first, always. The loaded EVENT used to gate this, so a
        # backend that never fires it made the watchdog misread a healthy page
        # as a dead one and re-feed the page inline eight seconds after every
        # launch: the refresh the owner kept seeing. The probe IS the truth:
        # if it answers readyState, booted and children, the page is alive and
        # nothing is touched. If the probe cannot reach the page at all, we
        # cannot judge it, and a possibly working page is left exactly as it is
        # rather than reloaded blind.
        pr1 = _probe(win)
        answered = [v for v in pr1.values()
                    if not (isinstance(v, str) and v.startswith("!"))]
        if answered and _page_ok(pr1):
            log_problem("load: verified, page booted")
            return
        if not answered and not _boot_state["loaded"]:
            log_problem("load: probe could not reach the page; leaving it alone")
            write_diag(["first check: no answer from the page; nothing touched"])
            return
        log_problem("load: page answered but NOT verified: %r" % (pr1,))
        write_diag(["first check: page answered but did not verify"] + _probe_lines(pr1))

        # Recovery: hand the window the page text directly. Reset the loaded
        # flag first so we can tell whether THIS load finishes.
        text = _read_html_text(html_path)
        _boot_state["loaded"] = False
        if _can_inline(text):
            try:
                win.load_html(text)
                log_problem("load: recovery inline load issued")
                write_diag(["recovery: inline load issued (%d chars)" % len(text)])
            except BaseException:
                log_problem("load: inline recovery failed", sys.exc_info())
                write_diag(["recovery: inline load FAILED", repr(sys.exc_info()[1])])
        else:
            write_diag(["recovery: inline load skipped (page too large for this web view)"])

        time.sleep(6)
        # Same rule: only probe if this recovery load actually finished.
        if _boot_state["loaded"]:
            pr2 = _probe(win)
            if _page_ok(pr2):
                log_problem("load: recovered, page booted after inline load")
                write_diag(["second check: recovered"] + _probe_lines(pr2))
                return
            write_diag(["second check: STILL NOT STARTING"] + _probe_lines(pr2))
        else:
            write_diag(["second check: recovery load never finished"])
        _speak_blank()

    try:
        import threading as _th
        _wt = _th.Thread(target=_watchdog, args=(window, app_html), daemon=True)
        _wt.start()
        log_problem("load: watchdog armed")
    except Exception:
        log_problem("load: watchdog could not be started", sys.exc_info())

    # gui=None lets pywebview pick the best native backend for the platform
    # (Edge WebView2 on Windows, WKWebView on macOS, WebKitGTK/Qt on Linux).
    # debug=False keeps developer tools hidden for a clean, app-like feel.
    def _hook(exc_type, exc, tb):
        log_problem("uncaught", (exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _hook
    try:
        import threading
        if hasattr(threading, "excepthook"):
            threading.excepthook = lambda a: log_problem(
                "uncaught in thread", (a.exc_type, a.exc_value, a.exc_traceback))
    except Exception:
        pass

    # private_mode=False is what makes the work survive closing the app.
    # storage_path keeps it in the user's profile rather than in a temporary
    # unpack directory. pywebview versions differ on which keywords they
    # accept, so each richer attempt degrades to the plainer one, and only a
    # genuine start failure reports anything.
    show_seal_in_dock('ingenium.png')
    give_bundle_its_seal()
    _start_monitor_thread(window)
    _store = storage_dir()
    _started = False
    _start_err = None
    for kwargs in (
        dict(gui=None, debug=False, private_mode=False, storage_path=_store) if _store else None,
        dict(gui=None, debug=False, private_mode=False),
        dict(gui=None, debug=False),
        dict(),
    ):
        if kwargs is None:
            continue
        try:
            # R62: no positional hook here at all. A pywebview old enough to
            # lack private_mode (as the owner's Windows log showed) also lacks
            # the func/args positionals, and rejecting them four times in a row
            # would keep the app from opening at all. The right-press monitor
            # installs from its own thread instead.
            _guard_bridge_frames()  # 6.7 (app_frames_67.py)
            webview.start(**kwargs)
            _started = True
            _engine_stop()
            _train_stop_at_exit()
            _end_session()
            if "private_mode" not in kwargs:
                log_problem("webview.start accepted no persistence options; "
                            "this pywebview is old and work may not persist")
            break
        except TypeError as te:
            _start_err = te
            log_problem("webview.start refused %r; trying a plainer call" % sorted(kwargs))
            continue
        except Exception as e:
            _start_err = e
            log_problem("webview.start failed", sys.exc_info())
            break
    if not _started:
        tell_user(
            "%s could not open its window on this machine.\n\n"
            "On Windows the usual cause is a missing Edge WebView2 runtime (see\n"
            "WINDOWS_README.txt); on a Mac, a Python built without the\n"
            "frameworks a window needs.\n\n"
            "Details were written to:\n  %s" % (APP_NAME, crash_log_path()))
        sys.exit(1)


def _entry():
    """Start the app (or its self-test), as a script or as a built app."""
    if "--selftest-model" in sys.argv:
        sys.exit(_selftest(sys.argv))
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A crash now leaves a file to read instead of a window that vanishes.
        log_problem("fatal")
        tell_user(
            "%s stopped unexpectedly.\n\n"
            "Details were written to:\n  %s\n\n"
            "Send that file with a note about what was on screen and the cause\n"
            "can usually be found exactly." % (APP_NAME, crash_log_path()))
        sys.exit(1)


if __name__ == "__main__":
    _entry()
