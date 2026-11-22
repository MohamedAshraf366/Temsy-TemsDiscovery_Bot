"""
WhatsApp remote control for TEMS Discovery (GREEN-API).

Commands (send them in the WhatsApp chat linked to the bot):
    /start              browse from "This PC" (+ new logs found automatically)
    /recent             only the newest log folders found automatically
    1, 2, ...           open the numbered folder / select the numbered file
    ..  or /up          go up one level
    /next, /prev        next / previous page
    /yes                confirm and start the import
    /all                import all .trp files in the current folder
    /project <name>     override the inferred target project
    /dataset <name>     override the target dataset
    /status             current TEMS import status
    /cancel, /help

The import drives the real TEMS UI (Import Drive Test Data dialog), i.e. the
same steps shown in the reference video. Nothing is imported until /yes.
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import win32con
import win32gui
from pywinauto import Desktop
from pywinauto.keyboard import send_keys

import monitor

log = logging.getLogger("tems-remote")

_STATE = None
_FAILS = 0
_LAST_QUOTA = 0.0


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _tops():
    out = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                out.append((hwnd, title))
    win32gui.EnumWindows(cb, None)
    return out


def _find(prefix, timeout=0, poll=0.4):
    deadline = time.time() + timeout
    while True:
        for hwnd, title in _tops():
            if title.lower().startswith(prefix.lower()):
                return hwnd, title
        if time.time() >= deadline:
            return None, None
        time.sleep(poll)


def _close(hwnd):
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass


def _drives():
    result = []
    for letter in "CDEFGHIJK":
        p = Path(f"{letter}:\\")
        try:
            if p.exists():
                result.append(p)
        except Exception:
            pass
    return result


def _state_file():
    return Path(getattr(monitor, "BASE", Path(__file__).resolve().parent)) / "imported.json"


def _file_key(path, size, mtime):
    return f"{str(path).lower()}|{size}|{int(mtime)}"


def _load_imported():
    try:
        return set(json.loads(_state_file().read_text(encoding="utf-8")))
    except Exception:
        return set()


def _mark_imported(files):
    keys = _load_imported()
    for f in files:
        try:
            st = Path(f).stat()
            keys.add(_file_key(f, st.st_size, st.st_mtime))
        except OSError:
            pass
    try:
        _state_file().write_text(json.dumps(sorted(keys)), encoding="utf-8")
    except Exception as e:
        log.warning("Cannot save imported.json: %s", e)


_SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata",
              "$recycle.bin", "system volume information", "node_modules", ".git",
              "__pycache__", "site-packages"}


def _scan_recent(remote_cfg, now=None):
    """Find folders holding .trp files that are recent and not imported yet.

    Returns [(Path, new_file_count, newest_mtime)], newest first.
    """
    now = now or time.time()
    home = Path.home()
    roots = list(remote_cfg.get("search_roots") or [])
    if not roots:
        roots = [remote_cfg.get("root", r"D:\WORK"), home / "Desktop", home / "Downloads",
                 home / "Documents"]
    days = float(remote_cfg.get("recent_days", 7))
    max_depth = int(remote_cfg.get("scan_depth", 4))
    limit = int(remote_cfg.get("recent_count", 3))
    deadline = now + float(remote_cfg.get("scan_seconds", 8))
    imported = _load_imported()
    found = {}

    def walk(folder, depth):
        if time.time() > deadline:
            return
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth and e.name.lower() not in _SKIP_DIRS \
                            and not e.name.startswith("."):
                        walk(e.path, depth + 1)
                elif e.name.lower().endswith(".trp") and e.is_file(follow_symlinks=False):
                    st = e.stat()
                    if days and now - st.st_mtime > days * 86400:
                        continue
                    if _file_key(e.path, st.st_size, st.st_mtime) in imported:
                        continue
                    count, newest = found.get(folder, (0, 0))
                    found[folder] = (count + 1, max(newest, st.st_mtime))
            except OSError:
                continue

    seen = set()
    for r in roots:
        r = Path(r)
        if str(r).lower() in seen:
            continue
        seen.add(str(r).lower())
        try:
            if r.exists():
                walk(str(r), 0)
        except OSError:
            pass
    ranked = sorted(found.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    return [(Path(folder), count, newest) for folder, (count, newest) in ranked]


def _mask_chat(chat):
    d = _digits(chat)
    return f"...{d[-4:]}" if len(d) > 4 else d


def _entries(folder, max_files=60):
    """[(kind, path)] - folders first, then .trp files."""
    dirs, files = [], []
    try:
        for child in folder.iterdir():
            name = child.name
            if name.startswith(".") or name.lower() in ("system volume information", "$recycle.bin"):
                continue
            try:
                if child.is_dir():
                    dirs.append(child)
                elif child.suffix.lower() == ".trp":
                    files.append(child)
            except Exception:
                pass
    except Exception as e:
        log.warning("Cannot list %s: %s", folder, e)
    dirs.sort(key=lambda p: p.name.lower())
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return [("dir", p) for p in dirs] + [("file", p) for p in files[:max_files]]


class Session:
    def __init__(self, cfg):
        self.cfg = cfg
        self.remote = cfg.get("remote", {}) or {}
        self.phone = _digits((cfg.get("whatsapp", {}) or {}).get("phone"))
        self.last_poll = 0.0
        self.mode = None          # "browse" | "confirm"
        self.path = None
        self.page = 0
        self.entries = []
        self.files = []
        self.import_limit = 0
        self.project = ""
        self.dataset = str(self.remote.get("dataset", "") or "")
        self._reply_chat = None
        self.recent_info = {}

    # ------------------------------------------------------------ messaging
    def say(self, text):
        try:
            chat = self._reply_chat
            if chat and _digits(chat) != self.phone:
                r = _request(self.cfg, "post", "sendMessage", payload={
                    "chatId": chat, "message": text,
                }, timeout=15)
                ok = r is not None and r.status_code == 200
                if ok:
                    try:
                        log.info("GREEN-API reply: %s", r.text[:120])
                    except Exception:
                        pass
            else:
                ok = monitor.notify(self.cfg, text)
            if not ok:
                log.warning("Could not send remote reply")
        except Exception as e:
            log.exception("Remote reply error: %s", e)

    def reset(self):
        self.mode = self.path = None
        self.entries, self.files, self.page = [], [], 0
        self.import_limit = 0
        self.recent_info = {}
        self.project = ""
        self.dataset = str(self.remote.get("dataset", "") or "")

    def help(self):
        self.say(
            "🤖 أوامر Temsy:\n"
            "/start - ابدأ التصفح من This PC (+ أحدث لوجات لقيتها)\n"
            "/recent - أحدث فولدرات فيها لوجات جديدة بس\n"
            "رقم - افتح المجلد / اختر الملف\n"
            ".. للرجوع • /next • /prev\n"
            "/yes تأكيد الاستيراد • /all كل الملفات • /all 20 آخر 20 ملف\n"
            "/project <name> • /dataset <name>\n"
            "/status • /cancel • /help"
        )

    # ----------------------------------------------------------- navigation
    def listing(self):
        page_size = int(self.remote.get("page_size", 18))
        title = str(self.path) if self.path else "💻 This PC"
        start = self.page * page_size
        chunk = self.entries[start:start + page_size]
        lines = [f"📂 {title}"]
        for i, (kind, p) in enumerate(chunk, start + 1):
            if kind == "recent":
                count, newest = self.recent_info.get(str(p), (0, 0))
                when = datetime.fromtimestamp(newest).strftime("%d %b %H:%M") if newest else ""
                lines.append(f"{i}) ⭐ {p} — {count} جديد {when}".rstrip())
                continue
            label = str(p) if (kind == "dir" and p.parent == p) else p.name
            lines.append(f"{i}) {'📁' if kind == 'dir' else '📄'} {label}")
        if not chunk:
            lines.append("(فاضي)")
        if any(k == "recent" for k, _ in chunk):
            lines.append("⭐ = لوجات جديدة لقيتها أوتوماتيك (لسه ما اتعملهاش import)")
        if len(self.entries) > start + page_size:
            lines.append("/next للصفحة التالية")
        if self.page:
            lines.append("/prev للصفحة السابقة")
        if self.path:
            lines.append(".. للأعلى")
        lines.append("/help للأوامر")
        try:
            files = list(self.path.glob("*.trp")) if self.path else []
        except Exception:
            files = []
        if files:
            lines.append(f"📄 {len(files)} ملف .trp هنا — /yes أو /all")
        self.say("\n".join(lines))

    def home(self):
        self.reset()
        self.mode = "browse"
        try:
            recents = _scan_recent(self.remote)
        except Exception as e:
            log.warning("Recent scan failed: %s", e)
            recents = []
        self.recent_info = {str(p): (n, t) for p, n, t in recents}
        self.entries = [("dir", p) for p in _drives()] + [("recent", p) for p, _, _ in recents]
        self.listing()

    def recent(self):
        self.reset()
        self.mode = "browse"
        try:
            recents = _scan_recent(self.remote)
        except Exception as e:
            log.warning("Recent scan failed: %s", e)
            recents = []
        if not recents:
            self.say("🔎 مالقيتش لوجات .trp جديدة. اكتب /start وتصفح بنفسك.")
            self.reset()
            return
        self.recent_info = {str(p): (n, t) for p, n, t in recents}
        self.entries = [("recent", p) for p, _, _ in recents]
        self.listing()

    def up(self):
        if self.mode != "browse" or not self.path:
            self.home()
            return
        parent = self.path.parent
        self.path = parent if str(parent) != str(self.path) else None
        if self.path is None:
            self.home()
            return
        self.page = 0
        self.entries = [("dir", p) for p in _drives()] if self.path is None else _entries(
            self.path, int(self.remote.get("page_size", 18)) + int(self.remote.get("max_files", 60))
        )
        self.listing()

    def turn_page(self, delta):
        if self.mode != "browse":
            return
        size = int(self.remote.get("page_size", 18))
        new = max(0, self.page + delta)
        if new * size < len(self.entries):
            self.page = new
        self.listing()

    def open(self, target):
        if not target.exists():
            self.say("المجلد مش موجود")
            return
        self.path = target
        self.page = 0
        self.entries = _entries(target, int(self.remote.get("max_files", 60)))
        self.listing()

    def by_name(self, text):
        wanted = text.strip().lower().rstrip("\\:")
        for kind, p in self.entries:
            label = str(p) if (kind == "recent" or (kind == "dir" and p.parent == p)) else p.name
            if label.lower().rstrip("\\:") == wanted:
                return kind, p
        return None

    def choose(self, index):
        if self.mode != "browse" or not (1 <= index <= len(self.entries)):
            self.say("اختيار غير صحيح، اكتب /help")
            return
        pos = index - 1
        if pos >= len(self.entries):
            self.say("اختيار غير صحيح")
            return
        kind, target = self.entries[pos]
        if kind in ("dir", "recent"):
            self.open(target)
        else:
            self.files = [target]
            self.summary()

    def infer_project(self):
        if self.project:
            return self.project
        if not self.path:
            return ""
        root = str(self.remote.get("root", r"D:\WORK")).rstrip("\\/")
        here = str(self.path)
        if here.lower().startswith(root.lower() + "\\"):
            rest = here[len(root) + 1:].split("\\")
            if rest and rest[0]:
                return rest[0]
        return ""

    def collect_files(self):
        if self.files:
            return self.files
        if not self.path:
            return []
        limit = self.import_limit or int(self.remote.get("max_files", 100))
        try:
            all_files = sorted(self.path.glob("*.trp"),
                               key=lambda p: p.stat().st_mtime if p.exists() else 0,
                               reverse=True)
        except Exception:
            all_files = []
        return all_files[:limit]

    def summary(self):
        files = self.collect_files()
        if not files:
            self.say("مفيش ملفات .trp في المجلد ده")
            return
        self.mode = "confirm"
        lines = ["📦 جاهز للاستيراد:"]
        if len(files) == 1:
            lines.append(f"📄 {files[0].name}")
        else:
            lines.append(f"📄 {len(files)} ملف .trp")
        try:
            total_here = len(list(self.path.glob("*.trp"))) if self.path else len(files)
        except Exception:
            total_here = len(files)
        if total_here > len(files):
            lines.append(f"(من أصل {total_here} ملف في الفولدر)")
        lines.append(f"📂 {self.path}")
        project = self.infer_project()
        lines.append(f"🎯 Project: {project or 'زي ما هو في TEMS'}")
        lines.append(f"🎯 DataSet: {self.dataset or 'زي ما هو في TEMS'}")
        lines.append("اكتب /yes للتأكيد أو /cancel")
        if project or self.dataset:
            lines.append("تقدر تغيّرهم بـ /project <name> و /dataset <name>")
        self.say("\n".join(lines))

    # ----------------------------------------------------------- TEMS stuff
    def status(self):
        try:
            win = monitor.find_window(self.cfg)
            if win is None:
                self.say("TEMS مش مفتوح دلوقتي يا قائد")
                return
            st, _ = monitor.read_status(win, self.cfg)
            if not st:
                self.say("مش قادر أقرأ حالة الاستيراد")
                return
            self.say(
                f"📊 الحالة: Running {st['running']} | Total {st['total']} | "
                f"Completed {st['completed']} | Failed {st['failed']}"
            )
        except Exception as e:
            log.exception("Status error: %s", e)
            self.say("حصلت مشكلة وأنا بقرأ الحالة")

    def confirm(self):
        if self.mode != "confirm":
            self.say("ابدأ بـ /start الأول")
            return
        files = self.collect_files()
        if not files:
            self.say("مفيش ملفات .trp")
            return
        try:
            perform_import(self.cfg, files, self.infer_project(), self.dataset)
            _mark_imported(files)
            self.say(f"🚀 بدأ الاستيراد: {len(files)} ملف. Temsy تحت أمرك يا قائد 👑")
        except Exception as e:
            log.exception("Import error: %s", e)
            self.say(f"⚠️ الاستيراد ما ابتداش: {str(e)[:160]}")
        self.reset()

    # -------------------------------------------------------------- routing
    def handle(self, text):
        t = (text or "").strip()
        low = t.lower()
        if not low.startswith("/"):
            if self.mode == "browse":
                if re.fullmatch(r"\d+", t):
                    self.choose(int(t))
                    return
                picked = self.by_name(t)
                if picked:
                    kind, target = picked
                    if kind in ("dir", "recent"):
                        self.open(target)
                    else:
                        self.files = [target]
                        self.summary()
                    return
            self.say("مش فاهم يا قائد… اكتب /help")
            return
        if low in ("/start", "/ابدأ"):
            self.home()
        elif low in ("/help", "/?"):
            self.help()
        elif low in ("/status",):
            self.status()
        elif low in ("/recent", "/حديث"):
            self.recent()
        elif low in ("/cancel", "/الغاء"):
            self.reset()
            self.say("تم الإلغاء يا قائد")
        elif low in ("/yes", "/تم"):
            self.confirm()
        elif low == "/all" or low.startswith("/all ") or low.startswith("/كل "):
            parts = t.split()
            self.import_limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            self.files = []
            self.summary()
        elif low in ("/up", "..", "/.."):
            self.up()
        elif low in ("/next",):
            self.turn_page(1)
        elif low in ("/prev",):
            self.turn_page(-1)
        elif low.startswith("/project "):
            self.project = t.split(None, 1)[1].strip()
            self.say(f"🎯 Project = {self.project}")
            if self.mode == "confirm":
                self.summary()
        elif low.startswith("/dataset "):
            self.dataset = t.split(None, 1)[1].strip()
            self.say(f"🎯 DataSet = {self.dataset}")
            if self.mode == "confirm":
                self.summary()
        else:
            self.say("مش فاهم الأمر… اكتب /help")


# ---------------------------------------------------------------- GREEN-API
def _api(cfg):
    w = cfg.get("whatsapp", {}) or {}
    return str(w.get("api_url", "")).rstrip("/"), str(w.get("id_instance", "")), str(w.get("api_token", ""))


def _request(cfg, method_name, endpoint, receipt="", payload=None, timeout=8):
    """Call GREEN-API, retrying the generic host when the instance host times out."""
    base, iid, token = _api(cfg)
    if not (base and iid and token):
        return None
    fallback = str((cfg.get("whatsapp", {}) or {}).get(
        "fallback_api_url", "https://api.greenapi.com")).rstrip("/")
    hosts = [base] + ([fallback] if fallback and fallback != base else [])
    suffix = f"/{receipt}" if receipt else ""
    url_tail = f"/waInstance{iid}/{endpoint}/{token}{suffix}"
    last = None
    for host in hosts:
        try:
            fn = getattr(requests, method_name)
            kwargs = {"timeout": timeout, "verify": monitor._verify(cfg)}
            if payload is not None:
                kwargs["json"] = payload
            return fn(f"{host}{url_tail}", **kwargs)
        except Exception as e:
            last = e
            log.debug("GREEN-API %s via %s failed: %s", endpoint, host, e)
    if last is not None:
        raise last
    return None


def _receive(cfg):
    r = _request(cfg, "get", "receiveNotification", timeout=8)
    if r is None or r.status_code != 200 or not r.text.strip():
        return None
    try:
        return r.json()
    except Exception:
        return None


def _delete(cfg, receipt):
    if not receipt:
        return
    try:
        _request(cfg, "delete", "deleteNotification", receipt=receipt, timeout=8)
    except Exception as e:
        log.warning("Cannot delete notification: %s", e)


def _message_text(body):
    data = body.get("messageData", {}) or {}
    if data.get("typeMessage") == "textMessage":
        return (data.get("textMessageData", {}) or {}).get("textMessage", "")
    if data.get("typeMessage") == "extendedTextMessage":
        return (data.get("extendedTextMessageData", {}) or {}).get("text", "")
    return ""


def _on_quota(cfg, body):
    """GREEN-API free plan allows 3 chats per month; explain it once an hour."""
    global _LAST_QUOTA
    if time.time() - _LAST_QUOTA < 3600:
        return
    _LAST_QUOTA = time.time()
    data = body.get("quotaData", {}) or {}
    desc = str(data.get("description", ""))
    allowed_chats = re.findall(r"\d{8,}@c\.us", desc)
    mine = _digits((cfg.get("whatsapp", {}) or {}).get("phone"))
    listed = ", ".join(_mask_chat(c) for c in allowed_chats) or "unknown"
    ok = any(_digits(c) == mine for c in allowed_chats) if allowed_chats else None
    log.warning("GREEN-API Developer plan quota reached (%s/%s chats). Allowed chats: %s. "
                "Your number allowed: %s", data.get("used"), data.get("total"), listed, ok)
    text = ("⚠️ GREEN-API free plan: monthly limit of 3 chats reached. Messages from other chats "
            "are not delivered until the quota resets (or the plan is upgraded).\n"
            f"Allowed chats: {listed}\n"
            + ("Your number is NOT among them, so WhatsApp commands will not arrive.\n" if ok is False else "")
            + "Run: python remote.py --green-check")
    token = str(cfg.get("telegram_token", ""))
    if token and "PUT_" not in token and hasattr(monitor, "send_telegram"):
        try:
            monitor.send_telegram(cfg, text)
        except Exception as e:
            log.warning("Quota alert via Telegram failed: %s", e)


def _handle_notification(cfg, remote_cfg, body, kind, stale):
    if kind == "quotaExceeded":
        _on_quota(cfg, body)
        return
    # Only real chat messages. This also drops outgoingAPIMessageReceived,
    # i.e. our own replies, so they can never be treated as commands.
    if kind not in ("incomingMessageReceived", "outgoingMessageReceived"):
        log.debug("Ignored notification type %s", kind)
        return
    ts = body.get("timestamp")
    if ts:
        age = time.time() - float(ts)
        if age > stale:
            log.info("Ignored stale %s (%d s old)", kind, age)
            return
    text = (_message_text(body) or "").strip()
    if not text or len(text) > 160:
        return
    sender = body.get("senderData", {}) or {}
    chat = str(sender.get("chatId") or "")
    allowed = set(filter(None, [
        _digits((cfg.get("whatsapp", {}) or {}).get("phone")),
        *[_digits(x) for x in (remote_cfg.get("allowed_phones") or [])],
    ]))
    # Commands are accepted ONLY inside a private chat with an allowed number.
    # (Never in groups, and never in someone else's chat just because the
    # sender happens to be you.)
    if not allowed or not chat.endswith("@c.us") or _digits(chat) not in allowed:
        if text.startswith("/"):
            log.warning("Ignored command from chat %s (not an allowed private chat)", _mask_chat(chat))
        return
    if kind == "outgoingMessageReceived":
        # Typed on the linked phone (the self-chat): explicit commands only.
        if not (text.startswith("/") or re.fullmatch(r"\d{1,3}|\.\.", text)):
            return
    log.info("Remote command [%s]: %s", kind, text[:80])
    _STATE._reply_chat = chat
    _STATE.handle(text)


def poll(cfg):
    global _STATE, _FAILS
    remote_cfg = cfg.get("remote", {}) or {}
    if not remote_cfg.get("enabled", False):
        return
    if _STATE is None:
        _STATE = Session(cfg)
    interval = max(1.0, float(remote_cfg.get("poll_seconds", 3)))
    if time.time() - _STATE.last_poll < interval:
        return
    _STATE.last_poll = time.time()
    stale = float(remote_cfg.get("stale_seconds", 180))
    try:
        for _ in range(10):
            notification = _receive(cfg)
            if not notification:
                break
            receipt = notification.get("receiptId")
            body = notification.get("body", {}) or {}
            kind = body.get("typeWebhook", "?")
            try:
                _handle_notification(cfg, remote_cfg, body, kind, stale)
            finally:
                _delete(cfg, receipt)
    except Exception as e:
        _FAILS += 1
        if _FAILS == 1 or _FAILS % 10 == 0:
            log.warning("Remote poll error (%d in a row): %s", _FAILS, _mask_secret(cfg, e))
    else:
        _FAILS = 0


def _mask_secret(cfg, value):
    token = str((cfg.get("whatsapp", {}) or {}).get("api_token", ""))
    text = str(value)
    return text.replace(token, "***") if token else text


# ------------------------------------------------------------- import steps
def _set_combo(dlg, auto_id, value):
    """Choose an item from a WinForms combo box (returns False if not found)."""
    combo = dlg.child_window(auto_id=auto_id, control_type="ComboBox")
    combo.child_window(control_type="Button").click_input()  # dropdown arrow
    time.sleep(0.6)
    found = False
    try:
        for item in dlg.descendants(control_type="ListItem"):
            if item.window_text().strip().lower() == value.strip().lower():
                item.click_input()
                found = True
                break
    finally:
        if not found:
            send_keys("{ESC}")
            time.sleep(0.3)
    return found


def perform_import(cfg, files, project="", dataset="", dry_run=False):
    """Drive the real TEMS 'Import Drive Test Data' dialog. Returns when started."""
    if _find("import drive test data", timeout=0) [0]:
        raise RuntimeError("في نافذة استيراد مفتوحة أصلاً — اقفلها وجرب")

    app = Desktop(backend="uia")
    win = app.window(title_re=cfg.get("window_title_regex", "TEMS Discovery.*"))
    if not win.exists():
        raise RuntimeError("TEMS Discovery مش مفتوح")
    win.set_focus()
    time.sleep(0.4)

    opened = []
    try:
        win.child_window(title="Import Drive Test Data", control_type="Button").click_input()
        hwnd, title = _find("import drive test data", timeout=25)
        if not hwnd:
            raise RuntimeError("نافذة الاستيراد ما فتحتش")
        opened.append(hwnd)
        dlg = app.window(handle=hwnd)

        dlg.child_window(title_re="Select Data Format.*", control_type="Button").click_input()
        fhwnd, _ = _find("select data format", timeout=25)
        if not fhwnd:
            raise RuntimeError("نافذة اختيار الملفات ما فتحتش")
        opened.append(fhwnd)
        fd = app.window(handle=fhwnd)
        fd.child_window(auto_id="1148", control_type="Edit").set_edit_text(
            " ".join(f'"{f}"' for f in files)
        )
        time.sleep(0.3)
        fd.child_window(auto_id="1", control_type="Button").click_input()
        time.sleep(2.5)

        names = [li.window_text() for li in dlg.descendants(control_type="ListItem")]
        if len(names) < len(files):
            raise RuntimeError(f"التEMS اضاف {len(names)} ملف بس من {len(files)}")

        if project and not _set_combo(dlg, "comboBoxDriveTestDataMainPageProject", project):
            raise RuntimeError(f"المشروع '{project}' مش موجود في TEMS")
        if dataset and not _set_combo(dlg, "comboBoxDriveTestDataMainPageDataSet", dataset):
            raise RuntimeError(f"الـ DataSet '{dataset}' مش موجود في TEMS")

        if dry_run:
            log.info("Dry run: files added, import NOT started")
            return

        dlg.child_window(auto_id="buttonImport", control_type="Button").click_input()
        end = time.time() + 20
        while time.time() < end and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
            time.sleep(0.5)
        if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
            raise RuntimeError("الاستيراد ما ابتداش — راجع رسائل TEMS")
        opened.clear()
    finally:
        for h in opened:
            if win32gui.IsWindow(h):
                _close(h)


# --------------------------------------------------------------- diagnostics
def green_check(cfg, fix=False):
    """python remote.py --green-check [--fix]: inspect the GREEN-API instance."""
    phone = _digits((cfg.get("whatsapp", {}) or {}).get("phone"))

    def call(method, endpoint, payload=None):
        r = _request(cfg, method, endpoint, payload=payload, timeout=25)
        if r is None:
            print("GREEN-API is not configured (whatsapp.id_instance / api_token / api_url).")
            return None
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:200]

    state = call("get", "getStateInstance")
    settings = call("get", "getSettings")
    if not state or not settings:
        return
    print("Instance state :", state[1])
    s = settings[1] if isinstance(settings[1], dict) else {}
    wid = str(s.get("wid", ""))
    for key in ("incomingWebhook", "outgoingMessageWebhook", "outgoingAPIMessageWebhook",
                "outgoingWebhook", "stateWebhook"):
        print(f"{key:27}:", s.get(key))
    self_mode = bool(wid) and _digits(wid) == phone
    print("Linked account  : ...%s   | whatsapp.phone: ...%s" % (_digits(wid)[-4:], phone[-4:]))
    if self_mode:
        print("Mode: SELF-CHAT. The linked WhatsApp is your own number; type commands in 'Message yourself'.")
        wanted = {"incomingWebhook": "no", "outgoingMessageWebhook": "yes",
                  "outgoingAPIMessageWebhook": "no", "outgoingWebhook": "no", "stateWebhook": "yes"}
        print("Recommended: incomingWebhook=no, so your other chats stop using the free 3-chat quota.")
    else:
        print("Mode: BOT ACCOUNT. You message the linked number from your own phone.")
        wanted = {"incomingWebhook": "yes", "outgoingMessageWebhook": "no",
                  "outgoingAPIMessageWebhook": "no", "outgoingWebhook": "no", "stateWebhook": "yes"}
    diff = {k: v for k, v in wanted.items() if str(s.get(k)).lower() != v}
    if not diff:
        print("Settings already match the recommendation.")
        return
    print("Differences    :", diff)
    if not fix:
        print("Run again with --fix to apply them (takes up to ~5 minutes to take effect).")
        return
    print("setSettings    :", call("post", "setSettings", payload=wanted))
    print("Applied. Wait a few minutes, then restart monitor.py.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--green-check" in sys.argv:
        green_check(monitor.load_config(), fix="--fix" in sys.argv)
    else:
        print(__doc__)