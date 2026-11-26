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


_token = None      # set from the admin page (settings.json); None = use .env


def token():
    if _token is not None:
        return _token
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def set_token(t):
    """Use this bot token from now on ("" = no bot). A different bot needs a
    fresh MTProto session, so the old session file is dropped."""
    global _token
    t = (t or "").strip()
    if t != token():
        for ext in (".session", ".session-journal"):
            try:
                os.remove(SESSION + ext)
            except OSError:
                pass
    _token = t


def masked():
    t = token()
    return f"{t[:4]}…{t[-3:]}" if len(t) > 10 else ""


def check_token(t):
    """getMe with a token that is not saved yet; raises TelegramError if wrong."""
    global _token
    old = _token
    _token = t.strip()
    try:
        return bot_info()
    finally:
        _token = old


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


def send_file(chat_id, path, filename, caption="", kind="video", thumb=None, cover=None,
              width=None, height=None, duration=None):
    """kind 'video' (plays inline) or 'document'. thumb: JPEG ≤320 px.
    cover: full-size JPEG Telegram shows on the video in the chat."""
    with open(path, "rb") as fh:
        data = fh.read()
    method, field = ("sendVideo", "video") if kind == "video" else ("sendDocument", "document")
    params = {"chat_id": chat_id, "caption": caption[:1000]}
    if kind == "video":
        params["supports_streaming"] = "true"
        # without the real size Telegram guessed a square/landscape box for 9:16 videos
        for k, v in (("width", width), ("height", height), ("duration", duration)):
            if v:
                params[k] = str(int(v))
    ctype = "video/mp4" if kind == "video" else "application/octet-stream"
    files = {field: (filename, data, ctype)}
    if thumb and os.path.exists(thumb):
        with open(thumb, "rb") as fh:
            files["thumbnail"] = ("thumb.jpg", fh.read(), "image/jpeg")
    if kind == "video" and cover and os.path.exists(cover):
        with open(cover, "rb") as fh:
            files["cover"] = ("cover.jpg", fh.read(), "image/jpeg")
        params["cover"] = "attach://cover"
    return _call(method, params, files=files, timeout=600)


def send_image_file(chat_id, path, filename, caption=""):
    """An image as a file (not a compressed photo), so it downloads full size."""
    with open(path, "rb") as fh:
        files = {"document": (filename, fh.read(), "image/jpeg")}
    return _call("sendDocument", {"chat_id": chat_id, "caption": caption[:1000]},
                 files=files, timeout=120)


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


UPLOAD_WORKERS = int(os.environ.get("TELEGRAM_UPLOAD_WORKERS") or 8)
PART = 512 * 1024                         # Telegram's largest upload part


async def _parallel_upload(client, path, filename, progress=None):
    """Upload the file in 512 KB parts, several at once (Telethon's own
    upload sends one part at a time, which is slow for big videos)."""
    import asyncio
    import random
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import InputFileBig

    size = os.path.getsize(path)
    total = (size + PART - 1) // PART
    file_id = random.getrandbits(63)
    done = [0]
    queue = asyncio.Queue()
    for i in range(total):
        queue.put_nowait(i)

    async def worker(fh):
        while True:
            try:
                i = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            fh.seek(i * PART)
            data = fh.read(PART)
            for attempt in range(5):
                try:
                    if await client(SaveBigFilePartRequest(file_id, i, total, data)):
                        break
                except Exception:  # noqa: BLE001 - retry a few times, then give up
                    if attempt == 4:
                        raise
                    await asyncio.sleep(1 + attempt * 2)
            done[0] += 1
            if progress:
                progress(done[0] / total)

    handles = [open(path, "rb") for _ in range(UPLOAD_WORKERS)]
    try:
        await asyncio.gather(*(worker(fh) for fh in handles))
    finally:
        for fh in handles:
            fh.close()
    return InputFileBig(file_id, total, filename)


DL_CHUNK = 1024 * 1024        # Telegram serves at most 1 MB per request
DL_WORKERS = int(os.environ.get("DUBLAB_TG_DL_WORKERS") or 8)


