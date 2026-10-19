"""Disk Map — see what is using a drive, then remove specific files.

Double-click "Start Disk Map.bat" or run:  py -3 diskspace.py
Everything stays on this PC. The page is served only to 127.0.0.1.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import heapq
import io
import json
import os
import shutil
import stat
import string
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import defaultdict
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

APP_NAME = "diskmap"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
LOG_PATH = os.path.join(HERE, "diskmap.log")

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_DIRECTORY = 0x10
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

DIR_MIN_KEEP = 16 * 1024
DIR_CAP = 60
GLOBAL_MIN = 64 * 1024
GLOBAL_CAP = 50000
OLD_LARGE_MIN = 100 * 1024 * 1024
OLD_LARGE_DAYS = 180
FULL_HASH_LIMIT = 256 * 1024 * 1024
EMPTY_CAP = 5000
DUP_FILE_CAP = 1500

CREATE_NO_WINDOW = 0x08000000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

kernel32.GetLogicalDrives.restype = wintypes.DWORD
kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel32.GetDriveTypeW.restype = wintypes.UINT
kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    wintypes.DWORD,
]
kernel32.GetVolumeInformationW.restype = wintypes.BOOL
kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
kernel32.GetFileAttributesW.restype = wintypes.DWORD

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Video": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".mpeg", ".mpg", ".ts", ".m2ts"),
    "Audio": (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".opus", ".aiff"),
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".dng", ".psd", ".svg"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab"),
    "Disk images": (".iso", ".img", ".vhd", ".vhdx", ".vmdk", ".wim", ".esd"),
    "Documents": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".rtf", ".odt", ".ods", ".md", ".epub", ".one"),
    "Programs": (".exe", ".msi", ".msix", ".appx"),
    "System": (".dll", ".sys", ".ocx", ".drv"),
    "Dumps": (".dmp", ".hdmp", ".mdmp"),
    "Dev": (".js", ".ts", ".tsx", ".jsx", ".py", ".pyc", ".map", ".pdb", ".jar", ".woff", ".woff2"),
}

EXT_CATEGORY = {ext: name for name, exts in CATEGORIES.items() for ext in exts}

PROTECTED_FILES = {
    "pagefile.sys": "Windows manages pagefile.sys. Disk Map will not delete it. Shrink it from Advanced system settings, Performance, Advanced, Virtual memory.",
    "hiberfil.sys": "Windows manages hiberfil.sys. Disk Map will not delete it. An administrator can turn hibernation off with: powercfg /hibernate off",
    "swapfile.sys": "Windows manages swapfile.sys. Disk Map will not delete it.",
}

LOCKED_TAILS = (
    "\\windows\\system32",
    "\\windows\\syswow64",
    "\\windows\\winsxs",
    "\\windows\\servicing",
    "\\windows\\systemresources",
    "\\windows\\fonts",
    "\\system volume information",
)

EXACT_BLOCK_TAILS = (
    "\\windows",
    "\\users",
    "\\program files",
    "\\program files (x86)",
    "\\programdata",
)

WARN_RULES = (
    ("\\windows.old", "This is a previous Windows installation. Remove it only if you do not need to roll back."),
    ("\\windows\\temp", "Windows temp is usually safe to clear while installers and updates are finished."),
    ("\\windows\\softwaredistribution", "This is the Windows Update download cache. Leave it alone if an update is running."),
    ("\\windows\\installer", "This is the Windows Installer cache. Removing it can block uninstall or repair later."),
    ("\\windows", "This file is inside Windows. Remove it only if you know you do not need it."),
    ("\\program files (x86)", "This belongs to an installed program. Removing it can break that program."),
    ("\\program files", "This belongs to an installed program. Removing it can break that program."),
    ("\\programdata", "This is shared app data. Removing it can reset or break a program."),
)

# (path suffix, label, minimum bytes). Matched against the folder itself, not its children.
PATH_RULES = (
    ("\\windows\\softwaredistribution", "Windows Update downloads", 20 << 20),
    ("\\windows\\winsxs", "Windows component store", 50 << 20),
    ("\\windows\\installer", "Windows Installer cache", 30 << 20),
    ("\\windows\\temp", "Temporary files", 10 << 20),
    ("\\windows\\logs", "Windows logs", 20 << 20),
    ("\\windows.old", "Previous Windows installation", 100 << 20),
    ("\\appdata\\local\\temp", "Temporary files", 10 << 20),
    ("\\appdata\\local\\pip", "pip cache", 10 << 20),
    ("\\appdata\\local\\npm-cache", "npm cache", 10 << 20),
    ("\\appdata\\local\\pnpm", "pnpm store", 20 << 20),
    ("\\appdata\\local\\yarn", "Yarn cache", 10 << 20),
    ("\\appdata\\local\\docker", "Docker data", 50 << 20),
    ("\\appdata\\local\\packages", "Microsoft Store app data", 80 << 20),
    ("\\appdata\\local\\discord", "Discord data", 80 << 20),
    ("\\appdata\\local\\google\\chrome", "Chrome data", 80 << 20),
    ("\\appdata\\local\\microsoft\\edge", "Edge data", 80 << 20),
    ("\\appdata\\local\\microsoft\\windows\\inetcache", "Internet cache", 10 << 20),
    ("\\appdata\\local\\crashdumps", "Crash dumps", 10 << 20),
    ("\\appdata\\local\\nvidia", "NVIDIA cache", 50 << 20),
    ("\\deliveryoptimization", "Delivery Optimization cache", 20 << 20),
    ("\\xboxgames", "Xbox games", 200 << 20),
    ("\\windowsapps", "Microsoft Store apps", 200 << 20),
    ("steamapps\\common", "Steam games", 200 << 20),
    ("\\$recycle.bin", "Recycle Bin", 10 << 20),
)

NAME_RULES = (
    ("node_modules", "Node.js modules", 15 << 20),
    (".git", "Git history", 20 << 20),
    ("__pycache__", "Python cache", 8 << 20),
    (".nuget", "NuGet packages", 20 << 20),
    (".gradle", "Gradle cache", 20 << 20),
    (".m2", "Maven cache", 20 << 20),
    (".npm", "npm cache", 10 << 20),
    (".cache", "Cache", 20 << 20),
    ("cache", "Cache", 40 << 20),
    ("caches", "Cache", 20 << 20),
    ("temp", "Temporary files", 20 << 20),
    ("tmp", "Temporary files", 20 << 20),
    ("logs", "Log files", 80 << 20),
    ("crashdumps", "Crash dumps", 20 << 20),
    ("dumps", "Crash dumps", 50 << 20),
    ("docker", "Docker data", 50 << 20),
    ("windows.old", "Previous Windows installation", 100 << 20),
    ("$recycle.bin", "Recycle Bin", 10 << 20),
    ("prefetch", "Prefetch data", 20 << 20),
)

LABEL_TIPS = {
    "Windows Update downloads": "Old update downloads. Safe to clear when Windows Update is not installing.",
    "Windows component store": "Windows needs this. Do not delete it by hand. Use Disk Cleanup, then Clean up system files, to shrink it.",
    "Windows Installer cache": "Used to repair or uninstall programs. Clearing it saves space and can block repairs later.",
    "Temporary files": "Leftover scratch files. Usually safe once the related program is closed.",
    "Previous Windows installation": "The previous Windows setup. Remove it only if you do not need to go back.",
    "pip cache": "Downloaded Python packages. Safe to clear. pip will download them again if needed.",
    "npm cache": "Cached npm downloads. Safe to clear.",
    "pnpm store": "Shared pnpm packages. Clearing it makes later installs re-download.",
    "Yarn cache": "Cached Yarn downloads. Safe to clear.",
    "Docker data": "Images and containers. Remove ones you do not need from Docker itself so it stays consistent.",
    "Microsoft Store app data": "Data for Store apps. Deleting a whole package folder can break that app.",
    "Discord data": "Discord cache and updates. You can clear the Cache folder while Discord is closed.",
    "Chrome data": "Chrome profile, cache, and downloads it tracks. Clearing Cache is safer than removing the whole folder.",
    "Edge data": "Edge profile and cache. Clearing Cache is safer than removing the whole folder.",
    "Internet cache": "Browser cache. Safe to clear.",
    "Crash dumps": "Memory dumps from crashes. Safe to remove if you are not sending them to support.",
    "NVIDIA cache": "Installer and shader leftovers. Safe to clear while games are closed.",
    "Delivery Optimization cache": "Windows update sharing cache. Safe to clear.",
    "Xbox games": "Installed Xbox or Microsoft Store games.",
    "Microsoft Store apps": "Installed Store apps. Uninstall them from Windows so the app list stays right.",
    "Steam games": "Installed Steam games. Uninstalling from Steam also clears the library entry.",
    "Recycle Bin": "Files already waiting to be deleted. Emptying it frees the space for good.",
    "Node.js modules": "Project dependencies. You can delete these and reinstall from the package file.",
    "Git history": "The full history of a project. Deleting .git drops the history, not just build output.",
    "Python cache": "Generated bytecode. Safe to delete.",
    "NuGet packages": "Cached .NET packages. Safe to clear.",
    "Gradle cache": "Cached build files. Safe to clear.",
    "Maven cache": "Cached Java packages. Safe to clear.",
    "Cache": "Cached files. Usually safe to clear while the app is closed.",
    "Log files": "Old logs. Safe to remove if you do not need them for troubleshooting.",
    "Prefetch data": "Windows startup notes. Small, and Windows rebuilds them. No need to chase these.",
    "Docker data": "Docker's disk. Prefer Docker's own cleanup so containers stay consistent.",
}

DISK_EXT_TIPS = {
    ".vhdx": "Virtual hard disk. The space is inside WSL, Docker, or a virtual machine.",
    ".vhd": "Virtual hard disk. The space is inside a virtual machine or an old backup.",
    ".vmdk": "VMware virtual disk.",
    ".iso": "Disc image. Safe to remove if you no longer install from it.",
    ".wim": "Windows image. Do not remove one inside Windows itself.",
    ".esd": "Compressed Windows image used by installs and updates.",
    ".img": "Raw disk image.",
}


class FileRec(tuple):
    __slots__ = ()

    def __new__(cls, size, mtime, path, name, ext, category):
        return tuple.__new__(cls, (size, mtime, path, name, ext, category))

    @property
    def size(self):
        return self[0]

    @property
    def mtime(self):
        return self[1]

    @property
    def path(self):
        return self[2]

    @property
    def name(self):
        return self[3]

    @property
    def ext(self):
        return self[4]

    @property
    def category(self):
        return self[5]


class DirNode:
    __slots__ = (
        "name",
        "path",
        "size",
        "file_count",
        "mtime",
        "children",
        "parent",
        "top_files",
        "immediate_file_bytes",
        "immediate_file_count",
        "error",
        "aborted",
    )

    def __init__(self, path: str):
        self.path = path
        self.name = path_name(path)
        self.size = 0
        self.file_count = 0
        self.mtime = 0.0
        self.children = []
        self.parent = None
        self.top_files = []
        self.immediate_file_bytes = 0
        self.immediate_file_count = 0
        self.error = None
        self.aborted = False


def path_name(path: str) -> str:
    trimmed = path.rstrip("\\/")
    base = os.path.basename(trimmed)
    return base or path


def to_long(path: str) -> str:
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def to_display(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def clean_path(raw: str) -> str:
    text = (raw or "").strip().strip('"').strip()
    if len(text) == 2 and text[1] == ":":
        text += "\\"
    return os.path.abspath(text)


def normkey(path: str) -> str:
    display = to_display(path)
    folded = os.path.normcase(os.path.normpath(display))
    if len(folded) == 3 and folded[1] == ":" and folded[2] == "\\":
        return folded[:2]
    return folded.rstrip("\\")


def tail_of(path: str) -> str:
    folded = normkey(path)
    if len(folded) >= 2 and folded[1] == ":":
        return folded[2:]
    return folded


def is_drive_root(path: str) -> bool:
    display = os.path.normpath(to_display(os.path.abspath(path)))
    return len(display) == 3 and display[1] == ":" and display[2] == "\\"


def is_inside(path: str, root: str) -> bool:
    return normkey(path).startswith(normkey(root) + "\\")


def drive_root_of(path: str) -> str:
    drive, _tail = os.path.splitdrive(os.path.abspath(path))
    if drive:
        return drive + "\\"
    return path


def os_message(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc)


def file_attributes(path: str) -> int:
    attrs = kernel32.GetFileAttributesW(to_long(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        return 0
    return int(attrs)


def is_reparse_path(path: str) -> bool:
    return bool(file_attributes(path) & FILE_ATTRIBUTE_REPARSE_POINT)


def category_for(ext: str) -> str:
    return EXT_CATEGORY.get(ext, "Other")


def extension_of(name: str) -> str:
    _base, ext = os.path.splitext(name)
    ext = ext.lower()
    if not ext or len(ext) > 16 or ext.count(".") > 1:
        return ""
    return ext


def age_bucket(mtime: float, now: float) -> str:
    if not mtime:
        return "unknown"
    days = (now - mtime) / 86400
    if days < 7:
        return "7"
    if days < 30:
        return "30"
    if days < 90:
        return "90"
    if days < 365:
        return "365"
    return "older"


def push_capped(heap: list, item, cap: int) -> None:
    if len(heap) < cap:
        heapq.heappush(heap, item)
    elif item[0] > heap[0][0]:
        heapq.heappushpop(heap, item)


def clutter_match(path: str, name: str) -> tuple[str, int] | None:
    low = path.casefold().rstrip("\\")
    for suffix, label, minimum in PATH_RULES:
        if low.endswith(suffix):
            return label, minimum
    folded = name.casefold()
    for exact, label, minimum in NAME_RULES:
        if folded == exact:
            return label, minimum
    return None


def removal_problem(path: str, scan_root: str | None) -> str | None:
    if not scan_root or not (normkey(path) == normkey(scan_root) or is_inside(path, scan_root)):
        return "That item is outside the scanned folder."
    if is_drive_root(path):
        return "Disk Map will not delete a whole drive."
    base = os.path.basename(path.rstrip("\\/")).casefold()
    if base in PROTECTED_FILES:
        return PROTECTED_FILES[base]
    tail = tail_of(path)
    for locked in LOCKED_TAILS:
        if tail == locked or tail.startswith(locked + "\\"):
            return "This is part of Windows. Removing it can break the system, so Disk Map blocks it."
    for exact in EXACT_BLOCK_TAILS:
        if tail == exact:
            return "Disk Map will not delete this whole folder. Open it and remove specific items inside."
    if is_reparse_path(path):
        return "This is a link or junction. Disk Map skips it so something outside the folder is not deleted."
    return None


def removal_warning(path: str, scan_root: str | None) -> str:
    notes = []
    if scan_root and normkey(path) == normkey(scan_root):
        notes.append("This is the folder you scanned. Removing it removes everything in the scan.")
    tail = tail_of(path)
    for prefix, message in WARN_RULES:
        if tail == prefix or tail.startswith(prefix + "\\"):
            notes.append(message)
            break
    parts = [part for part in tail.split("\\") if part]
    if len(parts) == 2 and parts[0] == "users":
        notes.append("This is an entire user profile.")
    return " ".join(notes)


def volume_label(root: str) -> str:
    name = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_comp = wintypes.DWORD()
    flags = wintypes.DWORD()
    ok = kernel32.GetVolumeInformationW(
        root, name, 261, ctypes.byref(serial), ctypes.byref(max_comp), ctypes.byref(flags), fs_name, 261
    )
    return name.value if ok else ""


def drive_kind(root: str) -> str:
    kinds = {2: "Removable", 3: "Local disk", 4: "Network", 5: "CD/DVD", 6: "RAM disk"}
    return kinds.get(int(kernel32.GetDriveTypeW(root)), "Drive")


def list_drives() -> list[dict]:
    mask = int(kernel32.GetLogicalDrives())
    drives = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        root = f"{letter}:\\"
        kind = drive_kind(root)
        label = ""
        total = used = free = 0
        ready = False
        try:
            label = volume_label(root)
            usage = shutil.disk_usage(root)
            total, used, free = usage.total, usage.used, usage.free
            ready = True
        except OSError:
            ready = False
        drives.append(
            {
                "path": root,
                "letter": letter,
                "label": label,
                "kind": kind,
                "total": total,
                "used": used,
                "free": free,
                "ready": ready,
            }
        )
    return drives


def list_shortcuts() -> list[dict]:
    import winreg

    names = [
        ("Desktop", "Desktop"),
        ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}"),
        ("Documents", "Personal"),
        ("Pictures", "My Pictures"),
        ("Videos", "My Video"),
        ("Music", "My Music"),
        ("AppData", "AppData"),
        ("Local AppData", "Local AppData"),
    ]
    found: list[dict] = []
    seen = set()
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
    except OSError:
        key = None

    def resolve(value_name: str) -> str:
        if key is None:
            return ""
        try:
            raw, _typ = winreg.QueryValueEx(key, value_name)
        except OSError:
            return ""
        return os.path.expandvars(str(raw))

    for label, value_name in names:
        path = resolve(value_name)
        if path and os.path.isdir(path):
            marker = normkey(path)
            if marker not in seen:
                seen.add(marker)
                found.append({"label": label, "path": path})
    temp = os.environ.get("TEMP", "")
    if temp and os.path.isdir(temp) and normkey(temp) not in seen:
        found.append({"label": "Temp", "path": temp})
    windir = os.environ.get("WINDIR", r"C:\Windows")
    windows_temp = os.path.join(windir, "Temp")
    if os.path.isdir(windows_temp):
        found.append({"label": "Windows Temp", "path": windows_temp})
    return found


def pick_folder() -> str:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose a folder to scan'
$dialog.UseDescriptionForTitle = $true
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write($dialog.SelectedPath)
}
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    data = completed.stdout or b""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    elif len(data) >= 4 and data[1] == 0 and data[3] == 0:
        text = data.decode("utf-16-le", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    return text.strip().strip("\x00")


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_uint),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
shell32.SHFileOperationW.restype = ctypes.c_int

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


def _sh_error_message(code: int) -> str:
    mapped = {
        0x71: 80,
        0x75: 1223,
        0x78: 5,
        0x79: 206,
        0x7C: 161,
        0x402: 161,
    }
    win_code = mapped.get(code, code)
    try:
        message = ctypes.FormatError(win_code).strip()
    except OSError:
        message = ""
    if not message or message.lower().startswith("the operation completed successfully"):
        return f"Could not move it to the Recycle Bin (code {code:#x})."
    return message


def _double_null_buffer(path: str):
    text = path + "\0\0"
    buf = ctypes.create_unicode_buffer(len(text))
    for index, char in enumerate(text):
        buf[index] = char
    return buf


def send_to_recycle(path: str) -> None:
    display = os.path.abspath(to_display(path))
    buf = _double_null_buffer(display)
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = 0
    operation.lpszProgressTitle = None
    result = int(shell32.SHFileOperationW(ctypes.byref(operation)))
    if result or operation.fAnyOperationsAborted:
        raise OSError(_sh_error_message(result or 0x75))


def _powershell_recycle(path: str) -> None:
    script = r"""
