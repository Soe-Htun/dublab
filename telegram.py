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


# ---- big files (> 50 MB) through MTProto, still as the same bot ----
# The Bot HTTP API caps uploads at 50 MB, but a bot logged in over MTProto
# (Telethon) can upload up to 2 GB. Needs TELEGRAM_API_ID / TELEGRAM_API_HASH
# from https://my.telegram.org → API development tools, and `pip install telethon`.
SESSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_bot")
BIG_MAX = 2000 * 1024 * 1024
_big_lock = __import__("threading").Lock()


def big_configured():
    return bool(token() and os.environ.get("TELEGRAM_API_ID", "").strip().isdigit()
                and os.environ.get("TELEGRAM_API_HASH", "").strip())


def big_available():
    if not big_configured():
        return False
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        return False


def send_big_video(chat_id, path, filename, caption="", duration=0, width=0, height=0,
                   thumb=None, progress=None):
    """Upload a video of up to 2 GB to chat_id as the bot (blocking)."""
    import asyncio
    from telethon import TelegramClient
    from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, PeerUser

    async def run():
        client = TelegramClient(SESSION, int(os.environ["TELEGRAM_API_ID"]),
                                os.environ["TELEGRAM_API_HASH"].strip())
        await client.start(bot_token=token())
        try:
            try:
                peer = await client.get_input_entity(chat_id)
            except ValueError:            # not cached yet: look the user up
                peer = await client.get_input_entity(PeerUser(chat_id))
            cb = (lambda sent, total: progress(sent / max(total, 1))) if progress else None
            await client.send_file(
                peer, str(path), caption=caption[:1000], supports_streaming=True,
                thumb=str(thumb) if thumb else None, part_size_kb=512, progress_callback=cb,
                attributes=[DocumentAttributeVideo(duration=int(duration), w=int(width),
                                                   h=int(height), supports_streaming=True),
                            DocumentAttributeFilename(filename)])
        finally:
            await client.disconnect()

    with _big_lock:                       # one session file → one upload at a time
        try:
            asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            raise TelegramError(str(e)[:300]) from None