async def _parallel_download(client, msg, dest, progress=None):
    """Download a message's file with several requests at once: worker w
    fetches chunks w, w+N, w+2N… (Telethon's own download fetches one chunk
    at a time, ~0.4 MB/s for big videos)."""
    import asyncio
    size = msg.file.size if msg.file else 0
    if not size:
        if not await client.download_media(msg, file=str(dest)):
            raise TelegramError("download failed")
        return
    total = (size + DL_CHUNK - 1) // DL_CHUNK
    workers = max(1, min(DL_WORKERS, total))
    done = [0]
    with open(dest, "wb") as fh:
        fh.truncate(size)

    async def worker(w):
        with open(dest, "r+b") as fh:
            pos = w * DL_CHUNK
            for attempt in range(5):
                try:
                    async for data in client.iter_download(
                            msg.media, offset=pos, stride=workers * DL_CHUNK,
                            request_size=DL_CHUNK, chunk_size=DL_CHUNK, file_size=size):
                        fh.seek(pos)
                        fh.write(data)
                        pos += workers * DL_CHUNK
                        done[0] += 1
                        if progress:
                            progress(min(1.0, done[0] / total))
                        if pos >= size:
                            break
                    return
                except Exception:  # noqa: BLE001 - resume this stripe where it stopped
                    if attempt == 4:
                        raise
                    await asyncio.sleep(1 + attempt * 2)

    await asyncio.gather(*(worker(w) for w in range(workers)))


def _client():
    from telethon import TelegramClient
    return TelegramClient(SESSION, int(os.environ["TELEGRAM_API_ID"]),
                          os.environ["TELEGRAM_API_HASH"].strip())


def upload_parts(path, filename, progress=None):
    """Upload the file to Telegram without sending it yet; returns a token for
    send_big_video(prepared=...) so the send itself takes only a second."""
    import asyncio

    async def run():
        client = _client()
        await client.start(bot_token=token())
        try:
            f = await _parallel_upload(client, path, filename, progress)
            return (f.id, f.parts, f.name)
        finally:
            await client.disconnect()

    with _big_lock:
        try:
            return asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            raise TelegramError(str(e)[:300]) from None


def send_big_video(chat_id, path, filename, caption="", duration=0, width=0, height=0,
                   thumb=None, progress=None, prepared=None, cover=None):
    """Send a video of up to 2 GB to chat_id as the bot (blocking). With
    `prepared` (from upload_parts) the file is already on Telegram's side;
    if Telegram has dropped it meanwhile, it is uploaded again."""
    import asyncio
    from telethon.tl.types import (DocumentAttributeFilename, DocumentAttributeVideo,
                                   InputFileBig, PeerUser)

    async def run():
        client = _client()
        await client.start(bot_token=token())
        try:
            try:
                peer = await client.get_input_entity(chat_id)
            except ValueError:            # not cached yet: look the user up
                peer = await client.get_input_entity(PeerUser(chat_id))
            attrs = [DocumentAttributeVideo(duration=int(duration), w=int(width),
                                            h=int(height), supports_streaming=True),
                     DocumentAttributeFilename(filename)]
            video_cover = None
            if cover and os.path.exists(cover):     # the cover shown on the video in the chat
                try:
                    from telethon.tl.functions.messages import UploadMediaRequest
                    from telethon.tl.types import InputMediaUploadedPhoto, InputPhoto
                    m = await client(UploadMediaRequest(peer, InputMediaUploadedPhoto(
                        await client.upload_file(str(cover)))))
                    video_cover = InputPhoto(m.photo.id, m.photo.access_hash, m.photo.file_reference)
                except Exception:  # noqa: BLE001 - send without a cover rather than fail
                    video_cover = None

            async def send(uploaded):
                if video_cover is None:
                    await client.send_file(peer, uploaded, caption=caption[:1000], supports_streaming=True,
                                           thumb=str(thumb) if thumb else None, attributes=attrs)
                    return
                from telethon.tl.functions.messages import SendMediaRequest
                from telethon.tl.types import InputMediaUploadedDocument
                th = await client.upload_file(str(thumb)) if thumb else None
                await client(SendMediaRequest(peer, InputMediaUploadedDocument(
                    file=uploaded, mime_type="video/mp4", attributes=attrs, thumb=th,
                    video_cover=video_cover), message=caption[:1000]))
            if prepared:
                try:
                    await send(InputFileBig(*prepared))
                    return
                except Exception as e:  # noqa: BLE001
                    if "FILE_PART" not in str(e).upper():
                        raise
            await send(await _parallel_upload(client, path, filename, progress))
        finally:
            await client.disconnect()

    with _big_lock:                       # one session file → one upload at a time
        try:
            asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            raise TelegramError(str(e)[:300]) from None


