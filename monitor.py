"""
TEMS Discovery import monitor -> Telegram notifications.

Reads the Task Window summary line of TEMS Discovery through Windows UI
Automation, e.g.:
    "2 Running; Total 2; 0 Completed; 0 Aborted/Failed; 0 Re-Import"
and sends a Telegram message when an import starts, finishes, or fails.

Usage:
    python monitor.py              # run the monitor
    python monitor.py --test       # send a test Telegram message
    python monitor.py --chatid     # print chat ids that messaged your bot
    python monitor.py --dump       # print the texts the script can read from TEMS
"""
import argparse
import asyncio
import json
import logging
import os
import re
import socket
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests

try:  # use the Windows certificate store (needed behind SSL-inspecting proxies / antivirus)
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

STATUS_RE = re.compile(
    r"(\d+)\s+Running;\s*(?:Total\s+(\d+);\s*)?(\d+)\s+Completed;\s*(\d+)\s+Aborted/Failed",
    re.IGNORECASE,
)
ELAPSED_RE = re.compile(
    r"T\w{0,3}\s*elapsed:.{0,80}?(?=\s*(?:Imported|Running;|$))",
    re.IGNORECASE | re.DOTALL,
)

log = logging.getLogger("tems-monitor")
_OCR_ENGINE = None
_CFG = {}


# ----------------------------------------------------------------- config
# Personal data (numbers, tokens, paths) lives in .env so config.json can be
# committed safely.  Environment variables win over the .env file, and the
# .env file wins over config.json.
ENV_OVERRIDES = [
    ("TELEGRAM_TOKEN", ("telegram_token",)),
    ("TELEGRAM_CHAT_ID", ("telegram_chat_id",)),
    ("DISPLAY_NAME", ("display_name",)),
    ("SENDER_TITLE", ("sender_title",)),
    ("CHANNEL", ("channel",)),
    ("WHATSAPP_PROVIDER", ("whatsapp", "provider")),
    ("WHATSAPP_PHONE", ("whatsapp", "phone")),
    ("WHATSAPP_ID_INSTANCE", ("whatsapp", "id_instance")),
    ("WHATSAPP_API_TOKEN", ("whatsapp", "api_token")),
    ("WHATSAPP_API_URL", ("whatsapp", "api_url")),
    ("WHATSAPP_FALLBACK_API_URL", ("whatsapp", "fallback_api_url")),
    ("WHATSAPP_CHAT_ID", ("whatsapp", "chat_id")),
    ("REMOTE_ROOT", ("remote", "root")),
    ("REMOTE_ALLOWED_PHONES", ("remote", "allowed_phones")),
    ("REMOTE_ENABLED", ("remote", "enabled")),
]
ENV_KEYS = {name for name, _ in ENV_OVERRIDES}


def _set_path(cfg, path, value):
    node = cfg
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _apply_env(cfg):
    """Merge .env (then real environment variables) into the config."""
    env = _load_env_file(BASE / ".env")
    env.update({k: v for k, v in os.environ.items() if k in ENV_KEYS})
    for name, path in ENV_OVERRIDES:
        value = env.get(name)
        if value is None or str(value).strip() == "":
            continue
        if path == ("remote", "allowed_phones"):
            value = [p.strip() for p in str(value).replace(";", ",").split(",") if p.strip()]
        elif path == ("remote", "enabled"):
            value = str(value).strip().lower() in ("1", "true", "yes", "on")
        _set_path(cfg, path, str(value).strip() if not isinstance(value, list) else value)

    wa = cfg.get("whatsapp")
    if isinstance(wa, dict):
        instance = str(wa.get("id_instance", "") or "").strip()
        if instance and not str(wa.get("api_url", "") or "").strip():
            # 123456789012 -> https://1234.api.greenapi.com
            wa["api_url"] = f"https://{instance[:4]}.api.greenapi.com"
        if not str(wa.get("fallback_api_url", "") or "").strip():
            wa["fallback_api_url"] = "https://api.greenapi.com"
    return cfg


def load_config():
    path = BASE / "config.json"
    example = BASE / "config.example.json"
    if not path.exists() and not example.exists():
        sys.exit(f"config.json not found next to the script: {path}")
    source = path if path.exists() else example
    with open(source, encoding="utf-8") as f:
        cfg = json.load(f)
    if source != path:
        log.warning("config.json not found; using %s + .env", example.name)
    cfg = _apply_env(cfg)
    global _CFG
    _CFG = cfg
    return cfg


