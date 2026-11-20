"""
Auto-start the TEMS monitor when TEMS Discovery starts.

Run one copy of this script at Windows logon (a scheduled task is created by
setup_autostart.ps1).  It waits for ``DiscoveryCore.exe``; as soon as TEMS is
running it launches ``monitor.py`` hidden, and when TEMS closes it stops the
monitor it started.  Only one watcher and one monitor can run at a time
(named Windows mutexes).

Manual use:
    pythonw autostart.py        # run the watcher (no window)
    python  autostart.py --status
"""
import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent
WATCHER_MUTEX = "TemsMonitorWatcher"
MONITOR_MUTEX = "TemsMonitorSingleton"
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002
CREATE_NO_WINDOW = 0x08000000

log = logging.getLogger("tems-autostart")


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.OpenMutexW.restype = wintypes.HANDLE
k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CreateMutexW.restype = wintypes.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]


def single_instance(name):
    """True when this process owns the named mutex (no other instance)."""
    handle = k32.CreateMutexW(None, False, name)
    if not handle:
        return True, None
    already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    return not already, handle


def mutex_exists(name):
    handle = k32.OpenMutexW(SYNCHRONIZE, False, name)
    if handle:
        k32.CloseHandle(handle)
        return True
    return False


def running_processes(wanted):
    wanted = {w.lower() for w in wanted}
    found = set()
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return found
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            if name in wanted:
                found.add(entry.th32ProcessID)
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return found


def load_config():
    try:
        with open(BASE / "config.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Cannot read config.json: %s", e)
        return {}


def python_exe(cfg):
    configured = str((cfg.get("autostart", {}) or {}).get("python", "") or "")
    if configured and Path(configured).exists():
        return configured
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if console.exists() else exe)


def start_monitor(cfg):
    exe = python_exe(cfg)
    log.info("Starting monitor: %s monitor.py", exe)
    return subprocess.Popen(
        [exe, str(BASE / "monitor.py")],
        cwd=str(BASE),
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_monitor(proc):
    if proc is None or proc.poll() is not None:
        return
    log.info("TEMS closed -> stopping monitor (pid %s)", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def watch(cfg):
    settings = cfg.get("autostart", {}) or {}
    names = settings.get("process_names") or ["DiscoveryCore.exe"]
    poll = max(1.0, float(settings.get("poll_seconds", 3)))
    stop_with_tems = bool(settings.get("stop_with_tems", True))
    proc = None
    log.info("Watcher started: waiting for %s (poll %.0fs)", ", ".join(names), poll)
    while True:
        tems = running_processes(names)
        if tems:
            if (proc is None or proc.poll() is not None) and not mutex_exists(MONITOR_MUTEX):
                proc = start_monitor(cfg)
                log.info("TEMS detected (pid %s) -> monitor pid %s", sorted(tems), proc.pid)
            elif proc is not None and proc.poll() is not None:
                log.warning("Monitor exited (code %s); will retry", proc.returncode)
                proc = None
        elif stop_with_tems and proc is not None:
            stop_monitor(proc)
            proc = None
        time.sleep(poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="print state and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(BASE / "autostart.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    cfg = load_config()
    settings = cfg.get("autostart", {}) or {}
    names = settings.get("process_names") or ["DiscoveryCore.exe"]

    if args.status:
        print("TEMS pids:", sorted(running_processes(names)))
        print("monitor running:", mutex_exists(MONITOR_MUTEX))
        print("watcher running:", mutex_exists(WATCHER_MUTEX))
        return

    owned, _handle = single_instance(WATCHER_MUTEX)
    if not owned:
        log.info("Another watcher is already running; exiting.")
        return
    watch(cfg)


if __name__ == "__main__":
    main()
