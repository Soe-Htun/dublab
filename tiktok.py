"""TikTok Login Kit + Content Posting API (Direct Post and Drafts/inbox).

One connected account (the site owner). Tokens are kept in a small JSON file
outside git. Docs: developers.tiktok.com/doc/content-posting-api-get-started
"""
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish,video.upload"

TOKEN_FILE = Path(__file__).parent / "brand" / "tiktok_token.json"
_lock = threading.Lock()
_states = {}            # OAuth state -> created time (CSRF protection)


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl()


class TikTokError(RuntimeError):
    pass


def client_key():
    return (os.environ.get("TIKTOK_CLIENT_KEY") or "").strip()


def client_secret():
    return (os.environ.get("TIKTOK_CLIENT_SECRET") or "").strip()


def public_url():
    url = (os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")
    if not url and os.environ.get("NGROK_DOMAIN"):
        url = "https://" + os.environ["NGROK_DOMAIN"].strip()
    return url


def redirect_uri():
    return public_url() + "/tiktok/callback"


def configured():
    return bool(client_key() and client_secret() and public_url())


# ---------------- HTTP helpers ----------------
def _req(method, url, *, data=None, json_body=None, token=None, headers=None, timeout=60):
    h = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json; charset=UTF-8"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") or "{}"
    try:
        out = json.loads(raw)
    except ValueError:
        raise TikTokError(f"bad response from TikTok: {raw[:200]}") from None
    err = out.get("error")
    if isinstance(err, dict) and err.get("code") not in (None, "ok"):
        raise TikTokError(f"{err.get('code')}: {err.get('message') or ''}".strip())
    if isinstance(err, str) and err:                       # oauth endpoint style
        raise TikTokError(f"{err}: {out.get('error_description', '')}")
    return out


# ---------------- tokens ----------------
def _load():
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        return None


def _save(tok):
    TOKEN_FILE.parent.mkdir(exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok))
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_FILE)


def _store(resp):
    now = time.time()
    tok = {"access_token": resp["access_token"],
           "refresh_token": resp.get("refresh_token", ""),
           "open_id": resp.get("open_id", ""),
           "scope": resp.get("scope", ""),
           "expires_at": now + int(resp.get("expires_in", 0)) - 60,
           "refresh_expires_at": now + int(resp.get("refresh_expires_in", 0))}
    old = _load() or {}
    tok["user"] = old.get("user") if old.get("open_id") == tok["open_id"] else None
    _save(tok)
    return tok


def access_token():
    """A valid access token (refreshed when needed), or None if not connected."""
    with _lock:
        tok = _load()
        if not tok:
            return None
        if time.time() < tok.get("expires_at", 0):
            return tok["access_token"]
        if not tok.get("refresh_token") or time.time() > tok.get("refresh_expires_at", 0):
            return None
        resp = _req("POST", f"{API}/oauth/token/", data={
            "client_key": client_key(), "client_secret": client_secret(),
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        return _store(resp)["access_token"]


def auth_url():
    state = secrets.token_urlsafe(24)
    now = time.time()
    for s, t in list(_states.items()):
        if now - t > 900:
            _states.pop(s, None)
    _states[state] = now
    q = urllib.parse.urlencode({"client_key": client_key(), "scope": SCOPES,
                                "response_type": "code", "redirect_uri": redirect_uri(),
                                "state": state})
    return f"{AUTH_URL}?{q}"


def finish_login(code, state):
    if not state or _states.pop(state, None) is None:
        raise TikTokError("login expired or invalid — try connecting again")
    resp = _req("POST", f"{API}/oauth/token/", data={
        "client_key": client_key(), "client_secret": client_secret(), "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    _store(resp)
    user_info(refresh=True)


def disconnect():
    tok = _load()
    if tok:
        try:
            _req("POST", f"{API}/oauth/revoke/", data={
                "client_key": client_key(), "client_secret": client_secret(),
                "token": tok["access_token"]})
        except Exception:  # noqa: BLE001 - local logout must still work
            pass
    TOKEN_FILE.unlink(missing_ok=True)


def user_info(refresh=False):
    tok = _load()
    if not tok:
        return None
    if tok.get("user") and not refresh:
        return tok["user"]
    t = access_token()
    if not t:
        return None
    resp = _req("GET", f"{API}/user/info/?fields=open_id,avatar_url,display_name", token=t)
    user = (resp.get("data") or {}).get("user") or {}
    tok = _load() or tok
    tok["user"] = {"display_name": user.get("display_name", ""),
                   "avatar_url": user.get("avatar_url", "")}
    _save(tok)
    return tok["user"]


def creator_info():
    """Account's posting options (privacy levels, max duration, toggles)."""
    t = access_token()
    if not t:
        raise TikTokError("TikTok account not connected")
    return _req("POST", f"{API}/post/publish/creator_info/query/", token=t, json_body={})["data"]


# ---------------- upload ----------------
MB = 1024 * 1024


def _chunks(size):
    """TikTok rules: a file up to 64 MB may go as one chunk; otherwise chunks
    of 5–64 MB where the last one absorbs the remainder (up to 128 MB)."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk          # last chunk = 10 MB + remainder (< 20 MB)


def upload(video_path, mode, title="", privacy="SELF_ONLY", disable_comment=False,
           disable_duet=False, disable_stitch=False, progress=None):
    """mode 'direct' posts to the profile; 'draft' sends to the TikTok inbox
    (finish in the app). Returns publish_id."""
    t = access_token()
    if not t:
        raise TikTokError("TikTok account not connected")
    size = os.path.getsize(video_path)
    chunk, count = _chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": chunk, "total_chunk_count": count}
    if mode == "direct":
        body = {"post_info": {"title": title[:2200], "privacy_level": privacy,
                              "disable_comment": disable_comment,
                              "disable_duet": disable_duet,
                              "disable_stitch": disable_stitch},
                "source_info": source}
        init = _req("POST", f"{API}/post/publish/video/init/", token=t, json_body=body)
    else:
        init = _req("POST", f"{API}/post/publish/inbox/video/init/", token=t,
                    json_body={"source_info": source})
    data = init["data"]
    upload_url, publish_id = data["upload_url"], data["publish_id"]
    with open(video_path, "rb") as fh:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            fh.seek(start)
            part = fh.read(end - start + 1)
            req = urllib.request.Request(upload_url, data=part, method="PUT", headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(part)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            try:
                with urllib.request.urlopen(req, timeout=300, context=_SSL):
                    pass
            except urllib.error.HTTPError as e:
                if e.code not in (200, 201, 206):
                    raise TikTokError(f"upload chunk {i + 1}/{count} failed: HTTP {e.code}") from None
            if progress:
                progress(i + 1, count)
    return publish_id


def status(publish_id):
    t = access_token()
    if not t:
        raise TikTokError("TikTok account not connected")
    return _req("POST", f"{API}/post/publish/status/fetch/", token=t,
                json_body={"publish_id": publish_id})["data"]