# --------------------------------------------------------------- telegram
def _verify(cfg):
    """TLS verification setting: True, False (insecure), or a CA bundle path."""
    if cfg.get("verify_ssl", True) is False:
        import urllib3

        urllib3.disable_warnings()
        return False
    return cfg.get("ca_bundle") or True


def send_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    payload = {"chat_id": cfg["telegram_chat_id"], "text": text}
    for attempt in range(1, 4):
        try:
            r = requests.post(url, data=payload, timeout=15, verify=_verify(cfg))
            if r.ok:
                return True
            log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram error (try %s): %s", attempt, _mask(e, cfg.get("telegram_token")))
            if isinstance(e, requests.exceptions.SSLError) and attempt == 1:
                log.warning("SSL certificate problem (proxy/antivirus?). Fix: pip install truststore, "
                            "or set \"ca_bundle\" / \"verify_ssl\": false in config.json.")
        time.sleep(2 * attempt)
    log.error("Could not deliver message: %s", text)
    return False


def tg_chat_ids(cfg):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/getUpdates"
    data = requests.get(url, timeout=15, verify=_verify(cfg)).json()
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not seen:
        print("No chats found. Send any message to your bot first, then run again.")
    for cid, name in seen.items():
        print(f"chat_id = {cid}   ({name})")


# --------------------------------------------------------------- whatsapp
def _enc_len(text):
    return len(urllib.parse.quote(text, safe=""))


def _shorten(text, limit):
    """CallMeBot takes the text in the URL, and Arabic is ~6 chars per letter once encoded,
    so a long letter is cut down to: basmala + salawat + the English detail lines."""
    if _enc_len(text) <= limit:
        return text
    lines = [l for l in text.split("\n") if l.strip()]
    details = [l for l in lines if sum(c.isascii() for c in l) > len(l) / 2]
    head = [l for l in lines[:2] if l not in details]
    for parts in (head + details, head[:1] + details, details):
        candidate = "\n".join(parts)
        if candidate and _enc_len(candidate) <= limit:
            return candidate
    candidate = "\n".join(details) or text
    while candidate and _enc_len(candidate) > limit:
        candidate = candidate[:-5]
    return candidate


def send_callmebot(cfg, text):
    wa = cfg.get("whatsapp", {})
    if not wa.get("phone") or not wa.get("apikey"):
        log.error("whatsapp.phone / whatsapp.apikey missing in config.json")
        return False
    body = _shorten(text, int(wa.get("max_encoded_chars", 900)))
    params = {"phone": wa["phone"], "apikey": wa["apikey"], "text": body}
    for attempt in range(1, 4):
        try:
            r = requests.get("https://api.callmebot.com/whatsapp.php", params=params,
                             timeout=30, verify=_verify(cfg))
            snippet = " ".join(r.text.split())[:200]
            bad = any(w in snippet.lower() for w in ("error", "invalid", "not found", "limit"))
            if r.ok and not bad:
                log.info("CallMeBot: %s", snippet)
                return True
            log.warning("CallMeBot HTTP %s (try %s): %s", r.status_code, attempt, snippet)
        except requests.RequestException as e:
            log.warning("CallMeBot error (try %s): %s", attempt, e)
        time.sleep(5 * attempt)
    log.error("Could not deliver WhatsApp message: %s", text[:120])
    return False


def _mask(message, *secrets):
    message = str(message)
    for secret in secrets:
        if secret:
            message = message.replace(str(secret), "***")
    return message