Add-Type -AssemblyName Microsoft.VisualBasic
$path = $env:DISKMAP_TARGET
if (Test-Path -LiteralPath $path -PathType Container) {
  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($path, 'OnlyErrorDialogs', 'SendToRecycleBin')
} else {
  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($path, 'OnlyErrorDialogs', 'SendToRecycleBin')
}
"""
    env = os.environ.copy()
    env["DISKMAP_TARGET"] = os.path.abspath(path)
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        env=env,
    )
    if completed.returncode != 0:
        err = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        if not err:
            err = (completed.stderr or b"").decode("utf-16-le", errors="replace").strip()
        raise OSError(err or "Could not move it to the Recycle Bin.")


def recycle_path(path: str) -> None:
    try:
        send_to_recycle(path)
    except OSError:
        if os.path.exists(to_long(path)):
            _powershell_recycle(path)
            return
        raise


def _make_writable(func, path, _exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise


def permanent_delete(path: str) -> None:
    long_path = to_long(path)
    if os.path.isdir(long_path) and not os.path.islink(long_path):
        shutil.rmtree(long_path, onerror=_make_writable)
    else:
        try:
            os.remove(long_path)
        except PermissionError:
            os.chmod(long_path, stat.S_IWRITE)
            os.remove(long_path)


def reveal_path(path: str) -> None:
    display = os.path.normpath(os.path.abspath(path))
    if os.path.isdir(display):
        subprocess.Popen(["explorer", display])
    else:
        subprocess.Popen(["explorer", f"/select,{display}"])


def open_path(path: str) -> None:
    os.startfile(os.path.normpath(os.path.abspath(path)))  # noqa: S606 - local file the user chose


def log_exception(context: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            traceback.print_exc(file=handle)
    except OSError:
        traceback.print_exc()


def file_public(rec: FileRec, total: int) -> dict:
    return {
        "kind": "file",
        "name": rec.name,
        "path": rec.path,
        "size": rec.size,
        "mtime": rec.mtime,
        "ext": rec.ext,
        "category": rec.category,
        "percent": (rec.size / total * 100) if total else 0,
    }


class DiskIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.cancel = False
        self.done = False
        self.error = None
        self.aborted = False
        self.target = ""
        self.started = 0.0
        self.finished = 0.0
        self.files_seen = 0
        self.dirs_seen = 0
        self.visited = 0
        self.bytes_seen = 0
        self.current = ""
        self.reparse_skipped = 0
        self.hardlink_skipped = 0
        self.denied_count = 0
        self.denied_paths: list[str] = []
        self.eligible_files = 0
        self.root: DirNode | None = None
        self.by_path: dict[str, DirNode] = {}
        self.dir_nodes: list[DirNode] = []
        self.files: list[FileRec] = []
        self.drive = None
        self.categories = defaultdict(lambda: [0, 0])
        self.extensions = defaultdict(lambda: [0, 0])
        self.age = {key: [0, 0] for key in ("7", "30", "90", "365", "older", "unknown")}
        self.clutter: list[tuple[str, str]] = []
        self.old_large: list[FileRec] = []
        self.surprises: list[dict] = []
        self.empty_dirs: list[str] = []
        self.empty_capped = False
        self._gheap: list[FileRec] = []
        self._old_heap: list[FileRec] = []
        self._surprise_heap: list[tuple] = []
        self._seen_inodes: set[tuple] = set()
        self._now = 0.0
        self.dup_running = False
        self.dup_cancel = False
        self.dup_done = 0
        self.dup_total = 0
        self.dup_groups: list[dict] = []
        self.dup_error = None
        self.dup_wasted = 0
        self.dup_note = ""

    def status(self) -> dict:
        elapsed = 0.0
        if self.started:
            end = self.finished or time.time()
            elapsed = max(0.0, end - self.started)
        return {
            "running": self.running,
            "done": self.done and self.root is not None,
            "error": self.error,
            "aborted": self.aborted,
            "target": self.target,
            "files": self.files_seen,
            "dirs": self.dirs_seen,
            "bytes": self.bytes_seen,
            "current": self.current,
            "elapsed": elapsed,
            "denied": self.denied_count,
        }

    def start(self, path: str) -> str | None:
        target = clean_path(path)
        if not os.path.exists(to_long(target)):
            return "That path does not exist."
        if os.path.isfile(to_long(target)):
            return "Choose a folder or a drive, not a single file."
        with self.lock:
            if self.running:
                return "A scan is already running."
            self.running = True
            self.cancel = False
            self.done = False
            self.error = None
            self.aborted = False
            self.target = target
            self.started = time.time()
            self.finished = 0.0
            self.files_seen = 0
            self.dirs_seen = 0
            self.visited = 0
            self.bytes_seen = 0
            self.current = target
            self.reparse_skipped = 0
            self.hardlink_skipped = 0
            self.denied_count = 0
            self.denied_paths = []
            self.eligible_files = 0
            self.root = None
            self.by_path = {}
            self.dir_nodes = []
            self.files = []
            self.categories = defaultdict(lambda: [0, 0])
            self.extensions = defaultdict(lambda: [0, 0])
            self.age = {key: [0, 0] for key in ("7", "30", "90", "365", "older", "unknown")}
            self.clutter = []
            self.old_large = []
            self.surprises = []
            self.empty_dirs = []
            self.empty_capped = False
            self._gheap = []
            self._old_heap = []
            self._surprise_heap = []
            self._seen_inodes = set()
            self.dup_cancel = True
        threading.Thread(target=self._run, args=(target,), name="diskmap-scan", daemon=True).start()
        return None

    def request_cancel(self) -> None:
        self.cancel = True

    def _run(self, target: str) -> None:
        try:
            self._prepare_drive(target)
            root = self._scan(target)
            self._finish_collections(root)
            with self.lock:
                self.root = root
                self.aborted = bool(root.aborted)
                self.done = True
        except Exception as exc:  # noqa: BLE001 - report any scan failure to the UI
            log_exception("scan")
            self.error = os_message(exc)
            self.done = False
        finally:
            self.finished = time.time()
            self.running = False
            self.current = ""

    def _prepare_drive(self, target: str) -> None:
        root = drive_root_of(target)
        try:
            usage = shutil.disk_usage(root)
            self.drive = {
                "path": root,
                "label": volume_label(root),
                "kind": drive_kind(root),
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
            }
        except OSError:
            self.drive = None

    def _scan(self, target: str) -> DirNode:
        self._now = time.time()
        root = DirNode(target)
        self.dirs_seen = 1
        try:
            root.mtime = os.stat(to_long(target), follow_symlinks=False).st_mtime
        except OSError:
            root.mtime = 0
        stack = [(root, 0)]
        while stack:
            node, phase = stack.pop()
            if phase == 1:
                self._finalize(node)
                continue
            if self.cancel:
                node.aborted = True
                self._finalize(node)
                continue
            self._read_dir(node, stack)
        return root

    def _read_dir(self, node: DirNode, stack: list) -> None:
        stack.append((node, 1))
        try:
            iterator = os.scandir(to_long(node.path))
        except OSError as exc:
            node.error = os_message(exc)
            self._deny(node.path)
            return
        with iterator:
            for entry in iterator:
                if self.cancel:
                    node.aborted = True
                    break
                self._consume(node, entry, stack)

    def _consume(self, node: DirNode, entry: os.DirEntry, stack: list) -> None:
        display = to_display(entry.path)
        self.visited += 1
        if self.visited % 40 == 0:
            self.current = display
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            self._deny(display)
            return
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT or stat.S_ISLNK(info.st_mode):
            self.reparse_skipped += 1
            return
        if stat.S_ISDIR(info.st_mode) or attrs & FILE_ATTRIBUTE_DIRECTORY:
            child = DirNode(display)
            child.parent = node
            child.mtime = info.st_mtime or 0
            node.children.append(child)
            self.dirs_seen += 1
            stack.append((child, 0))
            return
        # DirEntry.stat on Windows does not fill st_nlink or st_ino, so hard links are
        # counted in every folder that lists them. Opening each file to dedupe would
        # make a full-drive scan much slower. The drive summary explains the gap.
        size = max(0, int(info.st_size))
        name = entry.name
        ext = extension_of(name)
        category = category_for(ext)
        mtime = float(info.st_mtime or 0)
        rec = FileRec(size, mtime, display, name, ext, category)
        self.files_seen += 1
        node.size += size
        node.file_count += 1
        node.immediate_file_bytes += size
        node.immediate_file_count += 1
        self.bytes_seen += size
        bucket = age_bucket(mtime, self._now)
        self.age[bucket][0] += 1
        self.age[bucket][1] += size
        ext_key = ext or "(none)"
        self.extensions[ext_key][0] += 1
        self.extensions[ext_key][1] += size
        self.categories[category][0] += 1
        self.categories[category][1] += size
        if size >= DIR_MIN_KEEP:
            push_capped(node.top_files, rec, DIR_CAP)
        if size >= GLOBAL_MIN:
            self.eligible_files += 1
            push_capped(self._gheap, rec, GLOBAL_CAP)
        if size >= OLD_LARGE_MIN and mtime and (self._now - mtime) >= OLD_LARGE_DAYS * 86400:
            push_capped(self._old_heap, rec, 120)
        tip = surprise_tip(name, ext, size)
        if tip:
            push_capped(self._surprise_heap, (size, display, rec, tip), 80)

    def _finalize(self, node: DirNode) -> None:
        node.top_files = sorted(node.top_files, key=lambda rec: rec.size, reverse=True)
        self.by_path[normkey(node.path)] = node
        self.dir_nodes.append(node)
        if (
            node.parent is not None
            and node.size == 0
            and not node.error
            and not node.aborted
            and len(self.empty_dirs) < EMPTY_CAP
        ):
            self.empty_dirs.append(node.path)
        elif node.parent is not None and node.size == 0 and not node.error and not node.aborted:
            self.empty_capped = True
        match = clutter_match(node.path, node.name)
        if match and node.parent is not None and node.size >= match[1]:
            self._note_clutter(node, match[0])
        parent = node.parent
        if parent is not None:
            parent.size += node.size
            parent.file_count += node.file_count
            if node.aborted:
                parent.aborted = True

    def _note_clutter(self, node: DirNode, label: str) -> None:
        prefix = normkey(node.path) + "\\"
        self.clutter = [
            (path, existing)
            for path, existing in self.clutter
            if not (existing == label and normkey(path).startswith(prefix))
        ]
        self.clutter.append((node.path, label))

    def _deny(self, path: str) -> None:
        self.denied_count += 1
        if len(self.denied_paths) < 150:
            self.denied_paths.append(path)

    def _finish_collections(self, root: DirNode) -> None:
        self.files = sorted(self._gheap, key=lambda rec: rec.size, reverse=True)
        self.old_large = sorted(self._old_heap, key=lambda rec: rec.size, reverse=True)
        surprises = []
        for _size, _path, rec, tip in sorted(self._surprise_heap, key=lambda item: item[0], reverse=True):
            surprises.append(
                {
                    "kind": "file",
                    "name": rec.name,
                    "path": rec.path,
                    "size": rec.size,
                    "mtime": rec.mtime,
                    "ext": rec.ext,
                    "category": rec.category,
                    "tip": tip,
                }
            )
        self.surprises = surprises
        empty_keys = {normkey(path) for path in self.empty_dirs}
        pruned = []
        for path in self.empty_dirs:
            parent = os.path.dirname(path.rstrip("\\/"))
            if normkey(parent) in empty_keys:
                continue
            pruned.append(path)
        self.empty_dirs = pruned
        self._gheap = []
        self._old_heap = []
        self._surprise_heap = []
        self._seen_inodes = set()

    def overview(self) -> dict | None:
        with self.lock:
            root = self.root
            if root is None:
                return None
            total = root.size
            children = sorted(root.children, key=lambda node: node.size, reverse=True)
            categories = [
                {"name": name, "count": count, "bytes": amount}
                for name, (count, amount) in self.categories.items()
                if amount > 0
            ]
            categories.sort(key=lambda item: item["bytes"], reverse=True)
            extensions = [
                {"ext": ext, "count": count, "bytes": amount}
                for ext, (count, amount) in self.extensions.items()
                if amount > 0
            ]
            extensions.sort(key=lambda item: item["bytes"], reverse=True)
            age_labels = [
                ("7", "Last 7 days"),
                ("30", "8–30 days"),
                ("90", "1–3 months"),
                ("365", "3–12 months"),
                ("older", "Older than a year"),
            ]
            age = [
                {"id": key, "label": label, "count": self.age[key][0], "bytes": self.age[key][1]}
                for key, label in age_labels
            ]
            clutter = []
            for path, label in self.clutter:
                node = self.by_path.get(normkey(path))
                if node is None:
                    continue
                clutter.append(
                    {
                        "path": node.path,
                        "name": node.name,
                        "size": node.size,
                        "label": label,
                        "tip": LABEL_TIPS.get(label, ""),
                        "kind": "dir",
                    }
                )
            clutter.sort(key=lambda item: item["size"], reverse=True)
            gap = None
            if self.drive and is_drive_root(self.target):
                gap = int(self.drive["used"]) - int(total)
            return {
                "target": self.target,
                "target_name": path_name(self.target),
                "is_drive_root": is_drive_root(self.target),
                "aborted": self.aborted,
                "elapsed": max(0.0, (self.finished or time.time()) - self.started),
                "scanned_bytes": total,
                "file_count": root.file_count,
                "dir_count": len(self.dir_nodes),
                "denied_count": self.denied_count,
                "denied_paths": list(self.denied_paths),
                "reparse_skipped": self.reparse_skipped,
                "hardlink_skipped": self.hardlink_skipped,
                "drive": self.drive,
                "unaccounted": gap,
                "root_children": [
                    {
                        "kind": "dir",
                        "name": child.name,
                        "path": child.path,
                        "size": child.size,
                        "file_count": child.file_count,
                        "mtime": child.mtime,
                        "error": child.error,
                        "percent": (child.size / total * 100) if total else 0,
                    }
                    for child in children[:16]
                ],
                "categories": categories,
                "extensions": extensions[:12],
                "age": age,
                "clutter": clutter[:24],
                "largest": [file_public(rec, total) for rec in self.files[:12]],
                "old_large": [file_public(rec, total) for rec in self.old_large[:12]],
                "surprises": self.surprises[:12],
                "empty_total": len(self.empty_dirs),
                "empty_capped": self.empty_capped,
                "empty_dirs": self.empty_dirs[:80],
                "index_capped": self.eligible_files > GLOBAL_CAP,
                "eligible_files": self.eligible_files,
                "indexed_files": len(self.files),
                "min_indexed_bytes": GLOBAL_MIN,
                "min_folder_file_bytes": DIR_MIN_KEEP,
            }

    def folder_payload(self, raw_path: str, full: bool) -> dict | None:
        with self.lock:
            if self.root is None:
                return None
            path = clean_path(raw_path) if raw_path else self.root.path
            if not (normkey(path) == normkey(self.root.path) or is_inside(path, self.root.path)):
                raise PermissionError("That folder is outside this scan.")
            node = self.by_path.get(normkey(path))
            if node is None:
                return None
            entries = []
            for child in node.children:
                entries.append(
                    {
                        "kind": "dir",
                        "name": child.name,
                        "path": child.path,
                        "size": child.size,
                        "mtime": child.mtime,
                        "file_count": child.file_count,
                        "child_count": len(child.children),
                        "error": child.error,
                        "category": "Folder",
                        "ext": "",
                    }
                )
            truncated = False
            if full:
                live, truncated = self._live_files(node.path)
                listed = live
            else:
                listed = list(node.top_files)
            listed_bytes = 0
            for rec in listed:
                listed_bytes += rec.size
                entries.append(file_public(rec, node.size))
            other = None
            if not full:
                other_bytes = node.immediate_file_bytes - sum(rec.size for rec in node.top_files)
                other_count = node.immediate_file_count - len(node.top_files)
                if other_count > 0 and other_bytes > 0:
                    other = {"bytes": other_bytes, "count": other_count}
                    entries.append(
                        {
                            "kind": "other",
                            "name": f"{other_count:,} smaller files",
                            "path": "",
                            "size": other_bytes,
                            "count": other_count,
                            "category": "Other",
                            "ext": "",
                            "mtime": 0,
                        }
                    )
            total = node.size
            for entry in entries:
                entry["percent"] = (entry["size"] / total * 100) if total else 0
            entries.sort(key=lambda item: item["size"], reverse=True)
            treemap = [item for item in entries if item["size"] > 0]
            if len(treemap) > 200:
                rest = treemap[199:]
                treemap = treemap[:199]
                treemap.append(
                    {
                        "kind": "other",
                        "name": f"{len(rest)} more",
                        "path": "",
                        "size": sum(item["size"] for item in rest),
                        "category": "Other",
                        "ext": "",
                        "mtime": 0,
                        "percent": (sum(item["size"] for item in rest) / total * 100) if total else 0,
                    }
                )
                treemap.sort(key=lambda item: item["size"], reverse=True)
            crumbs = []
            cursor = node
            chain = []
            while cursor is not None:
                chain.append(cursor)
                cursor = cursor.parent
            for item in reversed(chain):
                crumbs.append({"name": item.name, "path": item.path})
            parent = node.parent.path if node.parent is not None else None
            return {
                "path": node.path,
                "name": node.name,
                "parent": parent,
                "size": node.size,
                "file_count": node.file_count,
                "mtime": node.mtime,
                "error": node.error,
                "aborted": node.aborted,
                "full": full,
                "truncated": truncated,
                "other": other,
                "entries": entries,
                "treemap": treemap,
                "crumbs": crumbs,
                "scan_root": self.root.path,
            }

    def _live_files(self, path: str) -> tuple[list[FileRec], bool]:
        found: list[FileRec] = []
        try:
            iterator = os.scandir(to_long(path))
        except OSError:
            return [], False
        with iterator:
            for entry in iterator:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                attrs = int(getattr(info, "st_file_attributes", 0) or 0)
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT or stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISDIR(info.st_mode) or attrs & FILE_ATTRIBUTE_DIRECTORY:
                    continue
                name = entry.name
                ext = extension_of(name)
                found.append(
                    FileRec(
                        max(0, int(info.st_size)),
                        float(info.st_mtime or 0),
                        to_display(entry.path),
                        name,
                        ext,
                        category_for(ext),
                    )
                )
        found.sort(key=lambda rec: rec.size, reverse=True)
        truncated = len(found) > 8000
        return found[:8000], truncated

    def query_files(self, params: dict) -> dict | None:
        with self.lock:
            if self.root is None:
                return None
            total_size = self.root.size
            query = (params.get("q") or "").casefold().strip()
            try:
                min_bytes = int(params.get("min_bytes") or 0)
            except ValueError:
                min_bytes = 0
            category = params.get("category") or ""
            ext = (params.get("ext") or "").lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            under = params.get("under") or ""
            under_key = normkey(under) if under else ""
            try:
                older_days = int(params["older_days"]) if params.get("older_days") else None
            except ValueError:
                older_days = None
            sort = params.get("sort") or "size"
            direction = params.get("direction") or "desc"
            try:
                offset = max(0, int(params.get("offset") or 0))
                limit = min(500, max(1, int(params.get("limit") or 200)))
            except ValueError:
                offset, limit = 0, 200
            now = time.time()
            rows = []
            for rec in self.files:
                if rec.size < min_bytes:
                    continue
                if category and rec.category != category:
                    continue
                if ext and rec.ext != ext:
                    continue
                if under_key and not (normkey(rec.path).startswith(under_key + "\\") or normkey(rec.path) == under_key):
                    continue
                if older_days is not None:
                    if not rec.mtime or (now - rec.mtime) < older_days * 86400:
                        continue
                if query and query not in rec.name.casefold() and query not in rec.path.casefold():
                    continue
                rows.append(rec)
            def sort_key(rec: FileRec):
                if sort == "name":
                    return rec.name.casefold()
                if sort == "path":
                    return rec.path.casefold()
                if sort == "mtime":
                    return rec.mtime
                if sort == "category":
                    return rec.category.casefold()
                return rec.size

            rows.sort(key=sort_key, reverse=direction != "asc")
            folders = []
            if query:
                for node in self.dir_nodes:
                    if query not in node.name.casefold() and query not in node.path.casefold():
                        continue
                    if under_key and not (
                        normkey(node.path).startswith(under_key + "\\") or normkey(node.path) == under_key
                    ):
                        continue
                    folders.append(node)
                folders.sort(key=lambda node: node.size, reverse=True)
                folders = folders[:40]
            page = rows[offset : offset + limit]
            return {
                "total": len(rows),
                "offset": offset,
                "limit": limit,
                "files": [file_public(rec, total_size) for rec in page],
                "folders": [
                    {
                        "kind": "dir",
                        "name": node.name,
                        "path": node.path,
                        "size": node.size,
                        "mtime": node.mtime,
                        "file_count": node.file_count,
                        "category": "Folder",
                        "percent": (node.size / total_size * 100) if total_size else 0,
                    }
                    for node in folders
                ],
            }

    def export_csv(self) -> bytes | None:
        with self.lock:
            if self.root is None:
                return None
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Name", "Path", "SizeBytes", "Modified", "Extension", "Category"])
            for rec in self.files:
                modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.mtime)) if rec.mtime else ""
                writer.writerow([rec.name, rec.path, rec.size, modified, rec.ext, rec.category])
            return buf.getvalue().encode("utf-8-sig")

    def _measure(self, path: str) -> tuple[int | None, str]:
        long_path = to_long(path)
        node = self.by_path.get(normkey(path))
        if os.path.isdir(long_path):
            return (node.size if node is not None else None), "dir"
        try:
            info = os.stat(long_path, follow_symlinks=False)
        except OSError:
            return None, "file"
        return max(0, int(info.st_size)), "file"

    def plan_delete(self, paths: list[str]) -> dict:
        with self.lock:
            if self.root is None:
                return {"error": "Scan a folder before removing anything."}
            if self.running:
                return {"error": "Wait for the scan to finish."}
            items = []
            total = 0
            for raw in paths:
                path = clean_path(str(raw))
                problem = removal_problem(path, self.root.path)
                if problem:
                    items.append({"path": path, "blocked": problem})
                    continue
                if not os.path.exists(to_long(path)):
                    items.append({"path": path, "blocked": "Already gone."})
                    continue
                size, kind = self._measure(path)
                warning = removal_warning(path, self.root.path)
                if size:
                    total += size
                items.append(
                    {
                        "path": path,
                        "name": path_name(path),
                        "size": size,
                        "kind": kind,
                        "warning": warning,
                    }
                )
            return {"items": items, "bytes": total}

    def delete_paths(self, paths: list[str], mode: str) -> dict:
        if mode not in {"recycle", "permanent"}:
            return {"error": "Choose recycle or permanent."}
        plan = self.plan_delete(paths)
        if plan.get("error"):
            return plan
        results = []
        for item in plan["items"]:
            if item.get("blocked"):
                results.append({"path": item["path"], "ok": False, "error": item["blocked"]})
                continue
            path = item["path"]
            try:
                if mode == "recycle":
                    recycle_path(path)
                else:
                    permanent_delete(path)
            except OSError as exc:
                results.append({"path": path, "ok": False, "error": os_message(exc)})
                continue
            if os.path.exists(to_long(path)):
                results.append({"path": path, "ok": False, "error": "It is still there. Windows may be using it."})
                continue
            with self.lock:
                self._forget(path, item.get("size") or 0, item.get("kind") or "file")
            results.append({"path": path, "ok": True, "error": None})
        ok_count = sum(1 for item in results if item["ok"])
        return {"results": results, "removed": ok_count, "mode": mode}

    def _forget(self, path: str, size: int, kind: str) -> None:
        if self.root is None:
            return
        key = normkey(path)
        node = self.by_path.get(key)
        if kind == "dir" and node is None:
            return
        if kind == "dir" and node is not None:
            parent = node.parent
            if parent is not None:
                parent.children = [child for child in parent.children if normkey(child.path) != key]
                self._subtract_up(parent, node.size, node.file_count)
            prefix = key + "\\"
            for existing in list(self.by_path):
                if existing == key or existing.startswith(prefix):
                    self.by_path.pop(existing, None)
            self.dir_nodes = [
                item
                for item in self.dir_nodes
                if normkey(item.path) != key and not normkey(item.path).startswith(prefix)
            ]
            self.files = [
                rec
                for rec in self.files
                if normkey(rec.path) != key and not normkey(rec.path).startswith(prefix)
            ]
            self.old_large = [
                rec
                for rec in self.old_large
                if normkey(rec.path) != key and not normkey(rec.path).startswith(prefix)
            ]
            self.surprises = [
                item
                for item in self.surprises
                if normkey(item["path"]) != key and not normkey(item["path"]).startswith(prefix)
            ]
            self.clutter = [
                (item_path, label)
                for item_path, label in self.clutter
                if normkey(item_path) != key and not normkey(item_path).startswith(prefix)
            ]
            self.empty_dirs = [
                item
                for item in self.empty_dirs
                if normkey(item) != key and not normkey(item).startswith(prefix)
            ]
            return
        parent = self.by_path.get(normkey(os.path.dirname(path)))
        if parent is not None:
            parent.top_files = [rec for rec in parent.top_files if normkey(rec.path) != key]
            parent.immediate_file_bytes = max(0, parent.immediate_file_bytes - size)
            parent.immediate_file_count = max(0, parent.immediate_file_count - 1)
            self._subtract_up(parent, size, 1)
        self.files = [rec for rec in self.files if normkey(rec.path) != key]
        self.old_large = [rec for rec in self.old_large if normkey(rec.path) != key]
        self.surprises = [item for item in self.surprises if normkey(item["path"]) != key]

    def _subtract_up(self, node: DirNode | None, size: int, files: int) -> None:
        while node is not None:
            node.size = max(0, node.size - size)
            node.file_count = max(0, node.file_count - files)
            node = node.parent

    def duplicate_status(self) -> dict:
        return {
            "running": self.dup_running,
            "done": self.dup_done,
            "total": self.dup_total,
            "groups": self.dup_groups,
            "wasted": self.dup_wasted,
            "error": self.dup_error,
            "note": self.dup_note,
        }

    def start_duplicates(self, min_bytes: int) -> str | None:
        with self.lock:
            if self.running:
                return "Wait for the scan to finish."
            if self.root is None:
                return "Scan a folder first."
            if self.dup_running:
                return "A duplicate search is already running."
            files = [rec for rec in self.files if rec.size >= min_bytes]
            self.dup_running = True
            self.dup_cancel = False
            self.dup_done = 0
            self.dup_total = 0
            self.dup_groups = []
            self.dup_error = None
            self.dup_wasted = 0
            self.dup_note = ""
        threading.Thread(
            target=self._duplicate_run, args=(files, min_bytes), name="diskmap-dups", daemon=True
        ).start()
        return None

    def _duplicate_run(self, files: list[FileRec], min_bytes: int) -> None:
        try:
            by_size: dict[int, list[FileRec]] = defaultdict(list)
            for rec in files:
                by_size[rec.size].append(rec)
            groups_in = [(size, group) for size, group in by_size.items() if len(group) >= 2]
            groups_in.sort(key=lambda item: item[0], reverse=True)
            chosen = []
            count = 0
            skipped_groups = 0
            for size, group in groups_in:
                if count >= DUP_FILE_CAP:
                    skipped_groups += 1
                    continue
                chosen.append((size, group))
                count += len(group)
            self.dup_total = count
            found = []
            for size, group in chosen:
                if self.dup_cancel:
                    break
                digests: dict[str, list[FileRec]] = defaultdict(list)
                partials: set[str] = set()
                for rec in group:
                    if self.dup_cancel:
                        break
                    try:
                        digest, partial = fingerprint(rec.path, rec.size)
                    except OSError:
                        self.dup_done += 1
                        continue
                    digests[digest].append(rec)
                    if partial:
                        partials.add(digest)
                    self.dup_done += 1
                for digest, recs in digests.items():
                    if len(recs) < 2:
                        continue
                    wasted = size * (len(recs) - 1)
                    found.append(
                        {
                            "size": size,
                            "wasted": wasted,
                            "partial": digest in partials,
                            "hash": digest,
                            "files": [file_public(rec, size) for rec in recs],
                        }
                    )
            found.sort(key=lambda item: item["wasted"], reverse=True)
            note = ""
            if skipped_groups:
                note = "Only the largest matching sizes were hashed. Raise the minimum size to finish faster, or scan a smaller folder for a full pass."
            if min_bytes < FULL_HASH_LIMIT:
                pass
            self.dup_groups = found[:200]
            self.dup_wasted = sum(item["wasted"] for item in found)
            self.dup_note = note
        except Exception as exc:  # noqa: BLE001
            log_exception("duplicates")
            self.dup_error = os_message(exc)
        finally:
            self.dup_running = False


def surprise_tip(name: str, ext: str, size: int) -> str:
    folded = name.casefold()
    if folded in PROTECTED_FILES and size >= 1024 * 1024:
        return PROTECTED_FILES[folded]
    if ext in {".dmp", ".hdmp", ".mdmp"} and size >= 20 * 1024 * 1024:
        return "Crash dump. Safe to remove if you are not sending it to support."
    if ext in DISK_EXT_TIPS and size >= 64 * 1024 * 1024:
        return DISK_EXT_TIPS[ext]
    return ""


def fingerprint(path: str, size: int) -> tuple[str, bool]:
    hasher = hashlib.blake2b(digest_size=16)
    partial = size > FULL_HASH_LIMIT
    with open(to_long(path), "rb") as handle:
        if not partial:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        else:
            hasher.update(handle.read(1024 * 1024))
            handle.seek(max(0, size - 1024 * 1024))
            hasher.update(handle.read(1024 * 1024))
    hasher.update(str(size).encode("ascii"))
    return hasher.hexdigest(), partial


INDEX = DiskIndex()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        try:
            message = fmt % args
        except TypeError:
            message = fmt
        if "/api/status" in message or "/api/duplicates" in self.path:
            return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), message))

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            route = parsed.path
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            if route == "/api/health":
                return self._json(200, {"ok": True, "app": APP_NAME})
            if route == "/api/home":
                return self._json(200, {"drives": list_drives(), "shortcuts": list_shortcuts()})
            if route == "/api/status":
                return self._json(200, INDEX.status())
            if route == "/api/overview":
                payload = INDEX.overview()
                if payload is None:
                    return self._json(404, {"error": "Nothing has been scanned yet."})
                return self._json(200, payload)
            if route == "/api/folder":
                try:
                    payload = INDEX.folder_payload(query.get("path", ""), query.get("full") == "1")
                except PermissionError as exc:
                    return self._json(400, {"error": str(exc)})
                if payload is None:
                    return self._json(404, {"error": "That folder is not in this scan."})
                return self._json(200, payload)
            if route == "/api/files":
                payload = INDEX.query_files(query)
                if payload is None:
                    return self._json(404, {"error": "Nothing has been scanned yet."})
                return self._json(200, payload)
            if route == "/api/duplicates":
                return self._json(200, INDEX.duplicate_status())
            if route == "/api/export.csv":
                data = INDEX.export_csv()
                if data is None:
                    return self._json(404, {"error": "Nothing has been scanned yet."})
                return self._bytes(
                    200,
                    data,
                    "text/csv; charset=utf-8",
                    "attachment; filename=diskmap-files.csv",
                )
            if route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._static(route)
        except Exception:  # noqa: BLE001
            log_exception(self.path)
            self._json(500, {"error": "Disk Map hit an unexpected error. Details are in diskmap.log."})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            route = parsed.path
            body = self._read_json()
            if route == "/api/browse":
                path = pick_folder()
                return self._json(200, {"path": path})
            if route == "/api/scan":
                problem = INDEX.start(str(body.get("path") or ""))
                if problem:
                    return self._json(400, {"error": problem})
                return self._json(200, {"ok": True, "target": INDEX.target})
            if route == "/api/cancel":
                INDEX.request_cancel()
                return self._json(200, {"ok": True})
            if route == "/api/delete":
                paths = body.get("paths") or []
                if not isinstance(paths, list) or len(paths) > 400:
                    return self._json(400, {"error": "Choose up to 400 items at a time."})
                mode = str(body.get("mode") or "recycle")
                if body.get("dry_run"):
                    return self._json(200, INDEX.plan_delete([str(path) for path in paths]))
                if mode == "permanent" and body.get("confirm") != "DELETE":
                    return self._json(400, {"error": "Type DELETE to confirm a permanent delete."})
                return self._json(200, INDEX.delete_paths([str(path) for path in paths], mode))
            if route == "/api/reveal":
                path = clean_path(str(body.get("path") or ""))
                if not os.path.exists(to_long(path)):
                    return self._json(404, {"error": "That item is not there anymore."})
                reveal_path(path)
                return self._json(200, {"ok": True})
            if route == "/api/open":
                path = clean_path(str(body.get("path") or ""))
                if not os.path.exists(to_long(path)):
                    return self._json(404, {"error": "That item is not there anymore."})
                open_path(path)
                return self._json(200, {"ok": True})
            if route == "/api/duplicates":
                try:
                    minimum = int(body.get("min_bytes") or (20 * 1024 * 1024))
                except (TypeError, ValueError):
                    minimum = 20 * 1024 * 1024
                problem = INDEX.start_duplicates(max(0, minimum))
                if problem:
                    return self._json(400, {"error": problem})
                return self._json(200, {"ok": True})
            if route == "/api/duplicates/cancel":
                INDEX.dup_cancel = True
                return self._json(200, {"ok": True})
            if route == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._json(404, {"error": "Unknown request."})
        except Exception:  # noqa: BLE001
            log_exception(self.path)
            self._json(500, {"error": "Disk Map hit an unexpected error. Details are in diskmap.log."})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 5_000_000:
            raise ValueError("Request is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self._bytes(code, raw, "application/json; charset=utf-8")

    def _bytes(self, code: int, raw: bytes, content_type: str, disposition: str | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _static(self, route: str) -> None:
        rel = unquote(route).lstrip("/") or "index.html"
        rel = rel.replace("/", os.sep)
        full = os.path.normpath(os.path.join(STATIC, rel))
        root = os.path.normpath(STATIC)
        if os.path.normcase(full) != os.path.normcase(root) and not os.path.normcase(full).startswith(
            os.path.normcase(root) + os.sep
        ):
            self._json(403, {"error": "Forbidden."})
            return
        if not os.path.isfile(full):
            self._json(404, {"error": "Not found."})
            return
        ext = os.path.splitext(full)[1].lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as handle:
            self._bytes(200, handle.read(), mime)


def our_server_is_up(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=0.7) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("app") == APP_NAME
    except Exception:
        return False


def serve(open_browser: bool = True, port: int = DEFAULT_PORT) -> None:
    httpd = None
    chosen = port
    for candidate in range(port, port + 12):
        if our_server_is_up(candidate):
            url = f"http://{HOST}:{candidate}"
            print(f"Disk Map is already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        try:
            httpd = ThreadingHTTPServer((HOST, candidate), Handler)
            chosen = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print("Could not find a free port for Disk Map.")
        sys.exit(1)
    httpd.daemon_threads = True
    url = f"http://{HOST}:{chosen}"
    print()
    print("  Disk Map")
    print(f"  {url}")
    print("  Leave this window open while you use it. Close it to quit.")
    print(flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except OSError:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        print("Disk Map stopped.")


def run_self_test() -> None:
    import tempfile

    assert removal_problem(r"C:\pagefile.sys", r"C:\\")
    assert removal_problem(r"C:\Windows\System32\cmd.exe", r"C:\\")
    assert removal_problem(r"C:\Windows", r"C:\\")
    assert removal_problem(r"C:\Users", r"C:\\")
    assert removal_problem(r"C:\\", r"C:\\")
    assert removal_problem(r"D:\notes\a.txt", r"C:\\")
    assert removal_problem(r"C:\Users\Someone\Documents\a.txt", r"C:\\") is None
    assert "program" in removal_warning(r"C:\Program Files\Vendor\app.exe", r"C:\\").lower()

    with tempfile.TemporaryDirectory(prefix="diskmap-test-") as folder:
        nested = os.path.join(folder, "video")
        docs = os.path.join(folder, "docs")
        empty = os.path.join(folder, "empty", "nested")
        os.makedirs(nested)
        os.makedirs(docs)
        os.makedirs(empty)
        big = os.path.join(nested, "movie.bin")
        copy = os.path.join(docs, "movie-copy.bin")
        small = os.path.join(docs, "note.txt")
        payload = b"D" * (180 * 1024)
        with open(big, "wb") as handle:
            handle.write(payload)
        with open(copy, "wb") as handle:
            handle.write(payload)
        with open(small, "wb") as handle:
            handle.write(b"hello disk map")
        link = os.path.join(docs, "hardlink.bin")
        try:
            os.link(big, link)
        except OSError:
            link = ""

        index = DiskIndex()
        index.running = True
        index.started = time.time()
        index.target = folder
        index._run(folder)
        assert index.error is None, index.error
        assert index.root is not None
        copies = 3 if link else 2
        expected = 180 * 1024 * copies + len(b"hello disk map")
        assert index.root.size == expected, (index.root.size, expected)
        assert any(normkey(path).endswith(normkey("empty")) or path.endswith("empty") for path in index.empty_dirs) or any(
            "empty" in path.casefold() for path in index.empty_dirs
        )

        index.start_duplicates(1024)
        for _ in range(100):
            if not index.dup_running:
                break
            time.sleep(0.05)
        assert not index.dup_running
        assert index.dup_groups, "expected the two identical files to match"
        assert index.dup_groups[0]["partial"] is False

        victim = os.path.join(folder, "recycle-me.txt")
        with open(victim, "wb") as handle:
            handle.write(b"recycle this diskmap self test")
        # Rescan so the tree includes the new file, then recycle it.
        index2 = DiskIndex()
        index2.running = True
        index2.started = time.time()
        index2._run(folder)
        assert index2.root is not None
        before = index2.root.size
        recycle_path(victim)
        assert not os.path.exists(victim)
        index2._forget(victim, os.path.getsize(victim) if os.path.exists(victim) else len(b"recycle this diskmap self test"), "file")
        # Size was captured before delete; file is gone, so subtract the known size.
        # _forget uses the passed size. Re-read tree.
        assert index2.root.size == before - len(b"recycle this diskmap self test")

        permanent = os.path.join(folder, "permanent.txt")
        with open(permanent, "wb") as handle:
            handle.write(b"bye")
        permanent_delete(permanent)
        assert not os.path.exists(permanent)

    print("Disk Map self-test passed.")


def main() -> None:
    if "--self-test" in sys.argv:
        run_self_test()
        return
    open_browser = "--no-browser" not in sys.argv
    serve(open_browser=open_browser)


if __name__ == "__main__":
    main()