# ---- videos forwarded to the bot → DubLab input ----
# A bot can't open channel links itself, but anything the owner sends or
# forwards to it can be downloaded (up to 2 GB over MTProto).

def recent_videos(chat_id, limit=15):
    """Videos the owner sent/forwarded to the bot (newest first, last ~24 h)."""
    updates = _call("getUpdates", {"limit": 100, "allowed_updates": ["message"]})
    out = []
    for u in updates:
        m = u.get("message") or {}
        if (m.get("chat") or {}).get("id") != chat_id:
            continue
        v = m.get("video")
        doc = m.get("document") or {}
        if not v and str(doc.get("mime_type", "")).startswith("video/"):
            v = doc
        if not v:
            continue
        origin = (m.get("forward_origin") or {})
        src = ((origin.get("chat") or {}).get("title") or (origin.get("sender_user") or {}).get("first_name")
               or origin.get("sender_user_name") or "")
        cap = (m.get("caption") or "").strip().splitlines()
        name = (cap[0] if cap else "") or v.get("file_name") or "Telegram video"
        out.append({"id": m["message_id"], "name": name[:120], "from": src[:60],
                    "duration": int(v.get("duration") or 0), "size": int(v.get("file_size") or 0),
                    "date": m.get("date", 0)})
    out.sort(key=lambda x: x["id"], reverse=True)
    return out[:limit]


def download_video(chat_id, msg_id, dest, progress=None):
    """Download the video of message msg_id (in the owner's chat with the bot)."""
    import asyncio
    from telethon.tl.types import PeerUser

    async def run():
        client = _client()
        await client.start(bot_token=token())
        try:
            try:
                peer = await client.get_input_entity(chat_id)
            except ValueError:
                peer = await client.get_input_entity(PeerUser(chat_id))
            msg = await client.get_messages(peer, ids=msg_id)
            if not msg or not msg.media:
                raise TelegramError("message has no video (it may have been deleted)")
            await _parallel_download(client, msg, dest, progress)
            return str(dest)
        finally:
            await client.disconnect()

    with _big_lock:
        try:
            return asyncio.run(run())
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TelegramError(str(e)[:300]) from None


def sent_videos(chat_id, days=3):
    """📥 The videos in the owner's chat with the bot from the last `days` days (what the bot
    sent: finished parts with their "Title - 3" caption), newest first:
    [{"id", "caption", "date", "size", "duration"}]. Bots can't read a chat's history, so
    messages are fetched by id, going back from the newest one."""
    import asyncio
    import time as _t
    from telethon.tl.types import PeerUser
    probe = send_message(chat_id, "🔎 …")           # the newest message id in this chat
    top = int(probe.get("message_id") or 0)
    try:
        _call("deleteMessage", {"chat_id": chat_id, "message_id": top})
    except TelegramError:
        pass
    since = _t.time() - days * 86400

    async def run():
        client = _client()
        await client.start(bot_token=token())
        out = []
        try:
            try:
                peer = await client.get_input_entity(chat_id)
            except ValueError:
                peer = await client.get_input_entity(PeerUser(chat_id))
            hi, empty = top - 1, 0
            while hi > 0 and empty < 3:
                ids = list(range(hi, max(0, hi - 100), -1))
                msgs = await client.get_messages(peer, ids=ids)
                old = 0
                for m in msgs:
                    if not m:
                        continue
                    if m.date.timestamp() < since:
                        old += 1
                        continue
                    v = getattr(m, "video", None) or (m.document if m.document and str(
                        getattr(m.document, "mime_type", "")).startswith("video/") else None)
                    if v and m.out:                   # sent by the bot
                        dur = 0
                        for a in getattr(v, "attributes", []) or []:
                            dur = int(getattr(a, "duration", 0) or dur)
                        out.append({"id": m.id, "caption": (m.message or "").strip(), "date": int(m.date.timestamp()),
                                    "size": int(getattr(v, "size", 0) or 0), "duration": dur})
                if old and old >= sum(1 for m in msgs if m) * 0.9:
                    break                              # this batch is older than `days`
                empty = empty + 1 if not any(msgs) else 0
                hi -= 100
        finally:
            await client.disconnect()
        return out

    with _big_lock:
        return asyncio.run(run())