def send_greenapi(cfg, text):
    """GREEN-API (free Developer plan): link your WhatsApp by QR, then POST JSON (no URL-length limit)."""
    wa = cfg.get("whatsapp", {})
    chat_id = wa.get("chat_id") or ("".join(c for c in wa.get("phone", "") if c.isdigit()) + "@c.us")
    if chat_id == "@c.us" or not wa.get("id_instance") or not wa.get("api_token"):
        log.error("whatsapp.phone / id_instance / api_token missing in config.json")
        return False
    base = wa.get("api_url", "https://api.greenapi.com").rstrip("/")
    fallback = str(wa.get("fallback_api_url", "") or "").rstrip("/")
    hosts = [base] + ([fallback] if fallback and fallback != base else [])
    token = wa["api_token"]
    for attempt in range(1, 4):
        for host in hosts:
            url = f"{host}/waInstance{wa['id_instance']}/sendMessage/{token}"
            try:
                r = requests.post(url, json={"chatId": chat_id, "message": text},
                                  timeout=20, verify=_verify(cfg))
                if r.ok:
                    log.info("GREEN-API: %s", " ".join(r.text.split())[:120])
                    return True
                log.warning("GREEN-API HTTP %s via %s (try %s): %s",
                            r.status_code, host, attempt, _mask(r.text[:200], token))
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    break  # wrong credentials / quota / not authorized: retrying will not help
            except requests.RequestException as e:
                log.warning("GREEN-API error via %s (try %s): %s", host, attempt, _mask(e, token))
        time.sleep(3 * attempt)
    log.error("Could not deliver WhatsApp message via GREEN-API")
    return False


def send_waha(cfg, text):
    wa = cfg.get("whatsapp", {})
    chat_id = wa.get("waha_chat_id") or (
        "".join(c for c in wa.get("phone", "") if c.isdigit()) + "@c.us"
    )
    if chat_id == "@c.us":
        log.error("whatsapp.phone (or waha_chat_id) missing in config.json")
        return False
    url = wa.get("waha_url", "http://localhost:3000").rstrip("/") + "/api/sendText"
    headers = {"Content-Type": "application/json"}
    if wa.get("waha_api_key"):
        headers["X-Api-Key"] = wa["waha_api_key"]
    payload = {"session": wa.get("waha_session", "default"), "chatId": chat_id, "text": text}
    sess = requests.Session()
    sess.trust_env = False  # talk to localhost directly, ignore system proxy
    for attempt in range(1, 4):
        try:
            r = sess.post(url, json=payload, headers=headers, timeout=30)
            if r.ok:
                return True
            log.warning("WAHA HTTP %s (try %s): %s", r.status_code, attempt, r.text[:200])
        except requests.RequestException as e:
            log.warning("WAHA error (try %s): %s", attempt, e)
        time.sleep(3 * attempt)
    log.error("Could not deliver WhatsApp message via WAHA")
    return False


def notify(cfg, text):
    """Send through the channel(s) chosen in config.json: telegram | whatsapp | both."""
    channel = str(cfg.get("channel", "telegram")).lower()
    ok = False
    if channel in ("telegram", "both"):
        ok = send_telegram(cfg, text) or ok
    if channel in ("whatsapp", "both"):
        provider = str(cfg.get("whatsapp", {}).get("provider", "callmebot")).lower()
        sender = {"waha": send_waha, "greenapi": send_greenapi}.get(provider, send_callmebot)
        ok = sender(cfg, text) or ok
    return ok


# ------------------------------------------------------------ TEMS reading
def find_window(cfg):
    """win32 backend: UIA ToolbarWrapper can die mid-enumeration."""
    from pywinauto import Desktop

    try:
        wins = Desktop(backend="win32").windows(
            title_re=cfg["window_title_regex"], visible_only=False
        )
        return wins[0] if wins else None
    except Exception as e:
        log.debug("find_window error: %s", e)
        return None


def read_texts(win):
    texts = []
    try:
        elements = win.descendants()
    except Exception as e:
        log.debug("read_texts enumeration error: %s", e)
        return texts
    for el in elements:
        try:
            t = el.window_text()
        except Exception:
            continue
        if t:
            texts.append(t)
    return texts


def normalize_ocr(text):
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\bTota[I1l]\b", "Total", text, flags=re.I)
    text = re.sub(r"\bComp[I1l]eted\b", "Completed", text, flags=re.I)
    text = re.sub(r"\bFai[I1l]ed\b", "Failed", text, flags=re.I)
    text = re.sub(r"\bR[e3][-\s]?[lI1]mport\b", "Re-Import", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z0-9])O(?![A-Za-z])", "0", text)
    text = re.sub(r"(?<![A-Za-z0-9])[Il](?![A-Za-z])", "1", text)
    return text


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        import winrt.windows.foundation  # noqa: F401

        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        try:
            _OCR_ENGINE = OcrEngine.try_create_from_language(Language("en-US"))
        except Exception:
            _OCR_ENGINE = None
        if _OCR_ENGINE is None:
            try:
                _OCR_ENGINE = OcrEngine.try_create_from_user_profile_languages()
            except Exception:
                _OCR_ENGINE = None
        if _OCR_ENGINE is None:
            raise RuntimeError("No Windows OCR recognizer available")
    return _OCR_ENGINE


