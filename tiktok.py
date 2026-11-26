"""📱 TikTok drafts: the owner's own account, through TikTok's Content Posting API.

Login Kit (OAuth v2) once, then each finished part goes to the account's TikTok
inbox as a DRAFT (scope video.upload) — the owner opens TikTok, writes or pastes
the caption, picks the cover and posts or schedules it. Nothing is posted publicly
from here.

Keys come from .env: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET (the app's sandbox or
production keys) and the redirect URI registered in the developer portal
(TIKTOK_REDIRECT_URI, default https://$NGROK_DOMAIN/tiktok/callback).
"""
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.upload"
CHUNK = 10 * 1024 * 1024          # TikTok takes 5–64 MB chunks (the last one may be bigger)


class TikTokError(RuntimeError):
    pass


def _ssl_context():
    """python.org's macOS Python has no CA certificates (CERTIFICATE_VERIFY_FAILED): certifi's."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


def client_key():
    return (os.environ.get("TIKTOK_CLIENT_KEY") or "").strip().strip('"')


def client_secret():
    return (os.environ.get("TIKTOK_CLIENT_SECRET") or "").strip().strip('"')


def redirect_uri():
    uri = (os.environ.get("TIKTOK_REDIRECT_URI") or "").strip()
    if not uri and os.environ.get("NGROK_DOMAIN"):
        uri = "https://" + os.environ["NGROK_DOMAIN"].strip().strip('"') + "/tiktok/callback"
    return uri


def configured():
    return bool(client_key() and client_secret() and redirect_uri())


_states = {}                       # OAuth state → time made (a login link works 15 minutes)


def login_url():
    state = secrets.token_urlsafe(16)
    now = time.time()
    for s, t in list(_states.items()):
        if now - t > 900:
            _states.pop(s, None)
    _states[state] = now
    q = urllib.parse.urlencode({"client_key": client_key(), "scope": SCOPES, "response_type": "code",
                                "redirect_uri": redirect_uri(), "state": state})
    return f"{AUTH_URL}?{q}"


def check_state(state):
    return bool(state) and _states.pop(state, None) is not None


def _post(url, data=None, form=None, token=None, timeout=60):
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(data or {}).encode()
        ctype = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            out = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode() or "{}")
        except ValueError:
            raise TikTokError(f"TikTok HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TikTokError(f"can't reach TikTok: {e}") from None
    err = out.get("error")
    if isinstance(err, dict) and err.get("code") not in (None, "", "ok"):
        raise TikTokError(f"{err.get('code')}: {err.get('message') or ''}".strip())
    if isinstance(err, str) and err:
        raise TikTokError(f"{err}: {out.get('error_description') or ''}".strip())
    return out


def _get(url, token, timeout=30):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            out = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise TikTokError(f"TikTok HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TikTokError(f"can't reach TikTok: {e}") from None
    err = out.get("error") or {}
    if err.get("code") not in (None, "", "ok"):
        raise TikTokError(f"{err.get('code')}: {err.get('message') or ''}")
    return out


def _token_record(out):
    now = time.time()
    return {"access_token": out["access_token"], "refresh_token": out.get("refresh_token", ""),
            "open_id": out.get("open_id", ""), "scope": out.get("scope", ""),
            "expires_at": now + int(out.get("expires_in") or 86400) - 120,
            "refresh_expires_at": now + int(out.get("refresh_expires_in") or 365 * 86400) - 120}


def exchange_code(code):
    """The ?code= TikTok sent back → a token record (access + refresh token)."""
    out = _post(f"{API}/oauth/token/", form={
        "client_key": client_key(), "client_secret": client_secret(), "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    if not out.get("access_token"):
        raise TikTokError("no access token in TikTok's answer")
    return _token_record(out)


def fresh(tok):
    """A token record with an access token that still works (refreshed when due).
    Returns (record, changed)."""
    if not tok or not tok.get("access_token"):
        raise TikTokError("TikTok not connected")
    if time.time() < tok.get("expires_at", 0):
        return tok, False
    if time.time() > tok.get("refresh_expires_at", 0) or not tok.get("refresh_token"):
        raise TikTokError("TikTok login expired — connect again")
    out = _post(f"{API}/oauth/token/", form={
        "client_key": client_key(), "client_secret": client_secret(),
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    return _token_record(out), True


def user_info(access_token):
    out = _get(f"{API}/user/info/?fields=open_id,display_name,avatar_url", access_token)
    return (out.get("data") or {}).get("user") or {}


def upload_draft(access_token, path, progress=None):
    """Send one video file to the account's TikTok inbox as a draft. Returns publish_id."""
    path = Path(path)
    size = path.stat().st_size
    if size <= CHUNK:
        chunk, count = size, 1
    else:
        chunk, count = CHUNK, size // CHUNK       # the last chunk takes the rest (≤ 2×CHUNK)
    init = _post(f"{API}/post/publish/inbox/video/init/", {
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk, "total_chunk_count": count}}, token=access_token)
    data = init.get("data") or {}
    url, pid = data.get("upload_url"), data.get("publish_id")
    if not url or not pid:
        raise TikTokError("TikTok gave no upload address")
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            blob = f.read(end - start + 1)
            req = urllib.request.Request(url, data=blob, method="PUT", headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(blob)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=300, context=_SSL) as r:
                        r.read()
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (200, 201, 206):
                        break
                    if attempt == 2 or e.code < 500:
                        raise TikTokError(f"upload chunk {i + 1}/{count}: HTTP {e.code}") from None
                except (urllib.error.URLError, TimeoutError) as e:
                    if attempt == 2:
                        raise TikTokError(f"upload chunk {i + 1}/{count}: {e}") from None
                time.sleep(3 * (attempt + 1))
            if progress:
                progress((i + 1) / count)
    return pid


def draft_status(access_token, publish_id):
    out = _post(f"{API}/post/publish/status/fetch/", {"publish_id": publish_id}, token=access_token)
    return (out.get("data") or {}).get("status", "")
