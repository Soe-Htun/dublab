"""Send finished dubs to the owner's Telegram through their own bot.

Setup: create a bot with @BotFather, put its token in .env as
TELEGRAM_BOT_TOKEN, send /start to the bot, then press "Connect" on the admin
page (it reads the bot's latest message to learn your chat id).
"""
import json
import os
import ssl
import urllib.error
import urllib.request
import uuid

API = "https://api.telegram.org/bot{token}/{method}"
MAX_UPLOAD = 49 * 1024 * 1024        # Bot API upload limit is 50 MB


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl()


class TelegramError(RuntimeError):
    pass


def token():
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def configured():
    return bool(token())


def _call(method, params=None, files=None, timeout=60):
    url = API.format(token=token(), method=method)
    if files:
        body, ctype = _multipart(params or {}, files)
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
    else:
        req = urllib.request.Request(url, data=json.dumps(params or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode())
        except ValueError:
            raise TelegramError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise TelegramError(f"can't reach Telegram ({e.reason})") from None
    if not out.get("ok"):
        raise TelegramError(out.get("description") or "Telegram error")
    return out["result"]


def _multipart(fields, files):
    """files: {field: (filename, bytes, content_type)}"""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        safe = fname.replace('"', "'")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                     f'filename="{safe}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        parts.append(data)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def bot_info():
    me = _call("getMe")
    return {"username": me.get("username", ""), "name": me.get("first_name", "")}


def find_chat():
    """The chat of the most recent message sent to the bot (send /start first)."""
    updates = _call("getUpdates", {"limit": 50, "allowed_updates": ["message"]})
    for u in reversed(updates):
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("id"):
            name = (" ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                    or chat.get("title") or chat.get("username") or str(chat["id"]))
            return chat["id"], name
    raise TelegramError("no messages found — open your bot in Telegram, send /start, then try again")


def send_message(chat_id, text):
    return _call("sendMessage", {"chat_id": chat_id, "text": text[:4000],
                                 "disable_web_page_preview": True})


def send_file(chat_id, path, filename, caption="", kind="video"):
    """kind 'video' (plays inline) or 'document'."""
    with open(path, "rb") as fh:
        data = fh.read()
    method, field = ("sendVideo", "video") if kind == "video" else ("sendDocument", "document")
    params = {"chat_id": chat_id, "caption": caption[:1000]}
    if kind == "video":
        params["supports_streaming"] = "true"
    ctype = "video/mp4" if kind == "video" else "application/octet-stream"
    return _call(method, params, files={field: (filename, data, ctype)}, timeout=600)