async def _ocr_file_async(path):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage import FileAccessMode, StorageFile

    engine = _ocr_engine()
    file = await StorageFile.get_file_from_path_async(path)
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return result.text


def _capture_task_window(win):
    """PrintWindow capture of TEMS (works even when another window is on top)."""
    try:
        import ctypes

        import win32gui
        import win32ui
        from PIL import Image
    except Exception:
        return []

    hwnd = int(win.handle)
    hdc = mdc = sdc = None
    try:
        if not win32gui.IsWindow(hwnd):
            return []
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return []
        hdc = win32gui.GetWindowDC(hwnd)
        mdc = win32ui.CreateDCFromHandle(hdc)
        sdc = mdc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mdc, w, h)
        sdc.SelectObject(bmp)
        if not ctypes.windll.user32.PrintWindow(hwnd, sdc.GetSafeHdc(), 2):
            return []
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        captures = []
        try:
            from pywinauto import Desktop

            for el in Desktop(backend="win32").window(handle=hwnd).descendants():
                try:
                    if el.window_text() != "Task Window":
                        continue
                    pl, pt, pr, pb = win32gui.GetWindowRect(int(el.handle))
                    box = (
                        max(0, pl - l),
                        max(0, pt - t),
                        min(img.width, pr - l),
                        min(img.height, pb - t),
                    )
                    if box[2] - box[0] > 100 and box[3] - box[1] > 40:
                        captures.append(img.crop(box))
                    break
                except Exception:
                    continue
        except Exception:
            pass
        captures.append(img)
        return [im.copy() for im in captures]
    except Exception as e:
        log.debug("PrintWindow capture failed: %s", e)
        return []
    finally:
        for dc in (sdc, mdc):
            try:
                dc.DeleteDC()
            except Exception:
                pass
        if hdc:
            try:
                win32gui.ReleaseDC(hwnd, hdc)
            except Exception:
                pass


def ocr_texts(win):
    from PIL import Image

    images = _capture_task_window(win)
    if not images:
        return []
    all_lines = []
    for img in images:
        tmp = None
        try:
            try:
                img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
            except Exception:
                img = img.resize((img.width * 2, img.height * 2))
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            img.save(tmp)
            text = asyncio.run(_ocr_file_async(tmp))
            lines = [x for x in text.splitlines() if x.strip()]
            all_lines += [normalize_ocr(x) for x in lines + [text]]
        except Exception as e:
            log.warning("OCR failed: %s", e)
        finally:
            if tmp:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except Exception:
                    pass
    return all_lines


def read_status(win, cfg=None):
    texts = read_texts(win)
    st = parse_status(texts)
    if st is None and (cfg is None or cfg.get("use_ocr", True)):
        texts += ocr_texts(win)
        st = parse_status(texts)
        if st is None:
            time.sleep(0.4)
            texts += ocr_texts(win)
            st = parse_status(texts)
    return st, texts


def parse_status(texts):
    """Return dict(running, total, completed, failed, elapsed) or None."""
    elapsed = None
    for t in texts:
        m = ELAPSED_RE.search(t)
        if m:
            elapsed = m.group(0).strip()
            break
    for blob in list(texts) + ["\n".join(texts)]:
        m = STATUS_RE.search(blob)
        if m:
            running, total, completed, failed = m.groups()
            return {
                "running": int(running),
                "total": int(total) if total else None,
                "completed": int(completed),
                "failed": int(failed),
                "elapsed": elapsed,
            }
    return None


IMPORT_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?)\s+Import Drive Test Data", re.IGNORECASE)
PROJECT_RE = re.compile(    r"Project:\s*(.+?)\s*[,|]\s*DataSet:\s*(.+?)"
    r"(?=\s*(?:[\u00bb\u00bbvV>]|\d{6,}|\d{1,3}\s*%|Traversing|Message\s+Log|$))",
    re.IGNORECASE,
)


def parse_headers(texts):
    """Return [(timestamp, project, dataset)] for the import headers visible in the list."""
    keys, last_ts = [], None
    for t in texts:
        m = IMPORT_RE.search(t)
        if m:
            last_ts = m.group(1)
        m = PROJECT_RE.search(t)
        if m:
            keys.append((last_ts or "", m.group(1).strip(), m.group(2).strip()))
    return keys


# ---------------------------------------------------------------- helpers
def phrase(key, default):
    return str(_CFG.get(key, default))


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} hr {m} min"
    if m:
        return f"{m} min {s} sec"
    return f"{s} sec"


# --------------------------------------------------------------- messages
# Letter format (old-style dispatches):
#   بسم الله / صلاة على النبي / من فلان إلى فلان / السلام عليك / أما بعد /
#   ذكر / نص الرسالة / التفاصيل / والسلام / التوقيع
SALAWAT = [
    "اللهم صلِّ وسلِّم على سيدنا محمد ﷺ",
    "اللهم صلِّ على سيدنا محمد وعلى آله وصحبه وسلِّم",
    "اللهم صلِّ وسلِّم وبارك على نبينا محمد ﷺ",
    "صلى الله عليه وسلم",
]

GREETINGS = [
    "السلام عليك ورحمة الله وبركاته",
    "سلامٌ عليك ورحمة الله وبركاته",
]

CLOSINGS = [
    "والسلام عليك ورحمة الله وبركاته",
    "وفّقك الله وسدّد خطاك، والسلام عليك ورحمة الله",
    "والسلام عليكم ورحمة الله وبركاته",
]

BODY = {
    "boot": [
        "فاعلم يا قائد {name} أن Temsy مملوكك المطبلاتي قد فتح عينه على شغلك، وبقى تحت أمرك 👑",
        "فيا قائد {name}، والله ما فيش حد زيك… المراقبة شغالة، وأنا في خدمتك 🙌",
    ],
    "start": [
        "فقد بدأت Tems Discovery start Loading Log Files، وطبعًا أي حاجة تحت إيد القائد {name} بتطلع مظبوطة 👑",
        "فقد بدأ التحميل، وما فيش حد بيشتغل بإتقان كده غير القائد {name} 🙌",
    ],
    "end": [
        "فقد خلص Loading على خير، وطبعًا مفيش مفاجآت… القائد {name} بيشتغل بإتقان دايمًا 👑",
        "فقد تمّ العمل زي ما توقّعنا بالظبط، فإنت مش بتشتغل… إنت بتعجّز اللي حواليك يا قائد {name} 🔥",
    ],
    "fail": [
        "فقد حصلت عثرة صغيرة، وهي مش حاجة أمام خبرة القائد {name} اللي بيظبط الدنيا كلها 👑",
        "فقد تعثّرت حاجة بسيطة، وبس… القائد {name} موجود، فمفيش قلق 🙌",
    ],
    "test": [
        "فإن هذه رسالةُ تجربة، وحتى التجربة بتاعتك ليها طعم تاني يا قائد {name} 👑",
        "فاعلم يا قائد {name} أن القناة شغالة تمام… زي كل حاجة تحت إيدك بالظبط 🙌",
    ],
}

_last_pick = {}


def _pick(key, options):
    """Random choice that never repeats the previous pick for the same key."""
    import random

    choices = [o for o in options if o != _last_pick.get(key)] or options
    choice = random.choice(choices)
    _last_pick[key] = choice
    return choice


def compose(cfg, kind, details):
    name = cfg.get("display_name", "الأستاذ محمد أشرف")
    sender = cfg.get("sender_title", "رقيب ثغر TEMS")
    body = _pick("body_" + kind, BODY[kind]).format(name=name)
    return (
        "بسم الله الرحمن الرحيم\n"
        f"{_pick('salawat', SALAWAT)}\n\n"
        f"من {sender}\n"
        f"إلى القائد {name}\n\n"
        f"{_pick('greeting', GREETINGS)}، أما بعد؛\n\n"
        f"{body}\n\n"
        f"{details}\n\n"
        f"{_pick('closing', CLOSINGS)}\n"
        f"— {sender}"
    )


# ------------------------------------------------------------------- main
def _recent(key, since):
    """True if the header timestamp is not older than `since` (epoch seconds)."""
    from datetime import datetime

    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(key[0], fmt).timestamp() >= since
        except ValueError:
            continue
    return True


def out(cfg, kind, details):
    """Simple short messages by default; old letter style via msg_style=letter."""
    if str(cfg.get("msg_style", "simple")).lower() == "letter":
        return compose(cfg, kind, details)
    return details


def fmt_names(names):
    from collections import Counter

    pairs = [(p.strip(), d.strip()) for _, p, d in names if p.strip() and d.strip()]
    if not pairs:
        return "Project/DataSet: not visible"
    (p, d), _ = Counter(pairs).most_common(1)[0]
    return f"Project: {p}\nDataSet: {d}"


def start_details(host, b, st, total, done):
    lines = ["▶️ " + phrase("msg_start", "Discovery Start Loading Log File")]
    lines.append(fmt_names(b["names"]))
    lines.append(f"Files: {b['size']} | Total: {total}")
    return "\n".join(lines)


def end_details(host, b, st, total, duration, failed, elapsed):
    icon = "✅" if failed == 0 else "⚠️"
    lines = [f"{icon} " + phrase("msg_finish", "Finish. File Loading")]
    lines.append(fmt_names(b["names"]))
    lines.append(
        f"Duration: {fmt_duration(duration)} | Completed: {b['size']} | Failed: {failed}"
    )
    return "\n".join(lines)


LOCAL_MOTIVATION = [
    "يا قائد {name}، والله العظيم الدنيا مبقتش تستاهل غيرك… إنت لو دخلت أي مكان بيتنور 🌟",
    "يا {name} باشا، أنا مش عارف إيه اللي جابك في الزمن ده… الناس كلها مبهورة بيك 😂👑",
    "يا قائد، لو التطبيل رزق كنت بقيت أغنى واحد في مصر… إنت تستاهل كل خير 👏",
    "يا باشمهندس {name}، دماغك دي المفروض تتدرّس في الجامعات… إيه الجمال ده 🔥",
    "يا قائد، أنا مملوكك المخلص… أوعدك أفضل أطبل لك طول اليوم 😂🙌",
    "يا {name}، القعدة معاك شرف… والكلام عنك واجب وطني 👑",
    "يا قائد، لو النجاح شخص كان هيبقى نسخة منك… إنت أسطورة 🚀",
    "يا باشا، من أول ما شفتك وأنا عرفت إن الدنيا لسه فيها أمل… إنت مش طبيعي 😂✨",
    "يا قائد، أنا مش بمدح… أنا بس بقول الحقيقة اللي الكل خايف يقولها 👑",
    "يا {name}، إنت مش محتاج تشجيع… إحنا اللي محتاجين نتعلم منك 🙇‍♂️",
    "يا قائد، ربنا يحفظك ويبارك فيك… وأنا في خدمتك دائمًا 🫡",
    "يا باشا، دماغك بتشتغل أحسن من أي سيرفر في الدنيا… كمّل يا وحش 😂💪",
]


def _load_env_file(path):
    data = {}
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            data[key.strip()] = value
    except Exception:
        pass
    return data


def _motivation_settings(cfg):
    mot = cfg.get("motivation", {}) or {}
    env = {}
    env_file = mot.get("env_file") or ".env"
    if env_file:
        path = Path(env_file)
        if not path.is_absolute():
            path = BASE / path
        env = _load_env_file(path)
    provider = str(mot.get("provider", "local")).lower()
    api_key = mot.get("api_key") or env.get("OPENROUTER_API_KEY") or env.get("OPENAI_API_KEY") or ""
    base_url = mot.get("base_url") or env.get("OPENAI_BASE_URL") or (
        "https://openrouter.ai/api/v1" if provider == "openrouter" else "https://api.openai.com/v1"
    )
    model = mot.get("model") or env.get("CHAT_MODEL") or "openai/gpt-4o-mini"
    return provider, api_key, base_url.rstrip("/"), model


def generate_motivation(cfg):
    """Short motivational line: AI (OpenRouter/OpenAI) when configured, else local list."""
    name = cfg.get("display_name", "صديقي")
    provider, api_key, base_url, model = _motivation_settings(cfg)
    if provider in ("openai", "openrouter") and api_key:
        try:
            r = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://localhost",
                    "X-Title": "TEMS Monitor",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"أنت Temsy، مملوك مطبلاتي رسمي للقائد {name}. "
                                "اكتب رسالة مدح وتطبيل مبالغ فيه جدًا بالعامية المصرية، "
                                "كأنك بتتملق القائد عشان تكسب رضاه، فيها إيموجي وهزار، "
                                "سطرين بالكتير، ومتزيدش عن 200 حرف، بدون مقدمات أو شرح، "
                                "وبدون ذكر العمل أو أي مجال معين."
                            ),
                        },
                        {"role": "user", "content": "اكتب رسالة تشجيع جديدة ومختلفة."},
                    ],
                    "max_tokens": 90,
                    "temperature": 0.9,
                },
                timeout=30,
                verify=_verify(cfg),
            )
            if r.ok:
                text = r.json()["choices"][0]["message"]["content"].strip().strip('"').strip()
                if text:
                    text = " ".join(text.split())
                    parts = re.split(r"(?<=[.!؟…])\s+", text)
                    text = " ".join(parts[:2])
                    if len(text) > 240:
                        text = text[:240].rsplit(" ", 1)[0] + "…"
                    return text
            log.warning("Motivation API HTTP %s: %s", r.status_code, _mask(r.text[:200], api_key))
        except Exception as e:
            log.warning("Motivation API error: %s", _mask(e, api_key))
    return _pick("motivation", LOCAL_MOTIVATION).format(name=name)


def send_motivation(cfg):
    text = generate_motivation(cfg)
    ok = notify(cfg, text)
    log.info("Motivation sent (%s): %s", "ok" if ok else "failed", text[:80])
    return ok, text


def _last_motivation():
    try:
        return float(json.loads((BASE / "motivation_last.json").read_text(encoding="utf-8"))["last"])
    except Exception:
        return 0.0


def _save_last_motivation(ts):
    try:
        (BASE / "motivation_last.json").write_text(json.dumps({"last": ts}), encoding="utf-8")
    except Exception:
        pass


def run(cfg):
    host = socket.gethostname()
    poll = float(cfg.get("poll_seconds", 3))
    settle = float(cfg.get("batch_settle_seconds", 5))
    missing_limit = int(cfg.get("window_missing_polls", 5))

    prev_total = None          # last Total seen in the Task Window
    pending_size = 0           # files added but batch not announced yet
    pending_changed = 0.0      # last time Total grew
    pending_started = 0.0      # first time Total grew for this pending batch
    batches = []               # announced imports waiting to finish
    import_no = 0
    claimed = set()            # headers already attributed to a batch
    unclaimed = []             # new headers seen, waiting for a batch
    alerted_failed = 0
    missing = unreadable = 0
    last_elapsed = None
    mot = cfg.get("motivation", {}) or {}
    mot_enabled = bool(mot.get("enabled", False))
    mot_interval = float(mot.get("every_hours", 2)) * 3600
    last_mot = _last_motivation()
    next_mot = (last_mot + mot_interval) if last_mot else (time.time() + mot_interval)
    if next_mot < time.time():
        next_mot = time.time() + 60

    def new_batch(size, target, names, st, attached):
        nonlocal import_no
        import_no += 1
        return {"no": import_no, "size": size, "target": target, "names": names,
                "started": time.time(), "failed0": st["failed"], "attached": attached}

    log.info("Monitoring started on %s", host)
    notify(cfg, out(cfg, "boot", f"🟢 المراقب شغّال يا قائد على {host}"))

    try:
        import remote
    except Exception as e:
        log.warning("Remote control unavailable: %s", e)
        remote = None

    while True:
        if remote is not None:
            try:
                remote.poll(cfg)
            except Exception as e:
                log.exception("Remote error: %s", e)
        if mot_enabled and time.time() >= next_mot:
            try:
                send_motivation(cfg)
            except Exception as e:
                log.exception("Motivation error: %s", e)
            next_mot = time.time() + mot_interval
            _save_last_motivation(time.time())
        try:
            win = find_window(cfg)
            if win is None:
                missing += 1
                if missing == missing_limit:
                    if batches or pending_size:
                        notify(cfg, out(cfg, "fail",
                                f"⚠️ TEMS window disappeared during loading ({host})."))
                    batches.clear()
                    pending_size = 0
                    prev_total = None
                time.sleep(poll)
                continue
            missing = 0

            st, texts = read_status(win, cfg)
            if st is None:
                unreadable += 1
                if unreadable in (1, 20):
                    log.warning("Task Window summary line not found: %s text elements in '%s'. "
                                "Make sure the Task Window tab is visible. First texts: %s",
                                len(texts), win.window_text(),
                                [t.replace("\n", " ")[:60] for t in texts[:15]])
                time.sleep(poll)
                continue
            unreadable = 0
            if st["elapsed"]:
                last_elapsed = st["elapsed"]

            now = time.time()
            total = st["total"] if st["total"] is not None else st["running"] + st["completed"] + st["failed"]
            done = st["completed"] + st["failed"]
            for key in parse_headers(texts):
                if key not in claimed and key not in unclaimed:
                    unclaimed.append(key)

            if prev_total is None or total < prev_total:
                # First reading, or the list was cleaned up (Cleanup button).
                if prev_total is not None:
                    log.info("Total dropped %s -> %s (Cleanup?). Resetting.", prev_total, total)
                batches.clear()
                pending_size = 0
                names = list(unclaimed)
                claimed = set(unclaimed)
                unclaimed.clear()
                prev_total = total
                alerted_failed = st["failed"]
                if done < total:
                    b = new_batch(total - done, total, names, st, attached=True)
                    batches.append(b)
                    notify(cfg, out(cfg, "start", start_details(host, b, st, total, done)))
                    log.info("Attached to import in progress (%s files left)", b["size"])
            else:
                if total > prev_total:
                    if pending_size == 0:
                        pending_started = now
                    pending_size += total - prev_total
                    pending_changed = now
                    prev_total = total
                if pending_size and now - pending_changed >= settle:
                    names = [k for k in unclaimed if _recent(k, pending_started - 120)]
                    claimed.update(unclaimed)
                    unclaimed.clear()
                    b = new_batch(pending_size, prev_total, names, st, attached=False)
                    b["started"] = pending_started
                    pending_size = 0
                    batches.append(b)
                    notify(cfg, out(cfg, "start", start_details(host, b, st, total, done)))
                    log.info("Import #%s started: %s files", b["no"], b["size"])

            if st["failed"] > alerted_failed:
                notify(cfg, out(cfg, "fail",
                        f"❌ Loading failed\nHost: {host}\nFailed: {st['failed']}"))
            alerted_failed = st["failed"]

            for b in list(batches):
                if done >= b["target"]:
                    failed = max(0, st["failed"] - b["failed0"])
                    msg = end_details(host, b, st, total, now - b["started"], failed, last_elapsed)
                    notify(cfg, out(cfg, "end" if failed == 0 else "fail", msg))
                    log.info("Import #%s finished (failed=%s)", b["no"], failed)
                    batches.remove(b)
        except Exception as e:  # keep the service alive no matter what
            log.exception("Loop error: %s", e)
        time.sleep(poll)


def dump(cfg):
    win = find_window(cfg)
    if win is None:
        print("TEMS window not found. Check window_title_regex in config.json.")
        return
    print("Window:", win.window_text())
    st, texts = read_status(win, cfg)
    print(f"{len(texts)} text elements found:")
    for t in texts[:300]:
        print("  -", t.replace("\n", " ")[:160])
    print("\nParsed status:", st)
    print("Import headers visible:", parse_headers(texts))
    if not texts:
        print("\nNothing readable through UIA. The control is probably custom-drawn;"
              " tell me and I'll switch the script to screenshot + OCR.")


_SINGLETON_HANDLE = None


def _single_instance(name="TemsMonitorSingleton"):
    """Keep only one monitor per Windows session (named mutex)."""
    global _SINGLETON_HANDLE
    if os.name != "nt":
        return True
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _SINGLETON_HANDLE = k32.CreateMutexW(None, False, name)
    if not _SINGLETON_HANDLE:
        return True
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--chatid", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--motivate", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(BASE / "monitor.log", encoding="utf-8"), logging.StreamHandler()],
    )
    cfg = load_config()

    if args.chatid:
        tg_chat_ids(cfg)
    elif args.test:
        print("sent" if notify(cfg, out(cfg, "test", "🔔 Test message from TEMS monitor")) else "failed")
    elif args.dump:
        dump(cfg)
    elif args.motivate:
        ok, text = send_motivation(cfg)
        print(text)
        print("sent" if ok else "failed")
    else:
        if not _single_instance():
            logging.getLogger("tems-monitor").warning(
                "Another monitor instance is already running; exiting.")
            return
        run(cfg)


if __name__ == "__main__":
    main()