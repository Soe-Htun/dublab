"""DubLab — short-video dubbing website. Upload video -> dubbed video."""
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file

import telegram
import tiktok

from dub import (tight_spans, voice_lengths, fix_dangling, join_fragments, add_cut_audio, WHISPER_MODELS, set_speak_speed, build_dub_track, cleanup_segments, configure_tts, frame_layout, render_frames,
                 frame_caption_y,
                 footage, speech_spans, char_spans, keep_most, trim_copy, picture_area,
                 add_outro_tail, caption_y, normalize_logo, render_captions, sample_frames, video_size, write_srt,
                 default_whisper_model, demucs_available, detect_gender,
                 download_url, extract_audio,
                 has_audio_stream, has_video_stream, is_supported_url, merge_segments,
                 build_recap_video, make_covers, mux, run, separate_vocals, beat_footage, trim_silence, frames_every, cut_ranges, mosaic_video, edit_video, fiction_notice, synthesize, speech_regions, snap_to_speech, fit_cues_to_speech, probe_duration, probe_url, separate_music, split_points, YouTubeBlocked, line_genders, mix_comments, NARRATOR_VOICE,
                 split_video, transcribe)
import gemini   # gemini.TTS_CPS follows the voice speed chosen in Settings (a copy imported once never did)
from gemini import (RateLimited, draw_cover, find_caption_band, find_logos, find_movie_bounds, pick_cover_frames, pick_hook, thumb_is_good, translate_lines,
                    write_cover_scene, write_intro, write_recap, write_title, movie_name,
                    transcribe_burmese, write_cast, write_posts, find_sensitive, analyze_warning, check_covers, write_comments, game_highlights)

# Gemini TTS key rotation.
# GEMINI_TTS_API_KEYS is comma-separated; falls back to the single GEMINI_API_KEY.
_tts_keys = [k.strip() for k in
             os.environ.get("GEMINI_TTS_API_KEYS",
                            os.environ.get("GEMINI_API_KEY", "")).split(",")
             if k.strip()]
configure_tts(_tts_keys)

BASE = Path(__file__).parent
JOBS = BASE / "jobs"
JOBS.mkdir(exist_ok=True)

# Longest video accepted. .env gives the default; the admin page can change it
# at runtime (saved to settings.json so it survives restarts).
SETTINGS_FILE = BASE / "settings.json"
MAX_MINUTES_LIMIT = 180
OUTRO_MAX = 200   # chars
AI_COVER = (os.environ.get("DUBLAB_AI_COVER") or "0") == "1"   # AI-drawn cover (needs a paid Gemini key); off = photo collage
SPLIT_CHOICES = (0, 3, 5, 10, 15, 20)
OWNER_MAX_MINUTES = int(os.environ.get("DUBLAB_OWNER_MAX_MINUTES") or 240)   # owner: 4 h   # minutes per part; 0 = one video
CHANNEL_MAX = 60
# channel logo burned into the video corner (uploaded on the admin page)
BRAND_DIR = BASE / "brand"
LOGO_FILE = BRAND_DIR / "logo.png"
LOGO_POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left")
_settings = {"max_minutes": int(os.environ.get("DUBLAB_MAX_MINUTES") or 30),
             # spoken at the end of every dub (in the chosen voice); "" = off
             "outro": (os.environ.get("DUBLAB_OUTRO") or "").strip()[:OUTRO_MAX],
             # the owner's channel: replaces other channels' names in the dub
             "channel_name": (os.environ.get("DUBLAB_CHANNEL_NAME") or "Marco TV").strip()[:CHANNEL_MAX],
             "logo_position": "top-right",
             "logo_size": 12,   # % of the video width
             # Burmese captions: text size (% of video height); blur band that
             # hides burned-in original captions (% from bottom, % tall)
             "caption_size": 5.1, "caption_bottom": 8, "blur_bottom": 4, "blur_height": 16,
             # portrait (TikTok/Shorts) videos: TikTok's own UI covers the bottom
             # ~20%, and burned-in captions there usually sit higher up
             "caption_bottom_v": 24, "blur_bottom_v": 20, "blur_height_v": 12,
             # how fast the Burmese voice speaks (1.0 = the TTS's own pace)
             "speak_speed": float(os.environ.get("DUBLAB_SPEAK_SPEED") or 1.15)}
_RANGES = {"caption_size": (3, 10), "caption_bottom": (0, 40),
           "blur_bottom": (0, 60), "blur_height": (4, 40),
           "caption_bottom_v": (0, 60), "blur_bottom_v": (0, 70), "blur_height_v": (4, 40),
           "speak_speed": (0.9, 1.5)}
try:
    _saved = json.loads(SETTINGS_FILE.read_text())
    if isinstance(_saved.get("max_minutes"), int) and 1 <= _saved["max_minutes"] <= MAX_MINUTES_LIMIT:
        _settings["max_minutes"] = _saved["max_minutes"]
    if isinstance(_saved.get("outro"), str):
        _settings["outro"] = _saved["outro"].strip()[:OUTRO_MAX]
    if isinstance(_saved.get("channel_name"), str):
        _settings["channel_name"] = _saved["channel_name"].strip()[:CHANNEL_MAX]
    if _saved.get("logo_position") in LOGO_POSITIONS:
        _settings["logo_position"] = _saved["logo_position"]
    if isinstance(_saved.get("logo_size"), int) and 5 <= _saved["logo_size"] <= 30:
        _settings["logo_size"] = _saved["logo_size"]
    for _k, (_lo, _hi) in _RANGES.items():
        if isinstance(_saved.get(_k), (int, float)) and _lo <= _saved[_k] <= _hi:
            _settings[_k] = _saved[_k]
    if isinstance(_saved.get("telegram_token"), str):   # set on the admin page
        telegram.set_token(_saved["telegram_token"])
    if isinstance(_saved.get("telegram_chat_id"), int):
        _settings["telegram_chat_id"] = _saved["telegram_chat_id"]
        _settings["telegram_chat_name"] = str(_saved.get("telegram_chat_name") or "")[:80]
except (OSError, ValueError, AttributeError):
    pass
set_speak_speed(_settings["speak_speed"])

# Largest upload / link download, set in .env only (run.sh sizes waitress's
# request-body limit from the same value).
MAX_MB = int(os.environ.get("DUBLAB_MAX_MB") or 3000)


def max_minutes():
    return _settings["max_minutes"]


def max_mb():
    return MAX_MB


# background music/effects (original voices removed by Demucs) kept under the
# dub at this volume, 0-1; 0 = no background at all
BG_VOLUME = float(os.environ.get("DUBLAB_BG_VOLUME") or 0.8)
TTL = 24 * 3600        # keep results a day
KEEP_SMALL = 7 * 24 * 3600   # a job whose videos went to Telegram is only covers + text: kept a week,
                             # so 🔧 fixing one of its parts still finds that part's cover

# ---- abuse protection (public site) ----
MAX_QUEUE = 15          # max jobs waiting in line
UPLOADS_PER_HOUR = 10   # per client IP
_ip_hits = {}
_ip_lock = threading.Lock()

# ---- maintenance mode (TTS outage) ----
# Turn True to pause new dub jobs; the home page shows the maintenance image.
MAINTENANCE = False
MAINTENANCE_MSG = ("🔧 DubLab ခေတ္တ ရပ်ထားပါတယ် — Microsoft အသံထုတ် service "
                   "ပျက်နေလို့ ပြင်ဆင်နေပါတယ်။ ပြန်ကောင်းတာနဲ့ အကြောင်းကြားမယ် 🙏")

# ---- reviews ----
REVIEWS = BASE / "reviews.jsonl"
# Optional e-mail for each new review / feedback (Gmail: use an App Password).
MAIL_TO = os.environ.get("DUBLAB_MAIL_TO", "").strip()
SMTP_HOST = os.environ.get("DUBLAB_SMTP_HOST", "").strip() or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("DUBLAB_SMTP_PORT") or 465)
SMTP_USER = os.environ.get("DUBLAB_SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("DUBLAB_SMTP_PASSWORD", "").replace(" ", "")
ADMIN_KEY = os.environ.get("DUBLAB_ADMIN_KEY", "")

VOICES = {
    "en2mm": {"female": "my-MM-NilarNeural", "male": "my-MM-ThihaNeural"},
    "mm2en": {"female": "en-US-AvaNeural", "male": "en-US-AndrewNeural"},
    "zh2mm": {"female": "my-MM-NilarNeural", "male": "my-MM-ThihaNeural"},
    # default: detect the spoken language automatically, always dub to Burmese
    "auto2mm": {"female": "my-MM-NilarNeural", "male": "my-MM-ThihaNeural"},
}
SRC_LANG = {"en2mm": "en", "mm2en": "my", "zh2mm": "zh", "auto2mm": None}  # whisper language
LANG_NAMES = {"my": "Burmese", "zh": "Chinese (Mandarin)", "yue": "Cantonese", "en": "English",
              "ja": "Japanese", "ko": "Korean", "th": "Thai", "hi": "Hindi",
              "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
              "ru": "Russian", "es": "Spanish", "fr": "French", "de": "German",
              "km": "Khmer (Cambodian)", "lo": "Lao", "tl": "Filipino (Tagalog)", "bn": "Bengali",
              "ta": "Tamil", "te": "Telugu", "pt": "Portuguese", "tr": "Turkish", "ar": "Arabic"}
MAX_LINE = 500          # chars per edited line
STAGE_LABEL = {
    "queue": "တန်းစီနေတယ်",
    "download": "ဗီဒီယို ဒေါင်းနေတယ်",
    "audio": "အသံထုတ်ယူနေတယ်",
    "transcribe": "စကားပြောမှတ်နေတယ်",
    "translate": "ဘာသာပြန်နေတယ်",
    "fixcheck": "ပြဿနာကို စစ်နေတယ်",
    "fixing": "ဗီဒီယိုကို ပြင်နေတယ်",
    "review": "ဘာသာပြန်စာ ပြင်ဖို့ စောင့်နေတယ်",
    "music": "နောက်ခံ music ခွဲထုတ်နေတယ်",
    "voice": "အသံဖော်နေတယ်",
    "mux": "ဗီဒီယိုပေါင်းနေတယ်",
    "split": "အပိုင်းခွဲနေတယ်",
    "recap": "Recap အခန်းတွေ ဖြတ်ဆက်နေတယ်",
    "done": "ပြီးပြီ",
}
STAGE_RANGE = {"download": (1, 4), "transcribe": (8, 44), "voice": (60, 90)}  # overall pct span

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True   # an edited page shows on refresh, without a restart
app.config["MAX_CONTENT_LENGTH"] = max_mb() * 1024 * 1024

jobs = {}
queue = []
lock = threading.Condition()


def _sp(job, frac):
    """This stage's own progress (0..1), shown as % on its ring."""
    job["spct"] = max(0, min(100, round(frac * 100)))
    job["spct_stage"] = job["stage"]


class _CoversDone(Exception):
    """The covers were made from the video's own thumbnail (skips the photo collage)."""


class Cancelled(Exception):
    """The user pressed ⏹ stop."""


def jlog(job, msg):
    """Append a timestamped line to the job's live log file. Also where a
    stopped job notices it should end (every step logs often)."""
    if job.get("cancel") and threading.current_thread().name == "worker":
        raise Cancelled()
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    try:
        with open(job["dir"] / "job.log", "a") as fh:
            fh.write(line)
    except OSError:
        pass


def clean_err(e):
    """User-facing error text — never leak server paths."""
    s = str(e)
    if ("speech.platform.bing.com" in s or "WSServerHandshakeError" in s
            or "Invalid response status" in s):
        return ("အသံထုတ်တဲ့ service (Microsoft) ခေတ္တ ပျက်နေတယ် — "
                "ပြန်ကောင်းအောင် ပြင်နေပါတယ်။ ခဏနေမှ ပြန်စမ်းပါ 🙏")
    return s.replace(str(BASE), "[app]").replace(str(JOBS), "[jobs]")[:300]


def _outro_on_parts(job, parts, d, outro, captions, blur, vw, vh, frame=None):
    """Every part ends with the channel outro ("like and follow…"), not only
    the last one (which already has it from the dub track)."""
    jlog(job, f"adding the outro to the end of {len(parts)} more part(s)…")
    png = cy = None
    if captions:
        try:
            cw, chh = (frame["cap_w"], frame["cap_h"]) if frame else (vw, vh)
            lst, ch = render_captions([(0.0, 5.0, outro)], cw, chh, d / "caps_outro", _settings["caption_size"],
                                      box=not blur)
            png = lst.parent / "c0.png"
            cy = frame_caption_y(frame, ch, blur) if frame else caption_y(ch, blur, captions.get("bottom", 0.08))
        except Exception as e:  # noqa: BLE001 - the voice still goes on without a caption
            jlog(job, f"outro caption skipped: {e}")
            png = None
    for n, p in enumerate(parts, 1):
        tmp = p.with_name(p.stem + "_o.mp4")
        try:
            add_outro_tail(p, d / "outro.mp3", tmp, png, cy)
            tmp.replace(p)
            _sp(job, 0.3 + 0.7 * n / len(parts))
        except Exception as e:  # noqa: BLE001 - keep the part without the outro
            tmp.unlink(missing_ok=True)
            jlog(job, f"outro on {p.name} failed: {str(e)[:150]}")
    shutil.rmtree(d / "caps_outro", ignore_errors=True)


def _hooks_on(job, d, names, cues=None, window=None):
    """⚡ Every video (and every part) opens with a 4-10 s teaser scene from inside it,
    picked by Gemini, then plays from the start. The pick is kept per length so
    the YouTube video and a one-part TikTok get the same moment."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not job.get("hook"):
        return
    from dub import add_hook, hook_span
    picks = job.setdefault("_hook_picks", {})
    for idx, n in enumerate(names):
        p = d / n
        try:
            dur = probe_duration(p)
            pc = (cues or [])[idx] if cues and idx < len(cues) else None
            if dur < 60:                       # too short for a teaser
                continue
            k = str(round(dur))
            if idx == 0 and "first" in picks and not window:
                k = "first"                    # TikTok part 1 = the YouTube video's teaser
            if k not in picks:
                lo_, hi_ = window or (0.0, dur)
                hi_ = min(hi_, dur)
                a, b, cnt = lo_ + (hi_ - lo_) * 0.12, lo_ + (hi_ - lo_) * 0.8, 40
                step = (b - a) / cnt
                # 🗣️ what is SAID at each picture too: the teaser plays with its voice — a gripping
                # line (a threat, a shock, a confession) makes the hook, not just a nice picture
                said = []
                for f in range(cnt):
                    t = a + step * (f + 0.5)
                    said.append(" / ".join(str(c[2]) for c in (pc or []) if len(c) > 2 and t - 3 <= c[0] <= t + 6)[:160])
                i = pick_hook(sample_frames(p, a, b, cnt, width=320), key, said)
                picks[k] = hook_span(p, a + step * (i + 0.5), dur, cues=pc)   # starts/ends with a caption line
                if window:
                    picks["first"] = picks[k]
            start, length = picks[k]
            tmp = p.with_name(p.stem + "_h.mp4")
            add_hook(p, start, tmp, length)
            tmp.replace(p)
            jlog(job, f"⚡ {n}: opens with the {start / 60:.1f}-min scene as a {length:.1f}s teaser")
        except Exception as e:  # noqa: BLE001 - keep the video without the teaser
            (p.with_name(p.stem + "_h.mp4")).unlink(missing_ok=True)
            jlog(job, f"teaser on {n} skipped: {str(e)[:150]}")


def _find_blur(job, src, sfx):
    """Where the video's own burned-in captions are, found by Gemini from a
    few frames: the band to blur (and to put the Burmese captions on), or
    None when there are none. Falls back to the saved position on errors."""
    key = os.environ.get("GEMINI_API_KEY")
    try:
        if not key:
            raise RuntimeError("no GEMINI_API_KEY")
        total = job["total"]
        blocks = find_caption_band(sample_frames(src, total * 0.05, total * 0.95, 10), key)
        if blocks is None:
            jlog(job, "no burned-in captions found — nothing to blur")
            return None
        if len(blocks) >= 2:
            # two subtitle tracks stacked (e.g. Chinese + English): blur only ONE of them —
            # the smaller one (less of the picture hidden; the lower one when about the same);
            # our Burmese captions go on it. Blurring both took ~35% of the picture.
            k = min(range(len(blocks)), key=lambda i: (round((blocks[i]["bottom"] - blocks[i]["top"]) / 0.03), -i))
            pick = blocks[k]
            lo = blocks[k - 1]["bottom"] + 0.005 if k > 0 else 0.0          # never into the track above…
            hi = blocks[k + 1]["top"] - 0.005 if k + 1 < len(blocks) else 1.0   # …or below
            top = max(pick["top"] - 0.015, lo)
            bot = min(pick["bottom"] + 0.02, hi)
            jlog(job, f"{len(blocks)} subtitle tracks ({', '.join(b['lang'] or '?' for b in blocks)}) — "
                      f"blurring only one: {pick['lang'] or '?'} ({pick['lines'] or '?'} line(s))")
        else:
            top, bot = blocks[0]["top"], blocks[0]["bottom"]
            # generous margin: a two-line subtitle reaches higher than the
            # sampled frames may show, and outlines/shadows spread a little
            # the model reads the top of a one-line subtitle; a two-line one starts
            # ~6-8% higher (Jaat: said 84-86%, really 78%)
            top, bot = max(0.0, top - (0.03 if blocks[0]["lines"] == 2 else 0.085)), min(1.0, bot + 0.02)
            if bot - top < 0.16:                             # at least two lines' worth
                top = max(0.0, bot - 0.16)
        # a film between bars (a portrait upload): the band is measured on the whole
        # frame, but must stay inside the picture and not cover most of it
        try:
            pa = picture_area(src, total * 0.1, total * 0.9)
        except Exception:  # noqa: BLE001
            pa = (0.0, 1.0)
        if pa != (0.0, 1.0):
            p0, p1 = pa
            ph = p1 - p0
            top, bot = max(top, p0), min(bot, p1)
            if bot - top > ph * 0.32:                    # at most ~1/3 of the picture, from its bottom
                top = bot - ph * 0.32
        jlog(job, f"original captions found at {top * 100:.0f}–{bot * 100:.0f}% of the height — blurring there")
        return {"bottom": 1 - bot, "height": bot - top}
    except Exception as e:  # noqa: BLE001 - never lose the dub over this
        bb, bh = _settings["blur_bottom" + sfx], _settings["blur_height" + sfx]
        jlog(job, f"caption search failed ({str(e)[:120]}) — blurring the default area "
                  f"(bottom {bb}%, {bh}% tall)")
        return {"bottom": bb / 100, "height": bh / 100}


def _find_sensitive(job, src, strict=False):
    """🫣 Blood / gore or nudity: [(start, end, x0, y0, x1, y1)] — only that part of the
    picture is blurred, like TV censoring. Gemini looks at a small frame every 2 s (in
    batches) and boxes the part; each hit covers ±1.5 s with a padded box (people move)."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return []
    from concurrent.futures import ThreadPoolExecutor
    step = 2.0
    try:
        jlog(job, "checking for blood / nudity scenes to blur…")
        times, frames = frames_every(src, job["total"], step)
        good = [(t, f) for t, f in zip(times, frames) if f]
        batches = [good[i:i + 40] for i in range(0, len(good), 40)]
        done = [0]

        def check(batch):
            hits = find_sensitive([f for _, f in batch], step, key, strict)
            done[0] += 1
            _sp(job, 0.5 * done[0] / max(1, len(batches)))
            return [(batch[i][0], box) for i, box in hits]
        with ThreadPoolExecutor(4) as ex:
            hits = sorted(h for part in ex.map(check, batches) for h in part)
    except Exception as e:  # noqa: BLE001 - the video goes on without it
        jlog(job, f"blood / nudity check skipped: {str(e)[:150]}")
        return []
    pad = 0.06                      # a margin around each box: the part moves between samples
    spans = []                      # [start, end, x0, y0, x1, y1]
    for t, (x0, y0, x1, y1) in hits:
        r = [max(0.0, t - 1.5), t + 1.5, max(0.0, x0 - pad), max(0.0, y0 - pad), min(1.0, x1 + pad), min(1.0, y1 + pad)]
        for s in spans:             # the same part a moment later: one region covering both
            if r[0] <= s[1] + 0.5 and r[2] < s[4] and s[2] < r[4] and r[3] < s[5] and s[3] < r[5]:
                s[1] = max(s[1], r[1])
                s[2], s[3], s[4], s[5] = min(s[2], r[2]), min(s[3], r[3]), max(s[4], r[4]), max(s[5], r[5])
                break
        else:
            spans.append(r)
    if spans:
        jlog(job, f"🫣 blurring {len(spans)} part(s) with blood / nudity "
                  f"({sum(s[1] - s[0] for s in spans):.0f} s in all, only those areas)")
    else:
        jlog(job, "no blood / nudity scenes found")
    return [tuple(s) for s in spans]


def _find_logos(job, src):
    """The video's own fixed logos/watermarks (found by Gemini), padded a little,
    to blur before our logo goes on. [] when none or on any error."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return []
    try:
        total = job["total"]
        # 1000 px wide: small corner logos and website text are missed at 640
        # 14 frames: a TikTok watermark that jumps between corners is caught in both places
        boxes = find_logos(sample_frames(src, total * 0.05, total * 0.95, 14, width=1000), key)
    except Exception as e:  # noqa: BLE001 - never lose the dub over this
        jlog(job, f"logo search failed ({str(e)[:120]}) — not blurring logos")
        return []
    # generous room: the model's boxes are often a bit tight, and a logo that
    # drifts a little or a second one right next to it must be covered too
    px, py = 0.025, 0.04
    boxes = [(max(0.0, x0 - px), max(0.0, y0 - py), min(1.0, x1 + px), min(1.0, y1 + py))
             for x0, y0, x1, y1 in boxes]
    merged = []                       # boxes that touch become one
    for bx in sorted(boxes):
        for i, m in enumerate(merged):
            if bx[0] <= m[2] and m[0] <= bx[2] and bx[1] <= m[3] and m[1] <= bx[3]:
                merged[i] = (min(m[0], bx[0]), min(m[1], bx[1]), max(m[2], bx[2]), max(m[3], bx[3]))
                break
        else:
            merged.append(bx)
    boxes = merged
    if boxes:
        jlog(job, "original logo(s) found — blurring " + ", ".join(
            f"x {x0 * 100:.0f}–{x1 * 100:.0f}% y {y0 * 100:.0f}–{y1 * 100:.0f}%" for x0, y0, x1, y1 in boxes))
    else:
        jlog(job, "no original logo found")
    return boxes


def _client_ip():
    # nginx sets X-Forwarded-For; first entry is the real client
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")


def _rate_ok(ip):
    now = time.time()
    with _ip_lock:
        dq = _ip_hits.get(ip)
        if dq is None:
            dq = _ip_hits[ip] = deque()
        while dq and now - dq[0] > 3600:
            dq.popleft()
        if len(dq) >= UPLOADS_PER_HOUR:
            return False
        dq.append(now)
        if len(_ip_hits) > 5000:  # don't let the table grow forever
            _ip_hits.clear()
        return True


# drop leftover job dirs from before a restart
for _old in JOBS.iterdir():
    try:
        if _old.is_dir() and time.time() - _old.stat().st_mtime > TTL:
            shutil.rmtree(_old, ignore_errors=True)
    except OSError:
        pass


# what a finished job needs to be shown again (history, after a restart)
_META_KEYS = ("id", "name", "title_mm", "status", "stage", "pct", "error", "ext", "parts",
              "part_times", "covers", "covers_tt", "tt", "cover_pick", "created", "owner", "recap", "tg", "url", "files_gone", "yt_child_id", "kind", "out_name", "fix_note", "fix_changed", "fix_appeal", "risk", "title_mm", "film_name", "episode", "orig_audio", "posts", "tk")


def _save_meta(job):
    try:
        meta = {k: job[k] for k in _META_KEYS if k in job}
        if (meta.get("tg") or {}).get("state") == "sending":   # can't resume a send
            meta["tg"] = {"state": "none"}
        (job["dir"] / "job.json").write_text(json.dumps(meta, ensure_ascii=False))
    except (OSError, TypeError, ValueError):
        pass


RESUME_KEEP = 3600     # a failed job's files stay this long, for ▶️ resume


def _save_failed(job):
    """Everything about a failed job (lines, recap beats, settings…) so it can be
    resumed even after a server restart."""
    try:
        meta = {}
        for k, v in job.items():
            if k == "dir":
                continue
            try:
                json.dumps(v)
                meta[k] = v
            except (TypeError, ValueError):
                pass
        (job["dir"] / "job.json").write_text(json.dumps(meta, ensure_ascii=False))
    except OSError:
        pass


def _load_saved_jobs():
    """Finished jobs from before a restart come back (until the TTL sweep)."""
    for f in JOBS.glob("*/job.json"):
        try:
            meta = json.loads(f.read_text())
            if meta.get("status") == "done" and time.time() - meta.get("created", 0) < (
                    KEEP_SMALL if meta.get("files_gone") or (meta.get("tk") or {}).get("want") else TTL):
                meta["dir"] = f.parent
                jobs.setdefault(meta["id"], meta)
            elif meta.get("status") in ("processing", "queued", "review"):
                # the server restarted mid-job: it comes back as ▶️ resumable
                meta.update(status="failed", failed_at=time.time(),
                            error="server ပြန်စလိုက်လို့ ရပ်သွားတယ် — ▶️ ဆက်လုပ်ပါ")
                meta["dir"] = f.parent
                jobs.setdefault(meta["id"], meta)
            elif meta.get("status") == "failed" and time.time() - meta.get("failed_at", 0) < RESUME_KEEP:
                meta["dir"] = f.parent                     # can still be resumed
                jobs.setdefault(meta["id"], meta)
        except (OSError, ValueError, KeyError):
            pass


_load_saved_jobs()


def sweep_old():
    now = time.time()
    for jid, job in list(jobs.items()):
        old = (now - job.get("failed_at", job["created"]) > RESUME_KEEP if job["status"] == "failed"
               else now - job["created"] > (KEEP_SMALL if job.get("files_gone") else TTL)
               and job["status"] in ("done", "review")
               and not (now - job["created"] < KEEP_SMALL and _tk_pending(job)))   # 📱 parts still for TikTok
        if old:
            shutil.rmtree(job["dir"], ignore_errors=True)
            jobs.pop(jid, None)
    # folders no job knows about: left by a restart in the middle of a job
    # (the source video, voice clips… — often gigabytes). Give a brand-new
    # upload a few minutes before its job entry exists.
    for d in JOBS.iterdir():
        try:
            if not d.is_dir() or d.name in jobs:
                continue
            # newest write inside (a slow phone upload keeps its file's time fresh)
            last = max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()])
            if now - last > 600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


sweep_old()                     # also right at start-up


def _queue_youtube_full(parent):
    """📺 YouTube "full": a second job, queued after this one, that dubs the whole
    film (music-only stretches left out, ~80% of it) as one 16:9 video. It reuses
    this job's video and transcript, so there is no second download or whisper."""
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    d.mkdir(parents=True, exist_ok=True)
    name = "input" + parent["ext"]
    try:
        os.link(parent["dir"] / name, d / name)       # same file, no copy
    except OSError:
        shutil.copy2(parent["dir"] / name, d / name)
    try:                                              # the same thumbnail-based covers
        shutil.copy2(parent["dir"] / "src_thumb.jpg", d / "src_thumb.jpg")
    except OSError:
        pass
    keep = ("ext", "direction", "voice", "model", "outro", "cover", "logo", "name", "telegram", "owner",
            "captions", "blur", "my_title", "no_title", "url", "segs", "seg_lang", "seg_engine", "lang", "total", "title_mm", "want_posts", "censor", "episode", "film_name", "hook", "cover_src")
    child = {k: parent[k] for k in keep if k in parent}
    # a LONG recap (narrated in our own words, ~80% of the film's length) — not a plain
    # dub of the whole film, which would look like a re-upload of someone else's video
    # at least 83% of the film's own story (job["total"] is already without the ads,
    # opening credits and end credits cut off in phase 1), aiming at ~85%
    import math
    long_min = max(parent.get("recap") or 0, math.ceil(parent.get("total", 0) * 0.83 / 60))
    child.update(status="queued", stage="queue", pct=0, error="", dir=d, id=jid, phase=1,
                 review=False, keep_bg=False, intro=False, recap=long_min, recap_aim=0.88 / 0.83, recap_strict=True, split=0,
                 youtube=True, yt_only=True, have_src=True, yt_child=True, yt_parent=parent["id"], tg_src=None,
                 created=time.time())
    jobs[jid] = child
    jlog(child, f"📺 YouTube long version of job {parent['id']}: a recap of at least {long_min} min "
                f"(83%+ of the {parent.get('total', 0) / 60:.0f}-min film, aiming ~88%)")
    jlog(parent, f"📺 YouTube full version queued as job {jid} — it runs after this one")
    parent["yt_child_id"] = jid
    _save_failed(child)          # survives a restart (comes back as ▶️ resumable)
    with lock:
        queue.append(jid)
        lock.notify()


def process_job(jid):
    job = jobs[jid]
    try:
        if job.get("kind") == "fix":
            _fix_job(job)
        elif job.get("kind") == "game":
            _game_job(job)
        elif job.get("phase") == 2:
            _voice_and_mux(job)
        else:
            _transcribe_and_translate(jid, job)
            job["phase1_done"] = True        # ▶️ resume skips straight to the voice
            _save_failed(job)                # snapshot (transcript + recap) in case of a restart
            if job.get("yt_full") and job.get("recap") and not job.get("yt_child"):
                _queue_youtube_full(job)
            if job.get("review"):
                job.update(status="review", stage="review", pct=55)
                jlog(job, "waiting for you to check the translation ✏️")
                return
            _voice_and_mux(job)
    except Cancelled:
        job.pop("cancel", None)
        jlog(job, "⏹ stopped by the user")
        job.update(status="failed", error="ရပ်လိုက်ပါပြီ", stage="cancelled")
        for f in job["dir"].iterdir():             # nothing to resume: free the disk
            if f.name != "job.log":
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 - surfaced to UI
        err = clean_err(e)
        jlog(job, f"FAILED: {type(e).__name__}: {err}")
        job.update(status="failed", error=err, failed_at=time.time())
        # keep everything for an hour so ▶️ resume can go on from here
        # (the sweep deletes it after RESUME_KEEP)
        _save_failed(job)


def _check_covers(job):
    """🖼️ Thumbnails can get a video limited too: Gemini looks at the covers for blood,
    nudity, betting logos… → job["cover_issues"] = {"cover1.jpg": ["blood"], …}."""
    key = os.environ.get("GEMINI_API_KEY")
    names = (job.get("covers") or []) + (job.get("covers_tt") or [])
    if not key or not names:
        return
    try:
        from PIL import Image as _Img
        import io as _io
        jpegs = []
        for n in names:
            im = _Img.open(job["dir"] / n).convert("RGB")
            im.thumbnail((640, 640))
            b = _io.BytesIO(); im.save(b, "JPEG", quality=85); jpegs.append(b.getvalue())
        res = check_covers(jpegs, key)
        job["cover_issues"] = {n: r for n, r in zip(names, res) if r}
        jlog(job, "cover check: " + ("; ".join(f"{n}: {', '.join(r)}" for n, r in job["cover_issues"].items())
                                      or "all covers look fine"))
    except Exception as e:  # noqa: BLE001 - only a note
        jlog(job, f"cover check skipped: {str(e)[:120]}")


_BIG_STUDIOS = re.compile(r"netflix|disney|marvel|warner|universal|paramount|sony|hbo|amazon|prime video|"
                          r"\bkbs\b|\bsbs\b|\bmbc\b|tvn|\bjtbc\b|t-series|yash raj|zee|star ?plus|"
                          r"iqiyi|youku|tencent|wetv|mango ?tv|viu|gma|abs-cbn", re.I)


def _risk_report(job):
    """⚠️ Before uploading: what could bring a claim, a warning or a strike, as
    [{"level": "high"|"mid"|"low", "text": Burmese}] (worst first)."""
    out = []
    add = lambda level, text: out.append({"level": level, "text": text})
    si = job.get("src_info") or {}
    ch = si.get("channel") or si.get("uploader") or ""
    big = bool(_BIG_STUDIOS.search(ch + " " + (job.get("name") or "")))
    if (si.get("license") or "").lower().startswith("creative commons"):
        add("low", f"Source က Creative Commons license ({ch}) — credit ပေးရင် ပြန်သုံးခွင့် ရှိတယ်")
    elif big or (si.get("channel_is_verified") and (si.get("channel_follower_count") or 0) > 200_000):
        add("high", f"Official / နာမည်ကြီး channel ({ch}) ကနေ ယူထားတယ် — copyright claim ခံရနိုင်ခြေ များတယ်")
    elif ch:
        add("mid", f"Source: {ch} — ရုပ်ရှင် ပိုင်ရှင်က claim လုပ်နိုင်တယ် (YouTube မှာ Private နဲ့ အရင် စစ်ပါ)")
    if job.get("orig_audio"):
        add("high", "🔊 မူရင်း အသံ mode — စာတန်း ထည့်ရုံမို့ TikTok က “unoriginal” လို့ သတ်မှတ်နိုင်တယ်: For You restrict + "
                    "Creator Rewards အပြီးပိတ် ဖြစ်နိုင်တယ် (YouTube မှာလည်း reused content → monetization မရ)။ "
                    "ပိုက်ဆံရမယ့် account မှာ မတင်ပါနဲ့ — 🎬 Recap ကို သုံးပါ")
    elif not job.get("recap"):
        add("mid", "Recap မဟုတ်ဘဲ ဗီဒီယို အပြည့် dub — reused content လို့ မြင်နိုင်တယ်")
    else:
        add("low", "Recap (ကိုယ်ပိုင် ဇာတ်ကြောင်းပြော) — original ဖြစ်မှု အကောင်းဆုံး ပုံစံ")
    if job.get("yt_child"):
        add("mid", "YouTube အရှည် version (ဇာတ်ကားရဲ့ 83%+) — claim ခံရနိုင်ခြေ ပိုများတယ်")
    for n, issues in (job.get("cover_issues") or {}).items():
        add("high", f"Cover ({n}) မှာ {', '.join(issues)} ပါနေတယ် — thumbnail ကြောင့် limit ခံရနိုင်တယ်၊ တခြား cover သုံးပါ")
    if job.get("censor"):
        n = job.get("censored") or 0
        add("low" if n else "low", f"🫣 သွေး/လိင် စစ်ပြီး — {n} နေရာ mosaic လုပ်ထားတယ်" if n else "🫣 သွေး/လိင် စစ်ပြီး — မတွေ့ပါ")
    elif job.get("recap") or not job.get("orig_audio"):
        add("mid", "🫣 သွေး/လိင် မစစ်ထားဘူး — action/horror ဇာတ်ကားဆိုရင် 🔧 ပြင်မယ် မှာ ဒီဗီဒီယိုကို တင်ပြီး "
                    "Mosaic \"အမြဲ လုပ်\" ရွေးပါ (ပြန်ထုတ်စရာ မလို · comedy ဆိုရင် လျစ်လျူရှုလို့ ရ)")
    if job.get("logos_hidden"):
        add("low", f"မူရင်း / လောင်းကစား logo {job['logos_hidden']} ခု ဖုံးထားပြီး")
    if _AI_WORDS.search(job.get("name") or "") or (job.get("posts") or {}).get("ai_film"):
        add("mid", "🤖 AI ရုပ်ရှင် ဖြစ်နိုင်တယ် — upload လုပ်တဲ့အခါ AI label ဖွင့်ပါ")
    order = {"high": 0, "mid": 1, "low": 2}
    return sorted(out, key=lambda r: order[r["level"]])


def _caption_times(job):
    """When the film's own (burned-in) captions are on screen, on the video being
    made: while someone talks in the film (its captions follow the speech) plus a little
    before and after. For a recap the film's talk is mapped through its clips. None when
    unknown (the band then stays on all the time)."""
    talk = sorted((s["start"], s["end"]) for s in (job.get("segs") or []) if s.get("end", 0) > s.get("start", 0))
    if not talk:
        return None
    pre, post = 0.4, 0.8
    if job.get("recap") and job.get("recap_clips"):
        out, t = [], 0.0
        for c in job["recap_clips"]:
            s, l = c[0], c[1]
            o = c[2] if len(c) > 2 else l
            k = o / l if l else 1.0
            for a, b in talk:
                if b + post > s and a - pre < s + l:
                    out.append((t + max(0.0, a - pre - s) * k, t + min(l, b + post - s) * k))
            t += o
    else:
        out = [(max(0.0, a - pre), b + post) for a, b in talk]
    out.sort()
    merged = []
    for a, b in out:                  # close gaps under 1.5 s: no flicker between lines
        if merged and a - merged[-1][1] < 1.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        elif b - a > 0.2:
            merged.append((a, b))
    return merged or None


def _use_thumb(job, thumb, src, key):
    """🖼️ Cover from the uploader's thumbnail, or from the video's own frames?
    cover_src: "thumb" / "frames" as chosen; "auto" (default) asks Gemini whether the
    thumbnail shows the main character well (old-film re-uploads often use a random
    frame of a minor character)."""
    mode = job.get("cover_src") or "frames"
    if mode in ("thumb", "frames"):
        jlog(job, "cover: " + ("the video's thumbnail (chosen)" if mode == "thumb" else "from the video's frames (chosen)"))
        return mode == "thumb"
    if not key:
        return True
    try:
        total = job["total"]
        frames = sample_frames(src, total * 0.08, total * 0.9, 16, width=480)
        good, why = thumb_is_good(thumb.read_bytes(), frames, key)
        jlog(job, f"cover: thumbnail {'looks good' if good else 'not used'} ({why})"
                  + ("" if good else " — making covers from the video's frames (main character)"))
        return good
    except Exception as e:  # noqa: BLE001 - the thumbnail then
        jlog(job, f"thumbnail check skipped: {str(e)[:100]}")
        return True


def _series_thumb(title):
    """Where a series' cover picture is kept (brand/series/<title>.jpg)."""
    key = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title).strip()[:80] or "series"
    return BRAND_DIR / "series" / f"{key}.jpg"


def _parse_ranges(text):
    """"1:20-1:45, 3:10–3:30" (or seconds) → [(80, 105), (190, 210)]."""
    out = []
    for a, b in re.findall(r"(\d+(?::\d{1,2}){0,2}(?:\.\d+)?)\s*[-–~to]+\s*(\d+(?::\d{1,2}){0,2}(?:\.\d+)?)", text or ""):
        def sec(v):
            n = 0.0
            for p in v.split(":"):
                n = n * 60 + float(p)
            return n
        if sec(b) > sec(a):
            out.append((sec(a), sec(b)))
    return out


def _fix_job(job):
    """🔧 A part that TikTok / YouTube / Facebook flagged: read the warning, then cut
    the named time ranges out and / or pixelate blood & nudity — on the finished
    video itself (frame, logo and captions stay), with the original part's cover."""
    d = job["dir"]
    src = d / "input.mp4"
    key = os.environ.get("GEMINI_API_KEY")
    total = probe_duration(src)
    job["total"] = total
    job.update(stage="fixcheck", pct=5)
    jlog(job, f"🔧 fixing {job.get('out_name')} ({total / 60:.1f} min)")
    plan = {"kind": "other", "cut": [], "censor": False, "fixable": True, "explain_mm": "", "advice_mm": "", "appeal": ""}
    shots = [p.read_bytes() for p in sorted(d.glob("shot*.jpg"))]
    if (job.get("fix_msg") or shots) and key:
        try:
            plan = analyze_warning(job.get("fix_msg"), total, key, shots)
            jlog(job, f"warning: {plan['kind']} · cut {plan['cut'] or 'nothing'} · mosaic {'yes' if plan['censor'] else 'no'}")
        except Exception as e:  # noqa: BLE001 - the ticked options still apply
            jlog(job, f"reading the warning failed: {str(e)[:150]}")
    cuts = plan["cut"] + _parse_ranges(job.get("fix_cuts"))
    censor_on = {"1": True, "0": False}.get(job.get("fix_censor"), plan["censor"])
    _sp(job, 1)
    job.update(stage="fixing", pct=40)
    cur = src
    if cuts:
        jlog(job, "cutting out " + ", ".join(f"{a // 60:.0f}:{a % 60:04.1f}–{b // 60:.0f}:{b % 60:04.1f}" for a, b in sorted(cuts)))
        cut_ranges(cur, d / "cut.mp4", cuts, lambda f: _sp(job, 0.4 * f))
        cur = d / "cut.mp4"
        job["total"] = probe_duration(cur)
    out = d / "dubbed.mp4"
    kind = plan.get("kind") or "other"
    # 🫣 sensitive / mature: a thorough scan (also graphic violence and sexual moments)
    spans = _find_sensitive(job, cur, strict=kind == "sensitive" or job.get("fix_censor") == "1") if censor_on else []
    # 🎰 betting / gambling: the video's own logos and ads erased (not our channel logo)
    hide = []
    if kind == "gambling":
        hide = [b for b in _find_logos(job, cur)
                if not ((b[0] + b[2]) / 2 > 0.72 and (b[1] + b[3]) / 2 < 0.30)]   # our logo's corner
    # 📰 misinformation: a "fictional film recap — not real news" banner + an opening card
    overlays = []
    if kind == "misinformation":
        try:
            vw, vh = video_size(cur)
            overlays = fiction_notice(vw, vh, d / "notice")
            jlog(job, "adding a \"fictional film recap — not real news\" notice")
        except Exception as e:  # noqa: BLE001
            jlog(job, f"notice skipped: {str(e)[:150]}")
    if spans or hide or overlays:
        edit_video(cur, out, censor=spans, hide=hide, overlays=overlays, progress=lambda f: _sp(job, 0.5 + 0.5 * f))
    elif cur != src:
        cur.replace(out)
    shutil.rmtree(d / "notice", ignore_errors=True)
    changed = bool(cuts or spans or hide or overlays)
    job["fix_changed"] = changed
    if not changed:
        # nothing in the picture was changed: the same video is NOT sent back (re-posting
        # it unchanged would only be flagged again) — just the explanation and the appeal
        jlog(job, "⚠️ editing the video can't fix this — sending the explanation and appeal text only")
    for f in ("cut.mp4", "input.mp4"):
        (d / f).unlink(missing_ok=True)
    note = [("🔧 " if changed else "⚠️ ဗီဒီယို ပြင်ရုံနဲ့ မဖြေရှင်းနိုင်ပါ — ") + (job.get("out_name") or "").replace(" (fixed)", "")]
    if plan["explain_mm"]:
        note += ["", plan["explain_mm"]]
    if not changed:
        note += ["", "❌ ဗီဒီယိုထဲမှာ ပြင်စရာ မတွေ့လို့ ဗီဒီယို အသစ် မထုတ်ပါ — အတူတူ ပြန်တင်ရင် ထပ်ပိတ်ခံရမှာပါ"]
    if plan.get("advice_mm"):
        note += ["", "👉 လုပ်ရမယ့် အဆင့်တွေ:", plan["advice_mm"]]
    if cuts:
        note += ["", "✂️ ဖြတ်ထုတ်လိုက်တာ: " + ", ".join(f"{a // 60:.0f}:{a % 60:02.0f}–{b // 60:.0f}:{b % 60:02.0f}" for a, b in sorted(cuts))]
    if spans:
        note += ["", f"🫣 mosaic လုပ်ထားတဲ့ နေရာ {len(spans)} ခု"]
    if hide:
        note += ["", f"🎰 လောင်းကစား / ကြော်ငြာ logo {len(hide)} ခု ဖျက်ထားတယ်"]
    if overlays:
        note += ["", "📰 \"ရုပ်ရှင် ဇာတ်လမ်းသာ၊ အဖြစ်မှန် သတင်း မဟုတ်\" ဆိုတဲ့ စာ ထည့်ထားတယ် (အပေါ်မှာ အမြဲ + အစ ၄ စက္ကန့်)"]
    if not changed and kind == "sensitive":
        note += ["", "🔍 AI က သွေး / အကြမ်းဖက် / လိင် အခန်း မတွေ့ပါ — ဇာတ်လမ်း အကြောင်းအရာ တစ်ခုလုံးကြောင့် ကန့်သတ်တာ ဖြစ်နိုင်တယ်"]
    if plan["appeal"]:
        done = []                                  # what was really changed, in plain words
        if cuts:
            done.append("removed " + ", ".join(f"{a // 60:.0f}:{a % 60:02.0f}-{b // 60:.0f}:{b % 60:02.0f}" for a, b in sorted(cuts)))
        if spans:
            done.append(f"pixelated {len(spans)} sensitive moment(s)")
        if hide:
            done.append("removed the betting / advertising logos")
        if overlays:
            done.append("added an on-screen notice that it is a recap of a fictional film, not real news")
        job["fix_appeal"] = plan["appeal"] + (" In this version I " + " and ".join(done) + "." if done else "")
        note += ["", "📝 Appeal (copy / paste):", job["fix_appeal"]]
    if changed:
        note += ["", "ဗီဒီယို အဟောင်းကို ဖျက်ပြီးမှ အသစ်ကို တင်ပါ (duplicate မဖြစ်အောင်)"]
    job["fix_note"] = "\n".join(note)
    (d / "fix.txt").write_text(job["fix_note"], encoding="utf-8")
    jlog(job, job["fix_note"])
    if not changed:                    # no new video: nothing goes to Telegram (the page shows why)
        job.update(status="done", stage="done", pct=100, files_gone=True)
        _save_meta(job)
        jlog(job, "done (explanation only) ✅")
        return
    if job.get("telegram"):
        job["tg"] = {"state": "sending", "error": "", "pct": 0}
    job.update(status="done", stage="done", pct=100)
    _save_meta(job)
    jlog(job, "done ✅")
    if job.get("telegram"):
        threading.Thread(target=_telegram_send, args=(job,), daemon=True).start()


MUSIC_DIR = BASE / "music"          # 🎮 royalty-free / licensed tracks for game highlights (mp3 / m4a / wav)
MEME_DIR = BASE / "memes"           # 😂 the owner's own / licensed short meme clips for game highlights
MEME_EXT = (".mp4", ".mov", ".webm", ".mkv", ".gif", ".m4v")


def _game_job(job):
    """🎮 A gameplay highlight (the player's own video): background music mixed in, the
    game's own sound kept but turned down, the channel logo on it. No captions, no voice."""
    import random
    d = job["dir"]
    src = d / ("input" + job["ext"])
    if job.get("url") and not src.exists():
        job.update(stage="download", pct=2)
        jlog(job, "downloading video from link…")
        download_url(job["url"], d, max_mb())
    if job.get("tg_src") and not src.exists():
        job.update(stage="download", pct=2)
        jlog(job, "downloading video from Telegram…")
        telegram.download_video(_settings.get("telegram_chat_id"), job["tg_src"], src, lambda f: None)
    track = None
    if job.get("game_music", True):
        tracks = [p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac", ".ogg")]
        if not tracks:
            raise ValueError("music folder ထဲမှာ သီချင်း မရှိသေးဘူး — copyright မရှိတဲ့ (royalty-free) သီချင်းတွေကို "
                             f"{MUSIC_DIR} ထဲ ထည့်ပါ (ဒါမှမဟုတ် 🎵 ကို ဖြုတ်ပါ)")
        track = random.choice(tracks)
    memes = None
    if job.get("game_meme"):
        # 😂 by situation: memes/kill, memes/fail, memes/teamfight, memes/objective, memes/outplay;
        # clips straight in memes/ fit anywhere
        memes = {}
        for p in MEME_DIR.rglob("*"):
            if p.suffix.lower() in MEME_EXT and not p.name.startswith("."):
                k = p.parent.name.lower() if p.parent != MEME_DIR else "any"
                memes.setdefault(k, []).append(p)
        for v in memes.values():
            random.shuffle(v)
        jlog(job, "😂 memes: " + (", ".join(f"{k} {len(v)}" for k, v in sorted(memes.items())) if memes else "none in the folder")
                  + " · our own Burmese meme cards where no clip fits")
    job["total"] = probe_duration(src)
    job.update(stage="mux", pct=40)
    jlog(job, "🎮 game highlight" + (f": music “{track.stem}”, game sound at 35%" if track else ": no music, game sound as it is"))
    from dub import game_edit, frames_every
    logo = LOGO_FILE if job.get("logo") and LOGO_FILE.exists() else None
    moments, kinds, idle = None, {}, []
    key = os.environ.get("GEMINI_API_KEY")
    if key and job.get("game_cut", True):
        # 👀 the AI watches the match (a frame every 2 s): the player's kills, team fights (even when the
        # player dies in them), Lord / Turtle, outplays — far better than going by loudness alone
        try:
            from concurrent.futures import ThreadPoolExecutor
            job.update(stage="recap", pct=15)
            jlog(job, "🎮 AI is watching the match for kills and team fights…")
            step = 2.0
            times, frames = frames_every(src, job["total"], step, width=512)
            good = [(t, f) for t, f in zip(times, frames) if f]
            batches = [good[i:i + 40] for i in range(0, len(good), 40)]
            hero = job.get("game_hero") or ""
            done = [0]

            def look(batch):
                hits = game_highlights([f for _, f in batch], step, key, hero)
                done[0] += 1
                _sp(job, done[0] / len(batches))
                return [(batch[n][0], sc + (0.5 if kind in ("kill", "easy") else 0), kind) for n, kind, sc in hits]
            with ThreadPoolExecutor(4) as ex:
                found = [h for part in ex.map(look, batches) for h in part]
            idle = sorted(t for t, _, k in found if k == "idle")
            found = [h for h in found if h[2] != "idle"]
            kinds = {t: k for t, _, k in found}
            # a fail is for a meme, not a reason to keep a stretch (its death screen follows)
            found = [(t, sc) for t, sc, k in found if k not in ("fail", "death", "ganked", "ambush", "stolen", "end")]
            if idle:
                jlog(job, f"🎮 {len(idle) * step:.0f}s of dead / shop / base / menu screens found — never in the highlight")
            if found:
                moments = sorted(found, key=lambda x: -x[1])
                kills = sum(1 for _, sc in found if sc % 1)
                jlog(job, f"🎮 {len(found)} moments worth keeping ({kills} with the player's kill)")
        except Exception as e:  # noqa: BLE001 - the game's sound then
            jlog(job, f"AI match check skipped ({str(e)[:120]}) — going by the game's sound")
    job["total"] = game_edit(src, d / "dubbed.mp4", track, cut=job.get("game_cut", True),
                             slowmo=job.get("game_slow", True), logo=logo, logo_size=_settings["logo_size"] / 100,
                             game_vol=0.35 if track else 1.0, log=lambda m: jlog(job, m), moments=moments,
                             memes=memes, kinds=kinds, idle=idle, idle_step=2.0, meme_cards=bool(job.get("game_meme")))
    src.unlink(missing_ok=True)
    if job.get("telegram"):
        job["tg"] = {"state": "sending", "error": "", "pct": 0}
    job.update(status="done", stage="done", pct=100)
    _save_meta(job)
    jlog(job, "done ✅")
    if job.get("telegram"):
        threading.Thread(target=_telegram_send, args=(job,), daemon=True).start()


def _find_part_cover(filename):
    """The cover of the part a re-uploaded file came from ("… - mm-2.mp4",
    "… - mm-tiktok-2.mp4"), while that job is still kept. Path or None."""
    for j in list(jobs.values()):
        if j.get("kind") == "fix" or j.get("status") != "done":
            continue
        try:
            for i, (_, name) in enumerate(_outputs(j), 1):
                if name == filename:
                    which, k = _out_kind(j, i)
                    return _cover_file(j, k, which)
        except Exception:  # noqa: BLE001
            continue
    return None


@app.post("/api/fix")
def create_fix_job():
    """🔧 Re-upload a flagged part with the platform's warning: only that part is fixed."""
    if not _admin_ok():
        return jsonify(error="admin password လိုတယ်"), 403
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="ပြင်မယ့် ဗီဒီယို ဖိုင်ကို ရွေးပါ"), 400
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    d.mkdir(parents=True)
    src = d / "input.mp4"
    f.save(src)
    try:
        if not has_video_stream(src):
            raise ValueError("ဗီဒီယိုဖိုင် မဟုတ်ဘူး")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=clean_err(e)), 400
    # screenshots of the warning: JPEGs (≤1600 px) for the model to read
    from PIL import Image as _Img
    for k, shot in enumerate(request.files.getlist("shots")[:5], 1):
        try:
            im = _Img.open(shot.stream).convert("RGB")
            im.thumbnail((1600, 1600))
            im.save(d / f"shot{k}.jpg", quality=88)
        except Exception:  # noqa: BLE001 - a bad image is just left out
            pass
    filename = os.path.basename(f.filename)
    stem = os.path.splitext(filename)[0]
    job = {"status": "queued", "stage": "queue", "pct": 0, "error": "", "dir": d, "ext": ".mp4", "id": jid,
           "kind": "fix", "name": stem, "out_name": stem + " (fixed).mp4", "owner": True,
           "fix_msg": (request.form.get("message") or "")[:4000], "fix_cuts": request.form.get("cuts") or "",
           "fix_censor": request.form.get("censor") or "auto",
           "telegram": request.form.get("telegram") == "1" and telegram.configured(),
           "created": time.time()}
    cover = _find_part_cover(filename)
    if cover:
        shutil.copy2(cover, d / "cover1.jpg")
        job["covers"] = ["cover1.jpg"]
    jobs[jid] = job
    n_shots = len(list(d.glob("shot*.jpg")))
    jlog(job, f"uploaded {filename} for fixing" + (f" · {n_shots} screenshot(s)" if n_shots else "")
              + (" · its cover found" if cover else " · its cover not found (job older than a week)"))
    with lock:
        queue.append(jid)
        lock.notify()
    return jsonify(job_id=jid)


def burmese_chunks(wav, piece=300):
    """The audio as 5-minute low-bitrate MP3s for Gemini: [(start, length, bytes)]."""
    import tempfile
    total = probe_duration(wav)
    out, t = [], 0.0
    with tempfile.TemporaryDirectory() as tmp:
        while t < total - 1:
            length = min(piece, total - t)
            f = Path(tmp) / "c.mp3"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-t", f"{length:.2f}", "-i", str(wav),
                            "-ac", "1", "-ar", "16000", "-b:a", "32k", str(f)], check=True)
            out.append((t, length, f.read_bytes()))
            t += piece
    return out


def _transcribe_and_translate(jid, job):
    d = job["dir"]
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("server config missing (GEMINI_API_KEY)")
    direction = job["direction"]
    jlog(job, f"job {jid} started · {direction} · {job['voice']} voice")
    have_src = bool(job.get("have_src")) and (d / ("input" + job["ext"])).exists()
    if have_src:
        jlog(job, "▶️ resuming: the video is already here")
    if job.get("url") and not have_src:
        job.update(stage="download", pct=2)
        jlog(job, "downloading video from link…")
        try:
            download_url(job["url"], d, max_mb())
        except Exception as e:  # noqa: BLE001
            jlog(job, f"download error: {str(e)[:300]}")
            raise ValueError("ဗီဒီယို ဒေါင်းမရဘူး — link စစ်ပါ၊ "
                             "ခဏနေ ပြန်စမ်းပါ") from None
        try:
            job["src_info"] = json.loads((d / "src_info.json").read_text())
            si = job["src_info"]
            jlog(job, f"source: {si.get('channel') or si.get('uploader') or '?'}"
                      + (" ✓ verified" if si.get("channel_is_verified") else "")
                      + (f" · {si['channel_follower_count']:,} followers" if si.get("channel_follower_count") else "")
                      + f" · licence: {si.get('license') or 'standard (all rights reserved)'}")
        except Exception:  # noqa: BLE001
            pass
    if job.get("tg_src") and not have_src:
        job.update(stage="download", pct=2)
        jlog(job, "downloading video from Telegram…")
        last = [0]
        def dl_prog(f):
            job["pct"] = 1 + int(3 * f)          # the bar and the ring show the download too
            job["dl_pct"] = int(f * 100)
            if f * 100 >= last[0] + 10:
                last[0] = int(f * 10) * 10
                jlog(job, f"Telegram download… {last[0]}%")
        try:
            telegram.download_video(_settings.get("telegram_chat_id"), job["tg_src"],
                                    d / ("input" + job["ext"]), dl_prog)
        except Exception as e:  # noqa: BLE001
            jlog(job, f"Telegram download error: {str(e)[:300]}")
            raise ValueError("Telegram ကနေ ဒေါင်းမရဘူး — bot ဆီ ပြန် forward လုပ်ပြီး ထပ်စမ်းပါ") from None
    src = d / ("input" + job["ext"])
    try:                                   # 📱 a phone video filmed sideways: turn its pixels upright
        from dub import make_upright
        if make_upright(src):
            jlog(job, "phone video with a rotation flag — turned upright")
    except Exception as e:  # noqa: BLE001
        jlog(job, f"rotation fix skipped: {str(e)[:120]}")
    total = probe_duration(src)
    job["total"] = total
    # re-uploads often open/close with casino, lottery and app ads: keep only the film
    # (▶️ resume: done already — unless that check failed the first time, e.g. no Gemini)
    a, b = (0.0, total) if have_src and job.get("bounds_done", True) else _movie_bounds(job, api_key)
    if a > 1.0 or b < total - 1.0:
        try:
            cut = d / ("film" + job["ext"])
            k = trim_copy(src, a, b if b < total - 1.0 else None, cut)
            cut.replace(src)
            total = probe_duration(src)
            job["total"] = total
            jlog(job, f"kept only the film: {k / 60:.1f}–{(k + total) / 60:.1f} min of the upload")
        except Exception as e:  # noqa: BLE001 - go on with the whole upload
            jlog(job, f"cutting the ads off failed: {str(e)[:150]}")
    job["have_src"] = True
    if job.get("recap") and job.get("recap_auto") and not job.get("yt_child"):
        # 🤖 a real recap, not most of the film: ~30% of its story (ads and credits already cut
        # off), 2–25 min. Short parts keep TikTok viewers to the end, and less of the film's
        # footage is reused (more of the video is our own telling). 55% made a 2-hour film a
        # 66-minute "recap".
        job["recap"] = max(2, min(25, round(total * 0.28 / 60)))
        job["recap_aim"] = 1.15
        job["recap_strict"] = False
        jlog(job, f"🤖 auto length: a recap of ~{job['recap']}–{round(job['recap'] * 1.15)} min "
                  f"(about 30% of the {total / 60:.0f}-min film, at most 25 min)")
    elif job.get("recap") and not job.get("yt_child") and job["recap"] * RECAP_AIM * 60 >= total * 0.85:
        # the chosen length is about as long as the video itself: still a recap (narrated
        # all the way through — a plain dub left every scene without dialogue silent),
        # as close to the chosen length as the video allows (at most ~88% of it)
        mins = max(1, min(job["recap"], int(total * 0.83 / 60)))
        job["recap_aim"] = (total * 0.88 / 60) / mins
        jlog(job, f"video is {total / 60:.0f} min — a recap of at least {mins} min "
                  f"(aiming ~{total * 0.88 / 60:.0f}), narrated all the way through")
        job["recap"] = mins
    jlog(job, f"video duration: {total:.1f}s")
    if not has_video_stream(src):
        raise ValueError("ဗီဒီယိုဖိုင် မဟုတ်ဘူး")
    if not has_audio_stream(src):
        raise ValueError("ဒီဗီဒီယိုမှာ အသံမပါဘူး — အသံပါတဲ့ ဗီဒီယိုမှ dub လို့ရမယ်")

    wav = d / "audio.wav"
    if not (job.get("segs") and wav.exists()):
        job.update(stage="audio", pct=4)
        jlog(job, "extracting audio…")
        extract_audio(src, wav, total, lambda f: _sp(job, f))

    job.update(stage="transcribe", pct=8)
    jlog(job, "transcribing with whisper…")
    last_log = [0]
    def stt_prog(frac):  # 8% → 44% while whisper works through the audio
        job.update(pct=8 + int(36 * frac))
        _sp(job, frac)
        if frac * 100 >= last_log[0] + 10:
            last_log[0] = int(frac * 10) * 10
            jlog(job, f"transcribing… {last_log[0]}%")
    if job.get("segs") and wav.exists():          # ▶️ resume: whisper already ran
        segs, lang = job["segs"], job["seg_lang"]
        jlog(job, "▶️ resuming: speech already transcribed")
    elif job.get("seg_lang") == "my" and wav.exists():   # known Burmese: Gemini listens, no whisper
        segs, lang = [], "my"
    else:
        jlog(job, f"speech model: {job.get('model') or default_whisper_model()}")
        segs, lang = transcribe(wav, SRC_LANG[direction], stt_prog, job.get("model"), title=job.get("name") or "",
                                words=bool(job.get("orig_audio") or job.get("char_voice")))   # exact word times
        job["segs"], job["seg_lang"] = segs, lang
    if lang == "my" and (job.get("seg_engine") != "gemini" or not segs):
        # whisper writes Burmese speech as junk ("වවවව…"): let Gemini listen instead
        jlog(job, "Burmese speech — transcribing with Gemini (whisper can't write Burmese)…")
        def g_prog(i, n):
            job.update(pct=8 + int(36 * i / max(n, 1)))
            _sp(job, i / max(n, 1))
            jlog(job, f"transcribing (gemini)… {i}/{n}")
        try:
            segs = transcribe_burmese(burmese_chunks(wav), api_key, g_prog)
        except RateLimited as e:
            raise ValueError("Gemini quota ဒီနေ့အတွက် ကုန်သွားပြီ — မနက်ဖြန် ပြန်စမ်းပါ" if e.daily else
                             "Gemini က ခဏ ပိတ်ထားတယ် — ၁–၂ မိနစ်နေရင် ▶️ ဆက်လုပ်မယ် ကို နှိပ်ပါ") from None
        except RuntimeError as e:          # every piece failed: why (VPN country, network…)
            raise ValueError(str(e)[:400]) from None
        if segs:                           # ▶️ resume asks Gemini again when it found nothing
            job["segs"], job["seg_engine"] = segs, "gemini"
    if not segs:
        raise ValueError("အသံထဲမှာ စကားပြောသံ မတွေ့ဘူး")
    if direction == "auto2mm":
        jlog(job, f"detected language: {lang}")
        if lang == "my" and not job.get("recap") and not job.get("orig_audio") and not job.get("char_voice"):
            raise ValueError("ဒီဗီဒီယိုက မြန်မာလို ပြောထားပြီးသားပါ — မြန်မာ ရုပ်ရှင်ဆို "
                             "🎬 Recap ကို ရွေးပါ (AI အသံနဲ့ ဇာတ်ကြောင်းပြော recap လုပ်ပေးမယ်)")
        if lang == "my" and job.get("orig_audio"):
            jlog(job, "Burmese film — its own sound stays; captions are what is said")
        elif lang == "my" and job.get("scene_recap"):
            # a Burmese film: no dubbing its own language — the narrated recap instead
            job.update(char_voice=False, scene_recap=False, commentary=False,
                       recap=max(2, min(25, round((job.get("total") or 0) * 0.28 / 60)))
                       if job.get("scene_auto") else job.get("scene_min") or 20)
            jlog(job, "Burmese film — a narrated recap instead (its own language isn't dubbed)")
        elif lang == "my" and job.get("char_voice"):
            # a Burmese film keeps its actors' own voices: original sound + Burmese captions, and our
            # narrator explains the quiet scenes (the film's sound turned down meanwhile)
            job["char_voice"] = False
            job["orig_audio"] = True
            job["commentary"] = True
            jlog(job, "Burmese film — the actors' own voices stay; captions + 🎙️ narrator remarks in the quiet scenes")
        elif lang == "my":
            jlog(job, "Burmese film — writing a Burmese recap (no translation; the original voices are dropped)")
        direction = {"zh": "zh2mm", "en": "en2mm"}.get(lang, "any2mm")
    # 🔊 captions only: short lines that follow the speech (a dub needs longer, speakable ones)
    if job.get("orig_audio"):
        utter = merge_segments(segs, max_gap=0.3, max_len=5.0)
    elif job.get("char_voice"):          # 🗣️ line by line: each voice line sits on its own speech
        # whole sentences (a line cut in two was rushed, stopped dead, then went on): longer pieces
        # (word-timed pieces: joined over a short breath into one sentence, but at most ~7 s so
        # a line the voice says faster never leaves more than ~3 s of the actor talking alone)
        utter = join_fragments(fix_dangling(merge_segments(segs, max_gap=1.2, max_len=7.0), near=3.0))   # "…抓的 你"|"對…" → whole sentences
        try:                             # 🎯 onto the real talking: in sync, and the true speaking time
            sw = JOBS / job["id"] / "audio.wav"
            utter, moved = snap_to_speech(utter, speech_regions(sw if sw.exists() else wav))
            jlog(job, f"🎯 {moved} of {len(utter)} lines moved onto the actors' real speech")
        except Exception as e:  # noqa: BLE001 - Whisper's times then
            jlog(job, f"speech timing skipped: {str(e)[:100]}")
    else:
        utter = merge_segments(segs)
    jlog(job, f"transcribed {len(segs)} segments → {len(utter)} utterances")
    if job["voice"] == "auto":
        try:
            g, f0 = detect_gender(wav, segs)
        except Exception as e:  # noqa: BLE001 - never fail a job over this
            g, f0 = "female", 0.0
            jlog(job, f"voice detection error: {e}")
        job["voice"] = g
        job["voice_auto"] = True               # 👥 each line gets its speaker's voice (male / female)
        jlog(job, f"auto voice: {g} (speaker pitch ≈ {f0:.0f} Hz)")

    if job.get("recap"):
        _write_recap(job, utter, lang, api_key)
        utter = job["utter"]
    elif job.get("orig_audio") and lang == "my":
        job["utter"] = utter                       # already Burmese: captions as spoken
    else:
        _translate(job, utter, direction, lang, api_key)
    if job.get("intro"):
        try:
            job["intro_text"] = write_intro([u["text"] for u in utter], api_key)
            jlog(job, f"intro: {job['intro_text']}")
        except Exception as e:  # noqa: BLE001 - the dub works without it
            job["intro_text"] = ""
            jlog(job, f"intro skipped: {e}")
    if job.get("commentary") and not job.get("recap") and "comments" not in job and not job.get("char_voice") \
            and not job.get("orig_audio"):
        _write_comments(job, api_key)        # (character-voice: after the cut, with pictures — see _voice_and_mux)
    if job.get("cover") and job.get("no_title"):
        job["title_mm"] = ""
        jlog(job, "no title on the cover / TikTok frame (unticked)")
    elif job.get("yt_child") and job.get("title_mm"):
        jlog(job, f"cover title (same as the TikTok version): {job['title_mm']}")
    elif job.get("cover"):
        try:
            # the title typed on the form wins; else a known film keeps its real
            # name on the cover, so people can search for it
            name = job.get("my_title") or ""
            film = ""
            try:                          # the film's real name (also for the file name)
                film = movie_name(job.get("name") or "", api_key) or ""
            except Exception as e:  # noqa: BLE001 - fall back to a made-up Burmese title
                jlog(job, f"movie name lookup failed: {str(e)[:120]}")
            job["film_name"] = film
            if name:
                jlog(job, f"cover title (typed): {name}")
            else:
                name = film
            if job.get("my_title"):
                job["title_mm"] = name
            elif name:
                job["title_mm"] = name
                jlog(job, f"cover title (the movie's name): {name}")
            else:
                job["title_mm"] = write_title([u["text"] for u in utter], api_key)
                jlog(job, f"cover title: {job['title_mm']}")
            if AI_COVER:
                try:
                    job["cover_scene"] = write_cover_scene([u["text"] for u in utter], api_key)
                    jlog(job, f"cover picture idea: {job['cover_scene'][:160]}…")
                except Exception as e:  # noqa: BLE001 - the photo collage is used instead
                    jlog(job, f"cover picture idea skipped: {e}")
        except Exception as e:  # noqa: BLE001
            job["title_mm"] = ""
            jlog(job, f"cover title skipped: {e}")


def _ends_with_channel_promo(utter, total):
    """True when the last minute or so already names the owner's channel
    (the translator swapped it in for the original creator's sign-off)."""
    name = (_settings["channel_name"] or "").casefold()
    if not name:
        return False
    tail_from = max(0.0, total - max(60.0, total * 0.1))
    return any(name in (u.get("text") or "").casefold()
               for u in utter if u["end"] >= tail_from)


def _write_comments(job, api_key, utter=None, video=None):
    """🎙️ Narrator remarks for a no-recap video: one in the longest quiet pause of every
    ~2½ minutes, written by Gemini to fit that pause. Our own telling on top of the film.
    🗣️ Character-voice (utter / video of the cut film given): EVERY pause of 3 s or more,
    Gemini seeing a picture of it — the voice goes on all the way, like a recap."""
    from dub import comment_gaps
    every_pause = utter is not None
    utter = utter if every_pause else (job.get("utter") or [])
    utter = [u for u in utter if (u.get("text") or "").strip()]
    if every_pause and job.get("char_voice"):   # the Burmese voice ends sooner than the line: the quiet after it counts
        utter = [{**u, "end": u["start"] + u["vdur"] + 0.3 if u.get("vdur") else
                  min(u["end"], u["start"] + len(u["text"]) / gemini.TTS_CPS + 0.4)} for u in utter]
    job["comments"] = []
    try:
        if every_pause:
            gaps = comment_gaps(utter, job.get("total") or 0, every=0,
                                min_gap=4.0 if job.get("orig_audio") else 3.0, first=2.0)
        else:
            gaps = comment_gaps(utter, job.get("total") or 0, every=150.0)
        if every_pause and job.get("orig_audio") and video and gaps:
            # 🔊 the actors' own voices play: a pause is only where the SOUND is quiet too
            # (whisper misses lines — the narrator must not talk over them)
            try:
                wav = job["dir"] / ("audio_cut.wav" if Path(video).name == "condensed.mp4" else "audio.wav")
                if not wav.exists():
                    extract_audio(video, wav)
                talk = speech_regions(wav)
                free = []
                for a, b, i in gaps:
                    best, t = None, a
                    for x, y in [r for r in talk if r[1] > a and r[0] < b] + [(b, b)]:
                        if x - t >= 4.0 and (not best or x - t > best[1] - best[0]):
                            best = (t, x)
                        t = max(t, y)
                    if best:
                        free.append((best[0] + 0.2, best[1] - 0.2, i))
                gaps = free
            except Exception as e:  # noqa: BLE001 - the transcript's pauses then
                jlog(job, f"quiet check skipped: {str(e)[:100]}")
        if every_pause:
            # ⚔️ a long scene without talk (a fight, a chase…): a line every ~4½ s, each from its own
            # picture — the narrator tells the action as it goes, no stopping in between (one line
            # per ~12 s left 7–9 s of quiet: a remark is ~3–4 s spoken at 1.3x)
            split = []
            for a, b, i in gaps:
                n = max(1, round((b - a + 0.6) / 5.1))
                w = (b - a - 0.6 * (n - 1)) / n
                split += [(a + k * (w + 0.6), a + k * (w + 0.6) + w, i) for k in range(n)]
            gaps = split
        if not gaps:
            jlog(job, "🎙️ no quiet pauses long enough for narrator remarks")
            return
        pauses = [{"before": [u["text"] for u in utter[max(0, i - 2):i + 1]],
                   "after": [u["text"] for u in utter[i + 1:i + 3]],
                   "max": max(12, int((b - a) * gemini.TTS_CPS * 0.85))} for a, b, i in gaps]
        if every_pause and video:
            from concurrent.futures import ThreadPoolExecutor
            from dub import sample_frames
            with ThreadPoolExecutor(8) as ex:
                pics = list(ex.map(lambda g: (sample_frames(video, g[0], g[1], count=1, width=384) or [None])[0],
                                   gaps))
            for p, j in zip(pauses, pics):
                p["jpeg"] = j
        cast = job.get("cast_mm")
        if every_pause and cast is None:     # 👤 names for "X နဲ့ Y ချနေကြပါပြီ" (who is fighting whom)
            try:
                cast = write_cast(list(enumerate(u["text"] for u in utter)), api_key, None,
                                  title=job.get("name") or "")
            except Exception as e:  # noqa: BLE001 - roles then
                cast = ""
                jlog(job, f"character list skipped: {str(e)[:100]}")
            job["cast_mm"] = cast
        texts = write_comments(pauses, api_key, describe=bool(job.get("char_voice") or every_pause),
                               cast=cast or "")
        job["comments"] = [{"start": round(a, 3), "end": round(b, 3), "text": t}
                           for (a, b, _), t in zip(gaps, texts) if t]
        if every_pause:
            job["comments_cut"] = True           # already on the cut film's timeline
            jlog(job, f"🎙️ {len(job['comments'])} narrator lines written for the scenes without talk "
                      f"(of {len(gaps)} pauses)")
        else:
            jlog(job, f"🎙️ {len(job['comments'])} narrator remarks written (one every ~2½ min, in the pauses)")
    except Exception as e:  # noqa: BLE001 - the video works without them
        jlog(job, f"narrator remarks skipped: {str(e)[:150]}")


def _translate(job, utter, direction, lang, api_key):
    job.update(stage="translate", pct=45)
    jlog(job, "translating with gemini…")
    genders = None
    wav = JOBS / job["id"] / "audio.wav"
    if direction != "mm2en" and wav.exists():
        try:                      # 👥 who says each line (voice pitch): a woman's line says ကျွန်မ / ရှင့်, not ဗျ
            genders = line_genders(wav, utter)
            jlog(job, f"👥 speakers by voice: {genders.count('male')} lines male, "
                      f"{genders.count('female')} female — the translation uses ကျွန်တော်/ဗျ or ကျွန်မ/ရှင့် to match")
        except Exception as e:  # noqa: BLE001 - the story context still guides it
            jlog(job, f"speaker detection skipped: {str(e)[:120]}")
    try:
        texts = translate_lines([u["text"] for u in utter], direction, api_key,
                                LANG_NAMES.get(lang, lang), _settings["channel_name"] or None,
                                prog=lambda f: _sp(job, f), title=job.get("name") or "",
                                short=bool(job.get("char_voice")), genders=genders,
                                durs=[u["end"] - u["start"] for u in utter] if job.get("char_voice") else None)
    except RateLimited as e:
        raise ValueError("Gemini ဘာသာပြန် quota ဒီနေ့အတွက် ကုန်သွားပြီ — မနက်ဖြန် ပြန်စမ်းပါ"
                         if e.daily else
                         "Gemini က ခဏ ပိတ်ထားတယ် (request များလွန်း) — ၁–၂ မိနစ်နေရင် 🔁 ပြန်စမ်းပါ") from None
    for u, t in zip(utter, texts):
        u["orig"] = u["text"]
        u["text"] = t or u["text"]
    job["utter"] = utter
    jlog(job, f"translated {len(texts)} lines")


RECAP_CHOICES = (0, 15, 20, 30, 40)   # recap: narration at least this many minutes (intro/outro not counted); 0 = full dub
RECAP_AIM = 1.25                      # aim a bit over the minimum (20 → ~25 min)


def _movie_bounds(job, api_key):
    """Where the film itself starts and ends: re-uploads often open (and close)
    with casino / lottery / app ads and channel intros that have speech too.
    Gemini looks at frames from the first and last minutes. (0, total) on error."""
    src = job["dir"] / ("input" + job["ext"])
    total = job["total"]
    if total < 600:                       # a short clip has no ad block worth looking for
        job["bounds_done"] = True
        return 0.0, total
    try:
        hs, ts = min(480.0, total * 0.15), min(600.0, total * 0.12)   # end credits can run long
        nh, nt = 24, 16
        head = sample_frames(src, 0.0, hs, nh, width=480)
        tail = sample_frames(src, total - ts, total, nt, width=480)
        si, ei = find_movie_bounds(head, tail, api_key)
        step_h, step_t = hs / nh, ts / nt
        start = 0.0 if si == 0 else step_h * (si + 0.5) - step_h / 2
        end = total if ei >= nt - 1 else total - ts + step_t * (ei + 0.5) + step_t / 2
        if start:
            jlog(job, f"the story starts at {start / 60:.1f} min — skipping the ads / opening credits before it")
        if end < total:
            jlog(job, f"the story ends at {end / 60:.1f} min — skipping the end credits / ads after it")
        job["bounds_done"] = True
        return start, end
    except Exception as e:  # noqa: BLE001 - use the whole video
        jlog(job, f"film start/end check skipped: {str(e)[:120]}")
        job["bounds_done"] = False        # ▶️ resume tries it again
        return 0.0, total


def _write_recap(job, utter, lang, api_key):
    """Recap mode: Gemini writes a shorter Burmese narration in beats, each
    tied to the scene (transcript lines) it describes."""
    job.update(stage="translate", pct=45)
    mins = job["recap"] * job.get("recap_aim", RECAP_AIM)
    jlog(job, f"writing a Burmese recap of at least {job['recap']} min (aiming ~{mins:.0f}) with gemini…")
    try:
        beats = write_recap([(u["start"], u["text"]) for u in utter], mins, api_key,
                            LANG_NAMES.get(lang, lang), _settings["channel_name"] or None,
                            min_minutes=job["recap"], log=lambda m: jlog(job, m),
                            slack=0 if job.get("recap_strict") else 5,
                            prog=lambda f: _sp(job, f), title=job.get("name") or "")
    except RateLimited as e:
        raise ValueError("Gemini quota ဒီနေ့အတွက် ကုန်သွားပြီ — မနက်ဖြန် ပြန်စမ်းပါ" if e.daily else
                         "Gemini က ခဏ ပိတ်ထားတယ် — ၁–၂ မိနစ်နေရင် 🔁 ပြန်စမ်းပါ") from None
    if not beats:
        raise ValueError("Recap ရေးလို့ မရဘူး — ပြန်စမ်းပါ")
    job["speech"] = speech_spans(utter)     # talking stretches: recap scenes come only from these
    job["utter"] = [{"start": utter[b["from"]]["start"], "end": utter[b["to"]]["end"],
                     "text": b["say"][:MAX_LINE],
                     "orig": " ".join(u["text"] for u in utter[b["from"]:b["to"] + 1])[:300]}
                    for b in beats]
    est = sum(len(u["text"]) for u in job["utter"]) / gemini.TTS_CPS / 60
    jlog(job, f"recap: {len(beats)} beats · ~{est:.1f} min of narration (need {job['recap']}+)"
              + ("" if est >= job["recap"] else " ⚠️ still short"))


def _recap_cut(job, voice_name):
    """Voice each beat, then cut the matching scenes to the narration's length
    and join them: returns (recap video, utterances on the new timeline)."""
    d = job["dir"]
    src = d / ("input" + job["ext"])
    utter = job["utter"]
    job.update(stage="voice", pct=58)
    jlog(job, f"voicing {len(utter)} recap beats ({voice_name})…")
    from concurrent.futures import ThreadPoolExecutor
    done = [0]
    def tts(i):
        p = d / f"r{i}.mp3"
        if not (p.exists() and p.stat().st_size > 0):      # ▶️ resume keeps voiced lines
            synthesize(utter[i]["text"], voice_name, p)
            trim_silence(p)                                  # no dead air before / after the beat
        done[0] += 1
        job.update(pct=58 + int(20 * done[0] / len(utter)))
        _sp(job, done[0] / len(utter))
        return p, probe_duration(p)
    with ThreadPoolExecutor(max(1, int(os.environ.get("DUBLAB_TTS_WORKERS") or 8))) as ex:
        voiced = list(ex.map(tts, range(len(utter))))
    GAP = 0.35
    spans = job.get("speech") or []
    # each beat's scenes come from ITS OWN part of the film (until the next beat's part
    # begins), so what is shown matches what is being said
    clips = []
    for k, (u, (_, dur)) in enumerate(zip(utter, voiced)):
        nxt = utter[k + 1]["start"] if k + 1 < len(utter) and utter[k + 1]["start"] > u["start"] else u["end"]
        clips += beat_footage(u["start"], max(u["end"], nxt), dur + GAP, spans)
    job.update(stage="recap", pct=78)
    jlog(job, f"cutting {len(clips)} scenes into the recap…")
    def prog(i, n):
        job.update(pct=78 + int(12 * i / max(n, 1)))
        _sp(job, i / max(n, 1))
    out = d / "recap.mp4"
    total = build_recap_video(src, clips, out, d / "clips", prog)
    new, t = [], 0.0
    for u, (p, dur) in zip(utter, voiced):
        new.append({"start": t, "end": t + dur, "text": u["text"], "audio": str(p)})
        t += dur + GAP
    jlog(job, f"recap video: {total / 60:.1f} min")
    job["recap_clips"] = clips          # (film start, length[, played length]) in recap order
    return out, new, total


def _condense(job, src, utter, voice=None):
    """The film with its long stretches without speech (songs, dances, montages,
    credits) cut out, and the lines moved onto the new timeline. Kept as long as
    at least 60% of the film remains."""
    char = bool(job.get("char_voice"))
    orig = bool(job.get("orig_audio"))
    try:         # the uncut film's own length: job["total"] is the CUT length after a first run, and a
        job["total"] = probe_duration(src)          # resume cut the cut film again (40 → 24 → 14 min)
    except Exception:  # noqa: BLE001
        pass
    if orig:     # 🎞️ no recap, the film's own voices: songs, dances and long silent scenes go (~85% stays).
        # The talking comes from the sound itself too (whisper misses lines; a cut must never take real talk)
        talk = [{"start": u["start"], "end": u["end"]} for u in utter]
        try:
            wav = job["dir"] / "audio.wav"
            if not wav.exists():
                extract_audio(src, wav)
            talk += [{"start": a, "end": b} for a, b in speech_regions(wav) if b - a >= 0.6]
        except Exception as e:  # noqa: BLE001 - the transcript alone then
            jlog(job, f"speech check skipped: {str(e)[:100]}")
        spans = keep_most(talk, job["total"], keep=0.85)
    elif char:   # 🎬 recap: talk is what matters — every pause is cut to ~1 s (a jump cut), a long
        # fight / chase keeps 5–8 s with one narrator remark on it
        # A line lasts as long as its BURMESE voice (at the speed chosen in Settings): the lines are voiced
        # first to know it — the actor still talking after the voice ends keeps under ½ s, then a jump cut
        said = [u for u in utter if (u.get("text") or "").strip()]
        try:
            jlog(job, f"🎙️ voicing {len(said)} lines first, so the cut follows the Burmese voice "
                      f"(speed {_settings['speak_speed']:.2f}x)…")
            lens = voice_lengths(said, voice or VOICES[job["direction"]][job["voice"]], job["dir"],
                                 lambda i, n: _sp(job, i / max(n, 1)))
        except Exception as e:  # noqa: BLE001 - the length estimated from the text then
            jlog(job, f"voice lengths estimated ({str(e)[:80]})")
            lens = [len(u["text"]) / gemini.TTS_CPS for u in said]
        for u, vd in zip(said, lens):
            u["vdur"] = round(vd, 2)
        spans = tight_spans([{"start": u["start"], "end": u["start"] + max(vd, min(u["end"] - u["start"], vd + 0.4)) + 0.1}
                             for u, vd in zip(said, lens)], job["total"])
    else:
        spans = speech_spans(utter, pad=1.5, join=8.0)
    kept = sum(b - a for a, b in spans)
    total = job["total"]
    if not spans or kept < total * (0.15 if char else 0.6) or kept > total * 0.97:
        jlog(job, "keeping the whole film (little to leave out)")
        return src, utter
    job.update(stage="recap", pct=55)
    jlog(job, ("🎬 recap: cutting the opening, ending, dances and long scenes without talk" if char
               else "🎞️ cutting songs, dances and long scenes without talk" if orig else "leaving out music-only parts")
         + f": {total / 60:.0f} → {kept / 60:.0f} min ({kept * 100 / total:.0f}%)")
    out = job["dir"] / "condensed.mp4"
    def prog(i, n):
        job.update(pct=55 + int(5 * i / max(n, 1)))
        _sp(job, i / max(n, 1))
    new_total = build_recap_video(src, [(a, b - a) for a, b in spans], out, job["dir"] / "clips", prog)
    if orig:              # 🔊 the film's own voices stay: the cut needs its sound (the picture-only cut had none)
        add_cut_audio(src, spans, out)
    starts, t = [], 0.0
    for a, b in spans:
        starts.append((a, b, t))
        t += b - a
    moved = []
    for u in utter:
        for a, b, t0 in starts:
            if a <= u["start"] < b:
                moved.append({**u, "start": t0 + u["start"] - a, "end": t0 + min(u["end"], b) - a})
                break
    if job.get("comments") and not job.get("comments_cut"):   # 🎙️ remarks follow their scene (or go with it)
        cm = []
        for c in job["comments"]:
            for a, b, t0 in starts:
                if a <= c["start"] and c["end"] <= b + 0.5:
                    cm.append({**c, "start": t0 + c["start"] - a, "end": t0 + min(c["end"], b) - a})
                    break
        job["comments"] = cm
    job["total"] = new_total
    return out, moved


def _voice_and_mux(job):
    d = job["dir"]
    utter = job["utter"]
    voice_name = VOICES[job["direction"]][job["voice"]]
    src = d / ("input" + job["ext"])
    posts = _start_posts(job) if not job.get("posts") else None
    bg_wav = None
    if job.get("recap"):                 # narration over the chosen scenes, no original audio
        src, utter, job["total"] = _recap_cut(job, voice_name)
    elif job.get("keep_bg", True) and BG_VOLUME > 0:
        if demucs_available():
            job.update(stage="music", pct=56)
            jlog(job, "removing original voices (demucs)…")
            bg_wav = separate_music(src, d)
        else:
            jlog(job, "demucs not installed — background music skipped "
                      "(pip install demucs)")
    if job.get("condense") or job.get("char_voice") or job.get("orig_audio"):   # long stretches without talk go
        src, utter = _condense(job, src, utter, voice_name)
    if (job.get("char_voice") or job.get("orig_audio")) and job.get("commentary") and not job.get("comments_cut"):
        _write_comments(job, os.environ.get("GEMINI_API_KEY"), utter, src)   # 🗣️ talk all the way
    if not job.get("recap") and not job.get("orig_audio"):
        job.update(stage="voice", pct=60)
        jlog(job, f"synthesizing {len(utter)} utterances ({voice_name})…")
    def tts_prog(i, n):
        if job.get("recap"):
            return
        job.update(pct=60 + int(30 * i / max(n, 1)))
        _sp(job, i / max(n, 1))
        if i % 5 == 0 or i == n:
            jlog(job, f"tts {i}/{n}")
    outro = _settings["outro"] if job.get("outro") else ""
    if outro and not job.get("recap") and _ends_with_channel_promo(utter, job["total"]):
        jlog(job, "video already ends with a channel shout-out — outro skipped")
        outro = ""
    if outro:
        jlog(job, f"adding outro: {outro}")
    intro = (job.get("intro_text") or "") if job.get("intro") else ""
    if intro:
        jlog(job, f"adding intro: {intro}")
    if job.get("orig_audio"):
        # 🔊 the film's own sound: no voice at all, captions on its lines; the
        # channel outro goes after each part (and after an unsplit video)
        job.update(stage="voice", pct=60)
        jlog(job, "keeping the original sound — Burmese captions only (no AI voice)")
        dub_wav = d / "orig_audio.mka"
        if job.get("no_music"):
            # 🎵 the film's background music taken out (voices kept): copyrighted songs
            # under the dialogue are what TikTok / YouTube flag
            jlog(job, "removing the background music (voices kept)…")
            try:
                def mprog(f):
                    _sp(job, f)
                    jlog(job, f"removing the music… {int(f * 100)}%")
                voc = separate_vocals(src, d, mprog)
                if voc:
                    run(["ffmpeg", "-y", "-v", "error", "-i", str(voc), "-c:a", "aac", "-b:a", "160k", str(d / "orig_voice.m4a")])
                    dub_wav = d / "orig_voice.m4a"
                    shutil.rmtree(d / "sep", ignore_errors=True)
                    (d / "orig_stereo.wav").unlink(missing_ok=True)
                    jlog(job, "background music removed")
                else:
                    jlog(job, "music removal not available (demucs missing) — original sound kept")
            except Exception as e:  # noqa: BLE001 - the original sound is still fine
                jlog(job, f"music removal failed ({str(e)[:150]}) — original sound kept")
        if dub_wav.name == "orig_audio.mka" and not dub_wav.exists():
            run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "copy", str(dub_wav)])
        cues = [(u["start"], u["end"], u["text"]) for u in utter if (u.get("text") or "").strip()]
        try:                                       # captions only while someone is talking
            wav = d / ("audio_cut.wav" if src.name == "condensed.mp4" else "audio.wav")   # (the cut film's sound)
            if not wav.exists():
                extract_audio(src, wav)
            cues = fit_cues_to_speech(cues, speech_regions(wav))
        except Exception as e:  # noqa: BLE001 - whisper's own times then
            jlog(job, f"caption timing check skipped: {str(e)[:120]}")
        lead = 0.0
        if job.get("comments"):               # 🎙️ the narrator over the quiet pauses (film sound turned down)
            clips = []
            from concurrent.futures import ThreadPoolExecutor
            jlog(job, f"🎙️ voicing {len(job['comments'])} narrator remarks…")
            done_n = [0]

            def _voice(kc):                        # several at once (one by one: ~4 s each, 13 min for 200
                k, c = kc                          # with nothing in the log — it looked stuck)
                p = d / f"cm{k}.mp3"
                try:
                    if not (p.exists() and p.stat().st_size > 0):
                        synthesize(c["text"], NARRATOR_VOICE, p)
                except Exception as e:  # noqa: BLE001 - checked below
                    jlog(job, f"remark {k + 1} skipped: {str(e)[:100]}")
                done_n[0] += 1
                if done_n[0] % 25 == 0:
                    jlog(job, f"🎙️ narrator remarks {done_n[0]}/{len(job['comments'])}")
                    _sp(job, done_n[0] / len(job["comments"]))

            with ThreadPoolExecutor(int(os.environ.get("DUBLAB_TTS_WORKERS") or 6)) as ex:
                list(ex.map(_voice, enumerate(job["comments"])))
            for k, c in enumerate(job["comments"]):
                p = d / f"cm{k}.mp3"
                try:
                    if not (p.exists() and p.stat().st_size > 0):
                        continue
                    if probe_duration(p) <= (c["end"] - c["start"]) * 1.2:
                        clips.append((p, c["start"]))
                        cues.append((c["start"], c["start"] + probe_duration(p), c["text"]))
                except Exception as e:  # noqa: BLE001
                    jlog(job, f"remark {k + 1} skipped: {str(e)[:100]}")
            if clips:
                dub_wav = mix_comments(dub_wav, clips, d / "orig_mix.m4a")
                cues.sort()
                jlog(job, f"🎙️ {len(clips)} narrator remarks mixed in")
        if outro:
            synthesize(outro, voice_name, d / "outro.mp3")
        _sp(job, 1)
    else:
        lines = utter
        if not job.get("recap"):
            if job.get("voice_auto") and not job.get("char_voice") and not any(u.get("voice") for u in utter):
                try:                              # 👥 a woman's line in the female voice, a man's in the male
                    wav = d / "audio.wav"
                    if not wav.exists():
                        extract_audio(src, wav)
                    gs = line_genders(wav, utter)
                    for u, g in zip(utter, gs):
                        if g:
                            u["voice"] = VOICES[job["direction"]][g]
                    nm = sum(1 for g in gs if g == "male")
                    nf = sum(1 for g in gs if g == "female")
                    jlog(job, f"👥 voices by speaker: {nm} lines male, {nf} female, {len(gs) - nm - nf} main voice")
                except Exception as e:  # noqa: BLE001 - one voice then
                    jlog(job, f"per-speaker voices skipped: {str(e)[:120]}")
            if job.get("comments"):               # 🎙️ narrator remarks in the pauses, a third voice
                nv = voice_name if job.get("char_voice") and not job.get("scene_recap") else NARRATOR_VOICE
                lines = sorted(utter + [{**c, "voice": nv, "comment": True} for c in job["comments"]],
                               key=lambda u: u["start"])
                jlog(job, f"🎙️ {len(job['comments'])} narrator remarks added")
        dub_wav, cues, lead = build_dub_track(lines, voice_name, d, job["total"], tts_prog,
                                              outro or None, intro or None,
                                              # recap: an even pace — a line squeezed up to 1.65x, then a pause,
                                              # sounded "fast, stop, fast"; it may run ≤ ~½ s late instead
                                              max_extra=1.12 if job.get("char_voice") else None)
    total = job["total"] + lead                 # the held first frame makes it longer
    write_srt(cues, d / "captions.srt")          # always offered as a download

    job.update(stage="mux", pct=93)
    jlog(job, "muxing dubbed audio with video…")
    logo = None
    if job.get("logo") and LOGO_FILE.exists():
        logo = {"path": LOGO_FILE, "position": _settings["logo_position"],
                "size": _settings["logo_size"] / 100}
        try:                                  # film between still bars: logo goes on the film
            area = picture_area(src, job["total"] * 0.1, job["total"] * 0.9)
            if area != (0.0, 1.0):
                logo["area"] = area
                jlog(job, f"picture sits at {area[0] * 100:.0f}–{area[1] * 100:.0f}% of the height "
                          "(bars around it) — logo goes on the picture")
        except Exception as e:  # noqa: BLE001 - corner of the whole frame then
            jlog(job, f"picture area check skipped: {str(e)[:100]}")
        jlog(job, f"adding logo ({_settings['logo_position']}, {_settings['logo_size']}%)")
    captions = blur = None
    try:
        vw, vh = video_size(src)
    except Exception:  # noqa: BLE001
        vw, vh = 1920, 1080
    sfx = "_v" if vh > vw else ""        # portrait video → its own positions
    if job.get("blur"):
        blur = _find_blur(job, src, sfx)
        if blur:
            when = _caption_times(job)
            if when:
                blur = {**blur, "when": when}
                jlog(job, f"blur only while the film's own captions show ({len(when)} stretches)")
    hide = _find_logos(job, src)
    censor = _find_sensitive(job, src) if job.get("censor") else []
    job["censored"] = len(censor)
    job["logos_hidden"] = len(hide)
    # TikTok (9:16) is the default for a landscape film; ticking "YouTube" also
    # (or, for a portrait source, only) keeps the normal 16:9 video
    landscape = vw > vh
    yt = job.get("youtube") and not job.get("yt_full")    # (yt_full: the YouTube one is a separate job)
    tt_only = landscape and not yt and not job.get("yt_only")   # just the 9:16 TikTok video
    want_tt = landscape and bool(yt) and not job.get("yt_only")  # 16:9 YouTube video + a 9:16 TikTok one
    main_frame = frame_layout(vw, vh) if tt_only else None
    logo_png = LOGO_FILE if job.get("logo") and LOGO_FILE.exists() else None

    def _caps(w, h, sub):
        if not job.get("captions"):
            return None
        try:
            lst, strip_h = render_captions(cues, w, h, d / sub, _settings["caption_size"],
                                           box=not blur)    # on the blur: no dark box
            return {"list": lst, "height": strip_h, "bottom": _settings["caption_bottom" + sfx] / 100}
        except Exception as e:  # noqa: BLE001 - never lose the dub over captions
            jlog(job, f"captions skipped: {e}")
            return None
    captions = _caps(*((main_frame["cap_w"], main_frame["cap_h"]) if main_frame else (vw, vh)), "caps")
    if captions:
        jlog(job, f"burning {len(cues)} Burmese captions")
    # the outro (or the last line) can run past the picture: hold the last
    # frame until the voice is done, or players stop before it is heard
    tail = 0.0
    try:
        over = probe_duration(dub_wav) - total
        if over > 0.05:
            tail = over + 0.4
            total += tail
            jlog(job, f"holding the last frame {tail:.1f}s for the end of the voice")
    except Exception:  # noqa: BLE001
        pass
    cuts = split_points(cues, total, job.get("split") or 0)
    nparts = len(cuts) + 1 if cuts else 1
    if cuts:
        jlog(job, ("TikTok version: " if want_tt else "") + f"splitting into {nparts} parts of ~{job['split']} min "
                  f"(cuts at {', '.join(f'{c / 60:.1f}' for c in cuts)} min)")
    if job.get("cover") and (job.get("title_mm") or job.get("no_title")):
        # same photos for every part; a split film gets "title - 1", "title - 2"…
        # made before the mux so part 1's cover is also the picture under the intro
        try:
            key = os.environ.get("GEMINI_API_KEY")
            cast = job.get("cast_mm") or ""
            if key and not cast and job.get("utter"):   # 👤 who the leads are (same call as the translation's: cached)
                try:
                    cast = write_cast(list(enumerate(u.get("orig") or u["text"] for u in job["utter"])), key,
                                      LANG_NAMES.get(job.get("seg_lang") or "", job.get("seg_lang")), title=job.get("name") or "")
                except Exception:  # noqa: BLE001 - pictures alone then
                    cast = ""
            chooser = (lambda shots: pick_cover_frames(shots, key, cast)) if key else None
            art = None
            if key and job.get("cover_scene"):
                try:
                    jlog(job, "drawing the cover picture with AI…")
                    refs = sample_frames(src, job["total"] * 0.1, job["total"] * 0.85, 4, width=768)
                    art = draw_cover(job["cover_scene"], refs, key, portrait=bool(sfx))
                    (d / "cover_art.jpg").write_bytes(art)
                except Exception as e:  # noqa: BLE001 - fall back to the photo collage
                    art = None
                    jlog(job, f"AI cover picture failed ({str(e)[:150]}) — using a photo collage")
            thumb = d / "src_thumb.jpg"
            series = _series_thumb(job["title_mm"]) if job.get("episode") and job.get("title_mm") else None
            use_series = bool(series and series.exists() and job.get("cover_src") != "frames")
            if thumb.exists() and not use_series and not _use_thumb(job, thumb, src, key):
                thumb = d / "no_thumb.jpg"          # → covers from the video's own frames
            # 📚 a series: every episode uses part 1's picture (only the number changes) —
            # the first episode made under a title keeps its picture for the next ones
            if series:
                if use_series:
                    thumb = series
                    jlog(job, "series cover: the same picture as the earlier episodes")
                elif thumb.exists():
                    series.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(thumb, series)
                    jlog(job, "series cover: this picture is kept for the next episodes")
            if thumb.exists() and not job.get("yt_child_frames"):
                # 🖼️ from the video's own thumbnail (the look the uploader chose): its text
                # blurred away, our title, part number and logo on it
                from dub import thumb_cover, part_suffix
                ep = job.get("episode")
                n_yt = 1 if want_tt else nparts
                job["covers"] = [thumb_cover(thumb, job["title_mm"], d / f"cover{i}.jpg", bool(sfx or tt_only), logo_png,
                                             part_suffix(i, n_yt, ep)).name for i in range(1, n_yt + 1)]
                if want_tt:
                    (d / "tt").mkdir(exist_ok=True)
                    job["covers_tt"] = ["tt/" + thumb_cover(thumb, job["title_mm"], d / "tt" / f"cover{i}.jpg", True, logo_png,
                                                            part_suffix(i, nparts, ep)).name for i in range(1, nparts + 1)]
                jlog(job, f"covers made from the video's own thumbnail ({len(job['covers']) + len(job.get('covers_tt') or [])})")
                _check_covers(job)
                raise _CoversDone()
            picks = {}                    # one photo choice for both the YouTube and the TikTok covers
            covers = make_covers(src, job["title_mm"], d, job["total"] * 0.04, job["total"] * 0.9,
                                 portrait=bool(sfx or tt_only), blur=blur, hide=hide, logo=logo_png,
                                 parts=1 if want_tt else nparts, episode=job.get("episode"),   # the YouTube video stays whole
                                 chooser=chooser, log=lambda m: jlog(job, m), art=art, picks=picks, single=True)
            job["covers"] = [c.name for c in covers]
            if want_tt:                  # 9:16 covers for the TikTok version
                (d / "tt").mkdir(exist_ok=True)
                tt = make_covers(src, job["title_mm"], d / "tt", job["total"] * 0.04, job["total"] * 0.9,
                                 portrait=True, blur=blur, hide=hide, logo=logo_png, parts=nparts, episode=job.get("episode"),
                                 chooser=chooser, log=lambda m: jlog(job, m), picks=picks, single=True)
                job["covers_tt"] = ["tt/" + c.name for c in tt]
            jlog(job, f"made {len(job['covers']) + len(job.get('covers_tt') or [])} cover image(s)")
            _check_covers(job)
        except _CoversDone:            # made from the thumbnail
            pass
        except Exception as e:  # noqa: BLE001 - never lose the dub over a cover
            jlog(job, f"cover skipped: {e}")
    intro_img = d / job["covers"][0] if lead and job.get("covers") else None

    tt_cuts = list(cuts)                    # TikTok's cut points (the YouTube video stays whole)

    def _render(frame, name, caps, cuts):
        """mux, then split into parts and give each part the outro; returns file names."""
        out = d / f"{name}.mp4"
        _sp(job, 0)
        mux(src, dub_wav, out, bg_wav=bg_wav, bg_volume=BG_VOLUME, logo=logo,
            captions=caps, blur=blur, hide=hide, keyframes=cuts or None, lead=lead, tail=tail,
            intro_img=intro_img, frame=frame, censor=censor or None,
            progress=lambda f: _sp(job, f))
        orig_outro = job.get("orig_audio") and outro and (d / "outro.mp3").exists()
        if not cuts:
            if orig_outro:                    # 🔊 mode: the dub track had no outro in it
                _outro_on_parts(job, [out], d, outro, caps, blur, vw, vh, frame)
            # the whole (YouTube) video opens with the SAME teaser as TikTok part 1: picked inside part 1
            _hooks_on(job, d, [out.name], [[(c[0], c[1], c[2]) for c in cues]],
                      window=(0.0, tt_cuts[0] if tt_cuts else 10.0 ** 9))
            return [out.name]
        job.update(stage="split", pct=97)
        jlog(job, "cutting the parts…")
        _sp(job, 0)
        parts = split_video(out, cuts, d, stem=f"{name}_part")
        out.unlink(missing_ok=True)                       # the parts replace it
        _sp(job, 0.3 if outro and (d / "outro.mp3").exists() else 1)
        if outro and (d / "outro.mp3").exists():
            # 🔊 mode: the last part needs it too (the dub track had no outro)
            _outro_on_parts(job, parts if job.get("orig_audio") else parts[:-1], d, outro, caps, blur, vw, vh, frame)
        bounds = [0.0, *cuts, 10 ** 9]                    # each part's own caption lines, on its own clock
        pcs = [[(c[0] - a, c[1] - a, c[2]) for c in cues if a <= c[0] < b] for a, b in zip(bounds, bounds[1:])]
        _hooks_on(job, d, [p.name for p in parts], pcs)
        return [p.name for p in parts]

    tag = "MOVIE  |  မြန်မာစာတန်း" if job.get("orig_audio") else None
    if main_frame:
        jlog(job, "making the TikTok (9:16) video")
        try:
            bounds = [0.0, *cuts, total]
            main_frame["list"] = render_frames(job.get("title_mm") or "", list(zip(bounds, bounds[1:])),
                                               d / "frames", main_frame, logo_png, tag=tag, episode=job.get("episode"))
        except Exception as e:  # noqa: BLE001 - fall back to the normal 16:9 video
            jlog(job, f"TikTok frame failed ({str(e)[:150]}) — making a normal video")
            main_frame = None
            captions = _caps(vw, vh, "caps")
    # YouTube + TikTok: the YouTube video stays whole (watch hours); only TikTok is split
    main_cuts = [] if want_tt else cuts
    names = _render(main_frame, "dubbed", captions, main_cuts)
    shutil.rmtree(d / "frames", ignore_errors=True)
    if main_cuts:
        job["parts"] = names
        job["part_times"] = list(zip([0.0, *cuts], [*cuts, total]))
    if want_tt:
        jlog(job, "YouTube video done — making the TikTok (9:16) version too…")
        job.update(stage="mux", pct=93)
        try:
            frame = frame_layout(vw, vh)
            bounds = [0.0, *cuts, total]
            frame["list"] = render_frames(job.get("title_mm") or "", list(zip(bounds, bounds[1:])),
                                          d / "frames", frame, logo_png, tag=tag, episode=job.get("episode"))
            job["tt"] = _render(frame, "tiktok", _caps(frame["cap_w"], frame["cap_h"], "caps_tt"), cuts)
            job["part_times"] = list(zip([0.0, *cuts], [*cuts, total]))
        except Exception as e:  # noqa: BLE001 - the normal version is still there
            jlog(job, f"TikTok version failed: {str(e)[:200]}")
        shutil.rmtree(d / "frames", ignore_errors=True)
        shutil.rmtree(d / "caps_tt", ignore_errors=True)
    shutil.rmtree(d / "sep", ignore_errors=True)
    cleanup_segments(d)
    (d / "dub.wav").unlink(missing_ok=True)
    (d / "audio.wav").unlink(missing_ok=True)
    (d / ("input" + job["ext"])).unlink(missing_ok=True)   # the source: covers/parts are made, not needed now
    for tmp in ("orig_stereo.wav",):
        (d / tmp).unlink(missing_ok=True)

    (d / "recap.mp4").unlink(missing_ok=True)          # recap intermediate
    (d / "condensed.mp4").unlink(missing_ok=True)      # YouTube-full intermediate
    (d / "orig_audio.mka").unlink(missing_ok=True)     # 🔊 mode's audio copy
    (d / "orig_voice.m4a").unlink(missing_ok=True)     # 🔊 mode's voices-only audio
    for f in d.glob("r*.mp3"):
        f.unlink(missing_ok=True)
    if posts:
        posts.join(60)               # long done by now (written while the video rendered)
    job["risk"] = _risk_report(job)
    if job.get("telegram"):          # mark before "done" so the page never offers a 2nd send
        job["tg"] = {"state": "sending", "error": "", "pct": 0}
    job.update(status="done", stage="done", pct=100)
    _save_meta(job)
    jlog(job, "done ✅")
    _tk_enqueue(job)
    if job.get("telegram"):
        threading.Thread(target=_telegram_send, args=(job,), daemon=True).start()
    elif _should_preupload(job):
        threading.Thread(target=_telegram_prepare, args=(job,), daemon=True).start()


def _stay_awake():
    """macOS: keep the machine from sleeping while a job runs (a sleeping
    laptop pauses the job and its network calls time out). Returns the
    caffeinate process to stop afterwards, or None elsewhere."""
    if not shutil.which("caffeinate"):
        return None
    try:
        # -i: no idle sleep; -s: no system sleep on AC power; -d: keep the
        # screen on too, so a running dub never looks like the Mac fell asleep
        return subprocess.Popen(["caffeinate", "-i", "-s", "-d"])
    except OSError:
        return None


_running = [None]          # the job the worker is on (Gemini's "waiting for VPN" goes to its log)


def _gemini_wait(msg):
    j = jobs.get(_running[0] or "")
    if j:
        jlog(j, msg)


import gemini as _gemini_mod  # noqa: E402
_gemini_mod.on_wait = _gemini_wait


def worker():
    while True:
        with lock:
            while not queue:
                lock.wait()
            jid = queue.pop(0)
        _running[0] = jid
        jobs[jid]["status"] = "processing"
        awake = _stay_awake()
        try:
            process_job(jid)
        finally:
            if awake:
                awake.terminate()
        sweep_old()


threading.Thread(target=worker, daemon=True, name="worker").start()


def _busy():
    """A job is queued / running, or videos are still going to Telegram."""
    for j in list(jobs.values()):
        if j.get("status") in ("queued", "processing"):
            return True
        if (j.get("tg") or {}).get("state") == "sending" or (j.get("tg_pre") or {}).get("state") == "uploading":
            return True
    return False


def _awake_while_busy():
    """☕ macOS: while there is work (a job, a Telegram upload) the Mac must not
    fall asleep — sleep stopped uploads half-way. `caffeinate -i` keeps it awake
    (the screen may still turn off) and is stopped as soon as the work is done;
    -w ties it to this server, so it never outlives it. Closing a MacBook's lid
    still sleeps it."""
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return
    proc = None
    while True:
        try:
            busy = _busy()
            if busy and (proc is None or proc.poll() is not None):
                proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif not busy and proc is not None and proc.poll() is None:
                proc.terminate()
                proc = None
        except Exception:  # noqa: BLE001 - never let this take the server down
            pass
        time.sleep(15)


threading.Thread(target=_awake_while_busy, daemon=True, name="awake").start()


@app.get("/")
def index():
    return render_template("index.html", max_minutes=0, max_mb=max_mb(),
                           default_model=default_whisper_model(), outro=_settings["outro"],
                           has_logo=LOGO_FILE.exists(),
                           maintenance=MAINTENANCE,
                           maintenance_msg=MAINTENANCE_MSG)


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=f"ဖိုင်က {max_mb()} MB ထက် ကြီးနေတယ်"), 413


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/jobs")
def create_job():
    if MAINTENANCE:
        return jsonify(error=MAINTENANCE_MSG), 503
    if len(queue) >= MAX_QUEUE:
        return jsonify(error="လူများနေတယ် — ခဏနေမှ ပြန်စမ်းပါ"), 429
    if not _rate_ok(_client_ip()):
        return jsonify(error="ခဏခဏ တင်နေတယ် — ၁ နာရီနေမှ ပြန်စမ်းပါ"), 429
    f = request.files.get("video")
    url = (request.form.get("url") or "").strip()
    direction = request.form.get("direction", "auto2mm")
    voice = request.form.get("voice", "auto")
    review = request.form.get("review") == "1"
    keep_bg = request.form.get("keep_bg", "0") == "1"
    outro = request.form.get("outro", "1") == "1"
    intro = request.form.get("intro", "0") == "1"      # off unless ticked
    commentary = request.form.get("commentary", "1") == "1"   # 🎙️ narrator remarks (no-recap videos)
    char_voice = request.form.get("fullstyle") == "char"       # 🗣️ characters' lines, short, one AI voice
    cover_src = "frames"       # 🖼️ covers always from the video: the main character (+ one more photo)
    hook = request.form.get("hook", "0") == "1"        # ⚡ a teaser scene from inside the video first (off unless ticked)
    youtube = request.form.get("youtube", "0") == "1"  # TikTok 9:16 always; this adds the 16:9 YouTube one
    yt_full = False            # 📺 YouTube = the recap as one video (the 83%+ long version was dropped)
    my_title = re.sub(r"\s+", " ", request.form.get("title") or "").strip()[:60]   # typed by the user
    episode = re.sub(r"\D", "", request.form.get("episode") or "")[:3] or None     # 📚 a series' episode number
    no_title = request.form.get("show_title", "1") != "1"   # unticked: covers / frame without a title
    cover = True                                          # every video gets a cover
    logo = request.form.get("logo", "1") == "1"
    send_tg = request.form.get("telegram") == "1" and _admin_ok() and telegram.configured()
    captions = request.form.get("captions", "1") == "1"
    try:
        split = int(request.form.get("split") or 0)
    except ValueError:
        split = 0
    split = split if split in SPLIT_CHOICES else 0
    blur = request.form.get("blur", "0") == "1"
    censor = request.form.get("censor", "0") == "1"      # 🫣 hide blood / nudity (only when ticked)
    want_posts = request.form.get("posts") == "1"        # 📝 upload texts to Telegram (off unless ticked)
    orig_audio = request.form.get("orig_audio") == "1"
    no_music = orig_audio and request.form.get("no_music") == "1"   # 🎵 take the background music out   # 🔊 keep the film's own sound, Burmese captions only
    model = request.form.get("model") or default_whisper_model()
    if model not in WHISPER_MODELS:
        model = default_whisper_model()
    if direction not in VOICES or voice not in ("auto", "female", "male"):
        return jsonify(error="ရွေးချယ်မှု မှားနေတယ်"), 400
    recap_auto = request.form.get("recap") == "auto"      # 🤖 55%+ of the film's own length
    try:
        recap = 20 if recap_auto else int(request.form.get("recap") or 0)   # (set from the length later)
    except ValueError:
        recap = 0
    recap = recap if recap in RECAP_CHOICES else 0
    if orig_audio:
        recap = 0                        # nothing is narrated: the film plays as it is
    # 🎬 recap = the film's own scenes (~80%: opening, ending, dances and silent stretches cut), the
    # characters' lines dubbed short in one AI voice, a narrator in the scenes without talk —
    # not a narrator retelling the whole film
    scene_recap = bool(recap) and request.form.get("recap_style", "scenes") == "scenes"
    scene_min = recap
    if scene_recap:
        char_voice, recap = True, 0
    # no length limit: any video is accepted (long films are split into parts)
    max_sec = float("inf")
    too_long = "ဗီဒီယို ရှည်လွန်းတယ်"
    tg_msg = None
    if request.form.get("tg_msg"):          # a video the owner forwarded to the bot
        if not (_admin_ok() and _settings.get("telegram_chat_id") and telegram.big_available()):
            return jsonify(error="Telegram ဗီဒီယို သုံးဖို့ admin password + bot ချိတ်ထားဖို့ လိုတယ်"), 403
        try:
            want = int(request.form["tg_msg"])
            item = next((v for v in telegram.recent_videos(_settings["telegram_chat_id"], 100)
                         if v["id"] == want), None)
        except (ValueError, telegram.TelegramError):
            item = None
        if not item:
            return jsonify(error="ဒီ Telegram ဗီဒီယို မတွေ့တော့ဘူး — bot ဆီ ပြန် forward လုပ်ပါ"), 400
        if item["duration"] > max_sec:
            return jsonify(error=too_long), 400
        tg_msg = want
        url, ext, name = "", ".mp4", item["name"]
        label = f"Telegram: {item['name']} ({item['size'] / 1048576:.0f} MB)"
    if tg_msg:
        pass
    elif url:
        if len(url) > 300 or not is_supported_url(url):
            return jsonify(error="YouTube / TikTok link ပဲ လက်ခံတယ်"), 400
        try:
            title, dur, live = probe_url(url)
        except YouTubeBlocked:
            return jsonify(error="YouTube က ဒီ ကွန်ပျူတာကို ခဏ ပိတ်ထားတယ် (ဒေါင်းတာ များလို့ bot စစ်နေတာ) — "
                                 "Chrome မှာ YouTube ကို sign in ဝင်ထားပါ၊ ဒါမှမဟုတ် ၃၀ မိနစ်–၁ နာရီနေမှ ပြန်စမ်းပါ "
                                 "(ဗီဒီယိုကို ဒေါင်းပြီး ဖိုင်နဲ့ တင်လည်း ရတယ်)"), 400
        except Exception:  # noqa: BLE001
            return jsonify(error="ဒီ link ကို ဖွင့်မရဘူး — link စစ်ပါ "
                                 "(private/အသက်ကန့်သတ် ဗီဒီယို မရဘူး)"), 400
        if live:
            return jsonify(error="Live stream မရဘူး"), 400
        if dur > max_sec:
            return jsonify(error=too_long), 400
        ext, label = ".mp4", f"{title} · {url}"
        name = title
    else:
        if not f or not f.filename:
            return jsonify(error="ဗီဒီယိုဖိုင် ရွေးပေးပါ"), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
            return jsonify(error="MP4/MOV ဖိုင်ပဲ လက်ခံတယ်"), 400
    jid = uuid.uuid4().hex[:12]
    d = JOBS / jid
    try:
        d.mkdir(parents=True)
        if not url and not tg_msg:
            src = d / ("input" + ext)
            f.save(src)
            # fail fast on bad files — nothing reaches the worker queue
            if probe_duration(src) > max_sec:
                raise ValueError(too_long)
            if not has_video_stream(src):
                raise ValueError("ဗီဒီယိုဖိုင် မဟုတ်ဘူး")
            label = f"{f.filename} ({src.stat().st_size / 1048576:.1f} MB)"
            name = os.path.splitext(os.path.basename(f.filename))[0]
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(d, ignore_errors=True)
        if isinstance(e, ValueError):
            return jsonify(error=clean_err(e)), 400
        return jsonify(error="ဖိုင်စစ်ရင်း အမှားရှိတယ်"), 500
    jobs[jid] = {"status": "queued", "stage": "queue", "pct": 0, "error": "",
                 "dir": d, "ext": ext, "direction": direction, "url": url,
                 "voice": voice, "review": review, "keep_bg": keep_bg, "phase": 1,
                 "model": model, "outro": outro, "intro": intro, "hook": hook or bool(recap) or char_voice, "cover_src": cover_src, "commentary": (char_voice or orig_audio) and not recap,   # 🎙️ our own narration in the quiet scenes
                 "char_voice": char_voice and not recap and not orig_audio,
                 "scene_recap": scene_recap, "scene_min": scene_min, "scene_auto": scene_recap and recap_auto,
                 **({"kind": "game", "recap": 0, "split": 0, "game_music": request.form.get("game_music", "1") == "1",
                     "game_cut": request.form.get("game_cut", "1") == "1",
                     "game_slow": request.form.get("game_slow", "1") == "1",
                     "game_meme": request.form.get("game_meme", "0") == "1",
                     "game_hero": re.sub(r"[^\w .'-]", "", request.form.get("game_hero") or "")[:30]}
                    if request.form.get("game") == "1" else {}), "cover": cover, "logo": logo, "name": name,
                 "recap": recap,
                 "tg_src": tg_msg,
                 "id": jid, "telegram": send_tg, "owner": _admin_ok(),
                 "captions": captions, "blur": blur, "split": split, "youtube": youtube, "my_title": my_title, "no_title": no_title,
                 "yt_full": yt_full and not orig_audio, "orig_audio": orig_audio, "want_posts": want_posts and send_tg, "censor": censor, "episode": episode,
                 "recap_auto": recap_auto and not orig_audio, "no_music": no_music,
                 "created": time.time()}
    jlog(jobs[jid], "uploaded " + re.sub(r"[\r\n]", " ", label)[:160])
    with lock:
        queue.append(jid)
        lock.notify()
    return jsonify(job_id=jid)


@app.get("/api/jobs/<jid>")
def job_status(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify(error="job မတွေ့ဘူး"), 404
    # per-stage progress for the stages that report it (overall pct ranges);
    # None = this stage has no measurable progress (UI shows a spinner)
    lo_hi = STAGE_RANGE.get(job["stage"])
    stage_pct = None
    if lo_hi:
        lo, hi = lo_hi
        stage_pct = max(0, min(100, round((job["pct"] - lo) * 100 / (hi - lo))))
    if job["stage"] == "download" and job.get("dl_pct") is not None:
        stage_pct = job["dl_pct"]          # the real download %, not the coarse overall one
    if job.get("spct") is not None and job.get("spct_stage") == job["stage"]:
        stage_pct = job["spct"]            # the stage's own progress (_sp)
    parts = []
    if job["status"] == "done" and (job.get("parts") or job.get("tt")):
        outs = _outputs(job)
        times = job.get("part_times") or [(0, job.get("total") or 0)]
        for i, (_, name) in enumerate(outs, 1):
            which, k = _out_kind(job, i)
            a, b = times[min(k, len(times)) - 1]
            if which == "covers" and not job.get("parts"):     # the whole (YouTube) video
                a, b = 0, job.get("total") or b
            parts.append({"n": i, "k": k, "tt": which == "covers_tt", "name": name,
                          "full": which == "covers" and not job.get("parts"),
                          "start": round(a), "end": round(b)})
    ahead = 0
    if job["status"] == "queued":       # jobs before this one: the running one + earlier in the queue
        with lock:
            q = list(queue)
        ahead = (q.index(jid) if jid in q else 0) + sum(1 for j in list(jobs.values()) if j.get("status") == "processing")
    return jsonify(status=job["status"], stage=job["stage"], pct=job["pct"],
                   stage_pct=stage_pct, parts=parts, ahead=ahead,
                   covers=len(job.get("covers") or []) + len(job.get("covers_tt") or [])
                   if job["status"] == "done" else 0,
                   covers_main=len(job.get("covers") or []),
                   cover_pick=job.get("cover_pick", 1), title=job.get("title_mm", ""),
                   name=job.get("name", ""), created=int(job.get("created", 0)),
                   gone=bool(job.get("files_gone")),
                   label=STAGE_LABEL.get(job["stage"], job["stage"]),
                   error=job["error"], resumable=_resumable(job), orig=bool(job.get("orig_audio")), kind=job.get("kind") or "",
                   fix_note=job.get("fix_note") or "", fix_changed=bool(job.get("fix_changed", True)),
                   fix_appeal=job.get("fix_appeal") or "", risk=job.get("risk") or [],
                   posts=_posts_view(job) if job["status"] == "done" else None,
                   yt_child=_child_info(job))


def _child_info(job):
    """The 📺 YouTube long version runs as its own job — the result page links to it."""
    c = jobs.get(job.get("yt_child_id") or "") or next(
        (j for j in list(jobs.values()) if j.get("yt_parent") == job["id"]), None)
    return {"id": c["id"], "status": c["status"], "pct": c["pct"]} if c else None


def _resumable(job):
    return (job.get("status") == "failed" and job["dir"].exists()
            and time.time() - job.get("failed_at", 0) < RESUME_KEEP
            and (job["dir"] / ("input" + job.get("ext", ".mp4"))).exists())


@app.post("/api/jobs/<jid>/cancel")
def cancel_job(jid):
    """⏹ stop: a queued job is dropped at once; a running one ends at its next step."""
    job = jobs.get(jid)
    if not job:
        return jsonify(ok=True)
    with lock:
        if jid in queue:
            queue.remove(jid)
            job.update(status="failed", error="ရပ်လိုက်ပါပြီ", stage="cancelled")
            shutil.rmtree(job["dir"], ignore_errors=True)
            jobs.pop(jid, None)
            return jsonify(ok=True)
    if job["status"] in ("processing", "queued"):
        job["cancel"] = True
    elif job["status"] in ("failed", "review"):
        shutil.rmtree(job["dir"], ignore_errors=True)
        jobs.pop(jid, None)
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/resume")
def resume_job(jid):
    """▶️ go on from where a failed job stopped: done steps are skipped
    (download, whisper, translation/recap, lines already voiced)."""
    job = jobs.get(jid)
    if not job or not _resumable(job):
        return jsonify(error="ဆက်လုပ်လို့ မရတော့ဘူး — 🔁 အစကနေ ပြန်စမ်းပါ"), 404
    job.update(status="queued", stage="queue", error="", pct=job.get("pct", 0) if job.get("phase1_done") else 0,
               phase=2 if job.get("phase1_done") else 1)
    job.pop("failed_at", None)
    _save_failed(job)            # snapshot: a server restart brings it back as resumable
    jlog(job, "▶️ resuming from where it stopped")
    with lock:
        queue.append(jid)
        lock.notify()
    return jsonify(ok=True)


@app.get("/api/jobs/<jid>/lines")
def job_lines(jid):
    """Translated lines, so the user can fix them before voicing."""
    job = jobs.get(jid)
    if not job or job["status"] != "review":
        return jsonify(error="ပြင်လို့ရတဲ့ အခြေအနေ မဟုတ်ဘူး"), 404
    return jsonify(lines=[{"start": round(u["start"], 2), "end": round(u["end"], 2),
                           "orig": u.get("orig", ""), "text": u["text"]}
                          for u in job["utter"]],
                   intro=job.get("intro_text") if job.get("intro") else None,
                   title=job.get("title_mm") if job.get("cover") else None)


@app.post("/api/jobs/<jid>/lines")
def submit_lines(jid):
    """Save the edited lines and queue the voice + mux phase."""
    job = jobs.get(jid)
    if not job or job["status"] != "review":
        return jsonify(error="ပြင်လို့ရတဲ့ အခြေအနေ မဟုတ်ဘူး"), 404
    data = request.get_json(force=True, silent=True) or {}
    texts = data.get("texts")
    if (not isinstance(texts, list) or len(texts) != len(job["utter"])
            or not all(isinstance(t, str) for t in texts)):
        return jsonify(error="စာကြောင်း အရေအတွက် မကိုက်ဘူး"), 400
    kept = []
    for u, t in zip(job["utter"], texts):
        t = re.sub(r"\s+", " ", t).strip()[:MAX_LINE]
        if t:  # an emptied line means "skip this line"
            kept.append({**u, "text": t})
    if not kept:
        return jsonify(error="စာကြောင်း အနည်းဆုံး တစ်ကြောင်း လိုတယ်"), 400
    with lock:
        if job["status"] != "review":  # double-click / second tab
            return jsonify(error="တင်ပြီးသားပါ"), 409
        job["utter"] = kept
        if job.get("intro") and isinstance(data.get("intro"), str):
            job["intro_text"] = re.sub(r"\s+", " ", data["intro"]).strip()[:300]   # "" = no intro
        if job.get("cover") and isinstance(data.get("title"), str):
            job["title_mm"] = re.sub(r"\s+", " ", data["title"]).strip()[:60]
        jlog(job, f"translation checked · {len(kept)} lines → voicing")
        job.update(status="queued", stage="queue", phase=2)
        queue.append(jid)
        lock.notify()
    return jsonify(ok=True)


@app.get("/api/jobs/<jid>/log")
def job_log(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify(error="job မတွေ့ဘူး"), 404
    p = job["dir"] / "job.log"
    return jsonify(log=p.read_text() if p.exists() else "")


def _mail_review(rec):
    """E-mail a new review to MAIL_TO (runs in a background thread)."""
    if not (MAIL_TO and SMTP_USER and SMTP_PASSWORD):
        return
    import smtplib
    from email.message import EmailMessage
    stars = "⭐" * rec["stars"] if rec["stars"] else "no rating"
    m = EmailMessage()
    m["Subject"] = f"DubLab feedback · {stars}"
    m["From"] = SMTP_USER
    m["To"] = MAIL_TO
    m.set_content(
        f"Stars: {stars}\n"
        f"Message: {rec['message'] or '—'}\n\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rec['ts']))}\n"
        f"IP: {rec['ip']}\n"
        f"Job: {rec['job_id'] or '—'}\n")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(m)
    except Exception as e:  # noqa: BLE001 - the review is already saved
        print(f"[mail] review e-mail failed: {type(e).__name__}: {e}")


@app.post("/api/reviews")
def add_review():
    if not _rate_ok(_client_ip()):
        return jsonify(error="ခဏနေမှ ပြန်စမ်းပါ"), 429
    data = request.get_json(force=True, silent=True) or {}
    try:
        stars = int(data.get("stars", 0))
    except (TypeError, ValueError):
        stars = 0
    msg = re.sub(r"\s+", " ", str(data.get("message") or "")).strip()[:500]
    jid = re.sub(r"[^0-9a-f]", "", str(data.get("job_id") or ""))[:12]
    if not (1 <= stars <= 5) and not msg:
        return jsonify(error="star ဒါမှမဟုတ် စာတစ်ခုခု ပေးပါ"), 400
    rec = {"id": uuid.uuid4().hex[:8], "ts": time.time(), "ip": _client_ip(),
           "stars": stars if 1 <= stars <= 5 else 0,
           "message": msg, "job_id": jid}
    try:
        with open(REVIEWS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return jsonify(error="သိမ်းမရဘူး"), 500
    threading.Thread(target=_mail_review, args=(rec,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/settings")
def settings_login():
    """Password box for the ⚙️ link; it opens the admin page with the key."""
    return render_template("settings_login.html")


@app.get("/admin/reviews")
def admin_reviews():
    if not _admin_ok():
        return redirect("/settings")
    return render_template("admin.html")


def _read_reviews():
    rows = []
    if REVIEWS.exists():
        for line in REVIEWS.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows


def _admin_ok():
    import hmac
    given = request.args.get("key") or request.headers.get("X-Admin-Key") or ""
    return bool(ADMIN_KEY) and hmac.compare_digest(given, ADMIN_KEY)


@app.get("/admin/api/reviews")
def admin_api_reviews():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    rows = _read_reviews()
    dist = {str(i): 0 for i in range(1, 6)}
    total_stars = rated = 0
    for r in rows:
        s = r.get("stars", 0)
        if 1 <= s <= 5:
            dist[str(s)] += 1
            total_stars += s
            rated += 1
    return jsonify(reviews=rows[:300], stats={
        "total": len(rows), "rated": rated,
        "avg": round(total_stars / rated, 1) if rated else 0,
        "dist": dist})


@app.get("/admin/api/settings")
def admin_get_settings():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    du = shutil.disk_usage(JOBS)
    jobs_bytes = sum(f.stat().st_size for f in JOBS.rglob("*") if f.is_file())
    return jsonify(max_minutes=max_minutes(), max_minutes_limit=MAX_MINUTES_LIMIT,
                   max_mb=max_mb(), outro=_settings["outro"], outro_max=OUTRO_MAX,
                   channel_name=_settings["channel_name"],
                   has_logo=LOGO_FILE.exists(), logo_position=_settings["logo_position"],
                   logo_size=_settings["logo_size"], caption_size=_settings["caption_size"],
                   speak_speed=_settings["speak_speed"],
                   caption_bottom=_settings["caption_bottom"],
                   blur_bottom=_settings["blur_bottom"], blur_height=_settings["blur_height"],
                   caption_bottom_v=_settings["caption_bottom_v"],
                   blur_bottom_v=_settings["blur_bottom_v"], blur_height_v=_settings["blur_height_v"],
                   disk_free_gb=round(du.free / 1e9, 1), disk_total_gb=round(du.total / 1e9, 1),
                   jobs_gb=round(jobs_bytes / 1e9, 2))


@app.post("/admin/api/settings")
def admin_set_settings():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    data = request.get_json(force=True, silent=True) or {}
    try:
        mins = int(data.get("max_minutes"))
    except (TypeError, ValueError):
        return jsonify(error="ဂဏန်း ထည့်ပါ"), 400
    if not 1 <= mins <= MAX_MINUTES_LIMIT:
        return jsonify(error=f"1 မှ {MAX_MINUTES_LIMIT} မိနစ်အတွင်း ထည့်ပါ"), 400
    _settings["max_minutes"] = mins
    if "outro" in data:
        _settings["outro"] = re.sub(r"\s+", " ", str(data.get("outro") or "")).strip()[:OUTRO_MAX]
    if "channel_name" in data:
        _settings["channel_name"] = re.sub(r"\s+", " ", str(data.get("channel_name") or "")).strip()[:CHANNEL_MAX]
    if data.get("logo_position") in LOGO_POSITIONS:
        _settings["logo_position"] = data["logo_position"]
    if "logo_size" in data:
        try:
            _settings["logo_size"] = max(5, min(30, int(data["logo_size"])))
        except (TypeError, ValueError):
            pass
    for k, (lo, hi) in _RANGES.items():
        if k in data:
            try:
                _settings[k] = max(lo, min(hi, round(float(data[k]), 2 if k == "speak_speed" else 1)))
            except (TypeError, ValueError):
                pass
    set_speak_speed(_settings["speak_speed"])
    try:
        SETTINGS_FILE.write_text(json.dumps(_settings, ensure_ascii=False))
    except OSError:
        return jsonify(error="သိမ်းမရဘူး"), 500
    return jsonify(ok=True, **{k: v for k, v in _settings.items() if k != "telegram_token"})


@app.get("/admin/api/logo")
def admin_get_logo():
    if not _admin_ok() or not LOGO_FILE.exists():
        return jsonify(error="not found"), 404
    return send_file(LOGO_FILE, mimetype="image/png", max_age=0)


@app.post("/admin/api/logo")
def admin_upload_logo():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify(error="ပုံ ရွေးပါ"), 400
    if os.path.splitext(f.filename)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return jsonify(error="PNG/JPG ပုံပဲ လက်ခံတယ် (နောက်ခံ ကြည်လင်တဲ့ PNG အကောင်းဆုံး)"), 400
    BRAND_DIR.mkdir(exist_ok=True)
    tmp = BRAND_DIR / ("upload" + os.path.splitext(f.filename)[1].lower())
    try:
        f.save(tmp)
        if tmp.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("ပုံက 10 MB ထက် ကြီးနေတယ်")
        normalize_logo(tmp, BRAND_DIR / "logo.new.png")
        (BRAND_DIR / "logo.new.png").replace(LOGO_FILE)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception:  # noqa: BLE001 - not an image ffmpeg can read
        return jsonify(error="ပုံဖိုင် ဖွင့်မရဘူး"), 400
    finally:
        tmp.unlink(missing_ok=True)
    return jsonify(ok=True)


@app.delete("/admin/api/logo")
def admin_delete_logo():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    LOGO_FILE.unlink(missing_ok=True)
    return jsonify(ok=True)


@app.delete("/admin/api/reviews/<rid>")
def admin_del_review(rid):
    if not _admin_ok():
        return jsonify(error="not found"), 404
    rid = re.sub(r"[^0-9a-f]", "", rid)[:8]
    rows = _read_reviews()
    kept = [r for r in rows if r.get("id") != rid]
    if len(kept) == len(rows):
        return jsonify(error="not found"), 404
    try:
        with open(REVIEWS, "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        return jsonify(error="write failed"), 500
    return jsonify(ok=True)


# ---------------- legal pages ----------------


def _contact_email():
    return (os.environ.get("DUBLAB_CONTACT_EMAIL") or MAIL_TO or "").strip()


@app.get("/terms")
def terms():
    return render_template("legal.html", page="terms", email=_contact_email(),
                           app_name=os.environ.get("DUBLAB_APP_NAME") or "DubLab")


@app.get("/privacy")
def privacy():
    return render_template("legal.html", page="privacy", email=_contact_email(),
                           app_name=os.environ.get("DUBLAB_APP_NAME") or "DubLab")


# ---------------- Telegram: send finished dubs to the owner's bot ----------------
def _public_url():
    url = (os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")
    if not url and os.environ.get("NGROK_DOMAIN"):
        url = "https://" + os.environ["NGROK_DOMAIN"].strip()
    return url


@app.get("/api/telegram")
def telegram_state():
    if not telegram.configured():
        return jsonify(configured=False)
    if not _admin_ok():
        return jsonify(configured=True, authorized=False)
    try:
        bot = telegram.bot_info()
    except telegram.TelegramError as e:
        return jsonify(configured=True, authorized=True, connected=False, error=str(e))
    return jsonify(configured=True, authorized=True, bot=bot, token=telegram.masked(),
                   connected=bool(_settings.get("telegram_chat_id")),
                   chat=_settings.get("telegram_chat_name", ""))


def _save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(_settings, ensure_ascii=False))
    except OSError:
        pass


@app.post("/api/telegram/connect")
def telegram_connect():
    """Body {token}: check and save the bot token (empty = keep the saved one),
    then link the chat that last messaged the bot."""
    if not _admin_ok():
        return jsonify(error="not found"), 404
    data = request.get_json(force=True, silent=True) or {}
    new = str(data.get("token") or "").strip()
    if new:
        if not re.fullmatch(r"\d{5,}:[\w-]{20,}", new):
            return jsonify(error="Token ပုံစံ မမှန်ဘူး — @BotFather ပေးတဲ့ 123456:ABC… အပြည့် ထည့်ပါ"), 400
        try:
            bot = telegram.check_token(new)
        except telegram.TelegramError as e:
            return jsonify(error=f"Token မှားနေတယ် ({e})"), 400
        if new != telegram.token():
            _settings.pop("telegram_chat_id", None)        # new bot → link the chat again
            _settings.pop("telegram_chat_name", None)
        telegram.set_token(new)
        _settings["telegram_token"] = new
        _save_settings()
    elif not telegram.configured():
        return jsonify(error="Bot token ထည့်ပါ"), 400
    try:
        bot = telegram.bot_info()
        chat_id, name = telegram.find_chat()
    except telegram.TelegramError as e:
        if "no messages" in str(e):
            return jsonify(ok=True, need_start=True, bot=bot)
        return jsonify(error=str(e)), 400
    try:
        telegram.send_message(chat_id, "✅ DubLab connected — finished dubs will be sent here.")
    except telegram.TelegramError as e:
        return jsonify(error=str(e)), 400
    _settings.update(telegram_chat_id=chat_id, telegram_chat_name=name)
    _save_settings()
    return jsonify(ok=True, chat=name, bot=bot)


@app.get("/api/telegram/videos")
def telegram_videos():
    """Videos the owner forwarded to the bot, to dub one of them."""
    if not _admin_ok():
        return jsonify(error="not found"), 404
    chat = _settings.get("telegram_chat_id")
    if not chat:
        return jsonify(error="Settings မှာ Telegram bot အရင် ချိတ်ပါ"), 400
    if not telegram.big_available():
        return jsonify(error="TELEGRAM_API_ID / TELEGRAM_API_HASH (.env) နဲ့ telethon လိုတယ်"), 400
    try:
        vids = telegram.recent_videos(chat)
    except telegram.TelegramError as e:
        return jsonify(error=str(e)), 400
    bot = ""
    try:
        bot = telegram.bot_info().get("username", "")
    except telegram.TelegramError:
        pass
    return jsonify(videos=vids, bot=bot, max_minutes=0)


@app.post("/api/telegram/disconnect")
def telegram_disconnect():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    _settings.pop("telegram_chat_id", None)
    _settings.pop("telegram_chat_name", None)
    _settings["telegram_token"] = ""           # forget the bot too (overrides .env)
    telegram.set_token("")
    _save_settings()
    return jsonify(ok=True)


def _cover_n(job, part, which="covers"):
    """Which cover a part uses: its own ("title - N") or the film's one."""
    covers = job.get(which) or []
    return part if 1 <= part <= len(covers) and len(covers) > 1 else 1


def _cover_file(job, part=1, which="covers"):
    covers = job.get(which) or []
    if not covers:
        return None
    p = job["dir"] / covers[_cover_n(job, part, which) - 1]
    return p if p.exists() else None


def _cover_thumb(job, part=1, which="covers"):
    """The part's cover, shrunk to Telegram's thumbnail size (≤320 px JPEG)."""
    covers = job.get(which) or []
    if not covers:
        return None
    n = _cover_n(job, part, which)
    src = job["dir"] / covers[n - 1]
    out = job["dir"] / f"tg_cover{'_tt' if which == 'covers_tt' else ''}{n}.jpg"
    try:
        if not out.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf",
                            "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "4",
                            str(out)], check=True, timeout=30)
        return out
    except Exception:  # noqa: BLE001
        return None


def _video_thumb(video, out):
    """A frame from 10% in, 320 px wide, for Telegram's preview."""
    try:
        t = max(1.0, probe_duration(video) * 0.1)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.1f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(out)],
                       check=True, timeout=60)
        return out if out.exists() else None
    except Exception:  # noqa: BLE001 - a missing preview is fine
        return None


_tg_lock = threading.Lock()


def _outputs(job):
    """[(path, download name)] — one file, or the parts of a split video;
    then the TikTok (9:16) version's file(s), when one was made."""
    d = job["dir"]
    if job.get("out_name"):                       # 🔧 a fixed part keeps its own name
        return [(d / "dubbed.mp4", job["out_name"])]
    if job.get("parts"):
        outs = [(d / f, _download_name(job, ".mp4", i)) for i, f in enumerate(job["parts"], 1)]
    else:
        outs = [(d / "dubbed.mp4", _download_name(job, ".mp4"))]
    tt = job.get("tt") or []
    for i, f in enumerate(tt, 1):
        name = _download_name(job, ".mp4", i if len(tt) > 1 else None, tiktok=True)
        if not _ready_title(job):
            name = name.replace(" - mm", " - mm-tiktok", 1)
        outs.append((d / f, name))
    return outs


def _out_kind(job, idx):
    """Output idx (1-based) → ("covers" or "covers_tt", its part number)."""
    n_main = len(job.get("parts") or []) or 1
    return ("covers", idx) if idx <= n_main else ("covers_tt", idx - n_main)


def _should_preupload(job):
    """Owner's big video(s), Telegram set up: upload in the background right
    away so pressing "send" later is instant."""
    try:
        sizes = [p.stat().st_size for p, _ in _outputs(job)]
    except OSError:
        return False
    return bool(job.get("owner") and _settings.get("telegram_chat_id")
                and any(sz > telegram.MAX_UPLOAD for sz in sizes)
                and all(sz <= telegram.BIG_MAX for sz in sizes) and telegram.big_available())


def _start_posts(job):
    """📝 upload texts (TikTok / YouTube / Facebook) written in the background
    while the video renders — no extra waiting."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:                     # 📝 always written (shown on the result page to copy);
        return None                 # sent to Telegram only when "want_posts" is ticked
    def run():
        try:
            story = " ".join(u["text"] for u in job.get("utter") or [])
            job["posts"] = write_posts(job.get("title_mm") or "", job.get("name") or "", story, key,
                                       _settings.get("channel_name") or "Marco TV")
            if job.get("episode") and job["posts"].get("youtube_title"):     # 📚 "Title - 3"
                job["posts"]["youtube_title"] = job["posts"]["youtube_title"].rstrip() + f" - {job['episode']}"
            (job["dir"] / "posts.txt").write_text(_posts_text(job), encoding="utf-8")
            jlog(job, "📝 upload texts written (TikTok / YouTube / Facebook)")
        except Exception as e:  # noqa: BLE001 - the video matters, not the texts
            jlog(job, f"upload texts skipped: {str(e)[:150]}")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


_AI_WORDS = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])|人工智能|AI短剧|ai movie|ai drama", re.I)


def _posts_view(job):
    """📝 The upload texts for the result page (each with a copy button)."""
    p = job.get("posts") or {}
    if not p or job.get("kind") == "fix":
        return None
    out = []
    if p.get("tiktok") and not job.get("yt_only"):
        out.append({"label": "📱 TikTok / Reels caption", "text": p["tiktok"]})
    if p.get("youtube_title"):
        out.append({"label": "📺 YouTube title", "text": p["youtube_title"]})
    if p.get("youtube_desc"):
        out.append({"label": "📺 YouTube description", "text": p["youtube_desc"]})
    if p.get("facebook"):
        out.append({"label": "📘 Facebook post", "text": p["facebook"]})
    ai = bool(p.get("ai_film") or _AI_WORDS.search(job.get("name") or ""))
    return {"items": out, "ai": ai}


def _posts_text(job):
    """The upload texts as one Telegram message, only for the versions this job made."""
    p = job.get("posts") or {}
    if not p:
        return ""
    has_tt = not job.get("yt_only")                      # the YouTube long version has no TikTok video
    has_yt = bool(job.get("yt_only") or job.get("tt") or (job.get("youtube") and not job.get("yt_full")))
    out = [f"📝 {job.get('title_mm') or job.get('name') or ''}"]
    if has_tt and p.get("tiktok"):
        out += ["", "📱 TikTok / Facebook Reels:", p["tiktok"]]
    if has_yt and p.get("youtube_title"):
        out += ["", "📺 YouTube title:", p["youtube_title"], "", "📺 YouTube description:", p.get("youtube_desc", "")]
    if p.get("facebook"):
        out += ["", "📘 Facebook post:", p["facebook"]]
    if p.get("ai_film") or _AI_WORDS.search(job.get("name") or ""):
        out += ["", "🤖 AI ရုပ်ရှင် ဖြစ်နိုင်တယ် — TikTok မှာ “AI-generated” label၊ YouTube မှာ “Altered or "
                    "synthetic content → Yes”၊ Facebook မှာ “AI info” ကို ဖွင့်ပါ"]
    return "\n".join(out).strip()


def _telegram_prepare(job):
    outs = _outputs(job)
    pre = job["tg_pre"] = {"state": "uploading", "pct": 0, "files": {}}
    try:
        jlog(job, "uploading to Telegram in the background…")
        for i, (path, name) in enumerate(outs):
            if path.stat().st_size <= telegram.MAX_UPLOAD:
                continue                      # small parts go through the Bot API anyway
            def prog(f, i=i):
                pre["pct"] = int((i + f) * 100 / len(outs))
                if job.get("tg_want"):
                    job["tg"]["pct"] = pre["pct"]
            pre["files"][str(path)] = telegram.upload_parts(path, name, prog)
        pre["state"] = "ready"
        jlog(job, "Telegram upload ready")
    except Exception as e:  # noqa: BLE001 - a normal send will upload again
        pre["state"] = "failed"
        jlog(job, f"Telegram background upload failed: {e}")
    with _tg_lock:
        want = job.pop("tg_want", False)
    if want:
        _telegram_send(job)


def _send_one(job, chat, path, name, caption, prog, part=1):
    size = path.stat().st_size
    which, part = _out_kind(job, part)
    cover = _cover_file(job, part, which)
    thumb = _cover_thumb(job, part, which) or _video_thumb(path, job["dir"] / f"tg_thumb_{path.stem}.jpg")
    if size <= telegram.MAX_UPLOAD:
        w, h = video_size(path)
        telegram.send_file(chat, path, name, caption=caption, thumb=thumb, cover=cover,
                           width=w, height=h, duration=probe_duration(path))
        return "file"
    if size <= telegram.BIG_MAX and telegram.big_available():
        w, h = video_size(path)
        pre = job.get("tg_pre") or {}
        prepared = pre.get("files", {}).get(str(path)) if pre.get("state") == "ready" else None
        telegram.send_big_video(chat, path, name, caption=caption,
                                duration=probe_duration(path), width=w, height=h,
                                thumb=thumb, progress=prog, prepared=prepared, cover=cover)
        return "file"
    link, mb = _public_url(), size / 1048576
    n = job["parts"].index(path.name) + 1 if job.get("parts") else 0
    url = f"{link}/r/{job['id']}" + (f"/{n}" if n else "")
    telegram.send_message(chat, (f"{caption}\n⬇️ {url}\n({mb:.0f} MB — too big for a Telegram "
                                 f"bot, so here is the download link; it works for 24 hours)")
                          if link else f"{caption} is ready ({mb:.0f} MB) — download it from DubLab.")
    return "link"


def _telegram_send(job):
    with _tg_lock:                    # still uploading in the background → send when it ends
        pre = job.get("tg_pre") or {}
        if pre.get("state") == "uploading":
            job["tg_want"] = True
            job["tg"] = {"state": "sending", "error": "", "pct": pre.get("pct", 0)}
            return
    tg = job["tg"] = {"state": "sending", "error": "", "pct": 0}
    try:
        chat = _settings.get("telegram_chat_id")
        if not chat:
            raise telegram.TelegramError("Telegram not connected (admin page → Telegram)")
        outs = _outputs(job)
        hows, sent_covers = [], set()
        for i, (path, name) in enumerate(outs):
            jlog(job, f"sending {name} to Telegram ({path.stat().st_size / 1048576:.0f} MB)…")
            which, k = _out_kind(job, i + 1)
            tag = ("📱 TikTok" if which == "covers_tt" else "📺 YouTube") if job.get("tt") else "🎬"
            same = [x for x in range(1, len(outs) + 1) if _out_kind(job, x)[0] == which]   # this kind's files
            caption = f"{tag} {name[:-4]}" + (f"  ({same.index(i + 1) + 1}/{len(same)})" if len(same) > 1 else "")
            p = job.get("posts") or {}
            if which == "covers_tt" or not job.get("tt"):
                if p.get("tiktok") and job.get("kind") != "fix":
                    # 📱 the TikTok caption itself, ready to copy on the phone: "Title - 1/3" + text + tags
                    from dub import part_suffix
                    head = (job.get("title_mm") or "") + part_suffix(same.index(i + 1) + 1, len(same), job.get("episode"))
                    caption = (head.strip() + "\n" + p["tiktok"].strip())[:1000]
            elif p.get("youtube_title") and job.get("kind") != "fix":
                # 📺 the YouTube title, then its description, ready to copy
                caption = (p["youtube_title"].strip() + "\n\n" + (p.get("youtube_desc") or "").strip()).strip()[:1000]
            def prog(f, i=i):
                tg["pct"] = int((i + f) * 100 / len(outs))
            hows.append(_send_one(job, chat, path, name, caption, prog, i + 1))
            cov = _cover_file(job, *reversed(_out_kind(job, i + 1)))
            if cov and cov not in sent_covers:   # the part's cover, full size, to upload to TikTok etc.
                sent_covers.add(cov)
                try:
                    telegram.send_image_file(chat, cov, name[:-4] + " cover.jpg",
                                             caption=f"🖼️ {tag.split(' ')[-1] + ' ' if job.get('tt') else ''}cover")
                except Exception as e:  # noqa: BLE001 - the video itself is what matters
                    jlog(job, f"cover send failed: {e}")
            tg["pct"] = int((i + 1) * 100 / len(outs))
            tg["sent"] = i + 1
        tg["how"] = "link" if "link" in hows else "file"
        text = "" if job.get("kind") == "fix" or not job.get("want_posts") else _posts_text(job)   # 🔧 the explanation stays on the page
        if text:
            try:
                telegram.send_message(chat, text)
            except Exception as e:  # noqa: BLE001
                jlog(job, f"upload texts send failed: {e}")
        tg["state"] = "done"
        jlog(job, f"sent {len(outs)} file(s) to Telegram ({tg['how']})")
        if tg["how"] == "file":       # the videos are on Telegram now: free the Mac's disk
            keep = {p for _, p in _tk_pending(job)}   # 📱 parts still waiting for TikTok drafts stay
            for path, _ in outs:      # (a "link" send still needs the file for the download link)
                if path not in keep:
                    path.unlink(missing_ok=True)
            for f in job["dir"].glob("tg_thumb_*.jpg"):
                f.unlink(missing_ok=True)
            job["files_gone"] = not keep
            tg["gone"] = True
            jlog(job, "videos deleted from this computer (they are on Telegram)"
                 + (f" — {len(keep)} kept for TikTok drafts" if keep else ""))
        _save_meta(job)
    except Exception as e:  # noqa: BLE001 - shown in the UI
        tg.update(state="failed", error=str(e)[:300])
        jlog(job, f"Telegram send failed: {e}")


@app.post("/api/jobs/<jid>/telegram")
def telegram_send(jid):
    if not _admin_ok():
        return jsonify(error="admin password လိုတယ်"), 403
    job = jobs.get(jid)
    if not job or job["status"] != "done":
        return jsonify(error="ဗီဒီယို အဆင်သင့် မဖြစ်သေးဘူး"), 404
    state = (job.get("tg") or {}).get("state")
    if state == "sending":
        return jsonify(error="ပို့နေဆဲပါ"), 409
    if state == "done" and request.args.get("again") != "1":
        return jsonify(error="ပို့ပြီးသားပါ"), 409
    job["tg"] = {"state": "sending", "error": "", "pct": 0}
    threading.Thread(target=_telegram_send, args=(job,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/jobs/<jid>/telegram")
def telegram_job_status(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify(error="job မတွေ့ဘူး"), 404
    out = dict(job.get("tg") or {"state": "none"})
    pre = job.get("tg_pre") or {}
    out["pre"] = pre.get("state", "")       # background upload: uploading / ready / failed
    out["pre_pct"] = pre.get("pct", 0)
    return jsonify(out)


# ---------------- 📱 TikTok drafts: parts go to the owner's TikTok inbox ----------------
# Each finished part is sent as a DRAFT (Content Posting API, scope video.upload); the owner
# posts or schedules it in TikTok. TikTok allows only a few drafts waiting at a time, so a
# background loop sends at most `tiktok_daily` a day, oldest film first, part after part —
# job["tk"] = {"want": bool, "upto": n (parts 1..n done, also set by hand), "error": ""}.
_tk_lock = threading.Lock()


def _tk_parts(job):
    """The TikTok (9:16) files of a job, in part order: [(part number, path)]."""
    outs = _outputs(job)
    picked = [(i + 1, p) for i, (p, _) in enumerate(outs)
              if not job.get("tt") or _out_kind(job, i + 1)[0] == "covers_tt"]
    return [(n, p) for n, (_, p) in enumerate(picked, 1)]


def _tk_pending(job):
    tk = job.get("tk") or {}
    if not tk.get("want") or job.get("status") != "done":
        return []
    return [(n, p) for n, p in _tk_parts(job) if n > tk.get("upto", 0)]


def _tk_token():
    tok, changed = tiktok.fresh(_settings.get("tiktok_token"))
    if changed:
        _settings["tiktok_token"] = tok
        _save_settings()
    return tok["access_token"]


def _tk_sent_today():
    now = time.time()
    log = [t for t in _settings.get("tiktok_log") or [] if now - t < 86400]
    _settings["tiktok_log"] = log
    return len(log)


def _tk_send_next():
    """One part to TikTok drafts when the day's quota allows. True when one went."""
    if not (tiktok.configured() and _settings.get("tiktok_token") and _settings.get("tiktok_auto", True)):
        return False
    cap = int(_settings.get("tiktok_daily") or 0)     # 0 = no cap of our own: every part right away
    if cap and _tk_sent_today() >= cap:               # (the owner closes the laptop after the dub)
        return False
    for job in sorted(jobs.values(), key=lambda j: j.get("created", 0)):
        todo = _tk_pending(job)
        if not todo:
            continue
        n, path = todo[0]
        tk = job["tk"]
        if not path.exists():
            tk["error"] = f"part {n} ဖိုင် မရှိတော့ဘူး (ဖျက်ပြီးသား)"
            continue
        try:
            jlog(job, f"📱 sending part {n} to TikTok drafts…")
            tiktok.upload_draft(_tk_token(), path)
        except tiktok.TikTokError as e:
            tk["error"] = str(e)[:200]
            if "spam_risk" in str(e) or "too_many" in str(e):    # TikTok's own limit on waiting drafts
                tk["error"] = ("TikTok က draft အများကြီး မစောင့်ခိုင်းဘူး — ရှိပြီးသား draft တွေ post/ဖျက်ပြီးရင် "
                               "ကျန်တာ အလိုလို ဆက်ပို့မယ်")
            jlog(job, f"TikTok draft failed: {e}")
            _save_meta(job)
            return False                      # (too many waiting drafts, expired login…): try later
        tk.update(upto=n, error="", last=time.time())
        _settings.setdefault("tiktok_log", []).append(time.time())
        _save_settings()
        jlog(job, f"📱 part {n} is in TikTok drafts — open TikTok to post or schedule it")
        if (job.get("tg") or {}).get("state") == "done" and (job.get("tg") or {}).get("how") == "file":
            path.unlink(missing_ok=True)      # on Telegram and TikTok now: free the disk
            if not _tk_pending(job):
                job["files_gone"] = True
        _save_meta(job)
        return True
    return False


def _tk_loop():
    while True:
        try:
            with _tk_lock:
                while _tk_send_next():
                    pass
        except Exception as e:  # noqa: BLE001 - the loop must live on
            print(f"[tiktok] {e}")
        time.sleep(600)


def _tk_enqueue(job):
    """A finished owner's job: its parts wait for TikTok drafts (when TikTok is connected)."""
    if (job.get("owner") and job.get("kind") != "fix" and _settings.get("tiktok_token")
            and _settings.get("tiktok_auto", True) and "tk" not in job):
        job["tk"] = {"want": True, "upto": 0, "error": ""}
        _save_meta(job)
        threading.Thread(target=_tk_send_all, daemon=True).start()


def _tk_send_all():
    """Every waiting part, one after another, right now (the laptop may close after the dub)."""
    with _tk_lock:
        try:
            while _tk_send_next():
                pass
        except Exception as e:  # noqa: BLE001
            print(f"[tiktok] {e}")


threading.Thread(target=_tk_loop, daemon=True, name="tiktok").start()


@app.get("/api/tiktok")
def tiktok_state():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    queue = []
    for job in sorted(jobs.values(), key=lambda j: j.get("created", 0)):
        tk = job.get("tk")
        if not tk or job.get("status") != "done":
            continue
        total = len(_tk_parts(job))
        queue.append({"id": job["id"], "title": job.get("title_mm") or job.get("name") or job["id"],
                      "total": total, "upto": tk.get("upto", 0), "want": tk.get("want", False),
                      "error": tk.get("error", ""),
                      "missing": sum(1 for n, p in _tk_parts(job) if n > tk.get("upto", 0) and not p.exists())})
    return jsonify(configured=tiktok.configured(), connected=bool(_settings.get("tiktok_token")),
                   user=_settings.get("tiktok_user", ""), auto=_settings.get("tiktok_auto", True),
                   daily=int(_settings.get("tiktok_daily") or 0), today=_tk_sent_today(),
                   redirect=tiktok.redirect_uri(), queue=queue)


_PART_RE = re.compile(r"^(.*?)\s+-\s+(End|\d+(?:/(?:\d+|End))?)\s*$", re.I)


def _tg_groups(vids):
    """Telegram videos → films: [{"title", "parts": [{"n", "id", "label"}]}], part order."""
    films = {}
    for v in vids:
        head = (v["caption"].splitlines() or [""])[0]
        head = re.sub(r"^[📱📺🎬]\s*\S*\s*", "", head).strip() if head[:1] in "📱📺🎬" else head
        m = _PART_RE.match(head)
        title, label = (m.group(1).strip(), m.group(2)) if m else (head or "(no title)", "1")
        if v["caption"].startswith("📺"):
            continue                                   # the YouTube (16:9) copy: not for TikTok
        f = films.setdefault(title, {"title": title, "parts": [], "date": 0})
        f["date"] = max(f["date"], v["date"])
        if any(p["label"] == label for p in f["parts"]):
            continue                                   # the same part sent twice: the newest counts
        f["parts"].append({"id": v["id"], "label": label, "size": v["size"]})
    out = []
    for f in films.values():
        def key(p):
            lab = p["label"].split("/")[-1]
            return 10 ** 6 if lab.lower() == "end" else int(lab) if lab.isdigit() else 0
        f["parts"].sort(key=lambda p: (p["label"].split("/")[0] if "/" in p["label"] else "", key(p)))
        for n, part in enumerate(f["parts"], 1):
            part["n"] = n
        out.append(f)
    return sorted(out, key=lambda f: -f["date"])


_tk_tg = {"state": "", "msg": "", "films": []}


@app.get("/api/tiktok/telegram")
def tiktok_tg_list():
    """📥 Films the bot sent in the last ?days= days, split into parts (to send to TikTok drafts)."""
    if not _admin_ok():
        return jsonify(error="not found"), 404
    chat = _settings.get("telegram_chat_id")
    if not chat or not telegram.big_available():
        return jsonify(error="Telegram bot + TELEGRAM_API_ID / HASH လိုတယ်"), 400
    try:
        vids = telegram.sent_videos(chat, days=max(1, min(14, int(request.args.get("days") or 3))))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[:200]), 400
    _tk_tg["films"] = _tg_groups(vids)
    return jsonify(films=[{"title": f["title"], "parts": [{"n": p["n"], "label": p["label"]} for p in f["parts"]]}
                          for f in _tk_tg["films"]], state=_tk_tg["state"], msg=_tk_tg["msg"])


@app.post("/api/tiktok/telegram")
def tiktok_tg_send():
    """Body {title, from}: parts from..last of that film: Telegram → this Mac → TikTok drafts."""
    if not _admin_ok():
        return jsonify(error="not found"), 404
    data = request.get_json(force=True, silent=True) or {}
    film = next((f for f in _tk_tg["films"] if f["title"] == data.get("title")), None)
    if not film:
        return jsonify(error="ဇာတ်ကား မတွေ့ဘူး — 🔎 ပြန်ရှာပါ"), 404
    if _tk_tg["state"] == "sending":
        return jsonify(error="တစ်ခု ပို့နေတုန်း — ပြီးမှ ထပ်နှိပ်ပါ"), 409
    start = int(data.get("from") or 1)
    todo = [p for p in film["parts"] if p["n"] >= start]

    def run():
        tmp = JOBS / "_tiktok_tmp"
        tmp.mkdir(exist_ok=True)
        _tk_tg.update(state="sending", msg="")
        sent = 0
        try:
            for p in todo:
                _tk_tg["msg"] = f"{film['title']} part {p['label']}: Telegram ကနေ ယူနေတယ်… ({sent}/{len(todo)})"
                f = tmp / f"tg_{p['id']}.mp4"
                telegram.download_video(_settings["telegram_chat_id"], p["id"], f)
                _tk_tg["msg"] = f"{film['title']} part {p['label']}: TikTok draft ပို့နေတယ်… ({sent}/{len(todo)})"
                try:
                    tiktok.upload_draft(_tk_token(), f)
                finally:
                    f.unlink(missing_ok=True)
                sent += 1
                _settings.setdefault("tiktok_log", []).append(time.time())
                _save_settings()
            _tk_tg.update(state="done", msg=f"✅ {film['title']}: part {len(todo)} ခု TikTok draft ထဲ ရောက်ပြီ")
        except Exception as e:  # noqa: BLE001 - shown on the page
            msg = str(e)[:200]
            if "spam_risk" in msg or "too_many" in msg:
                msg = "TikTok က draft အများကြီး မစောင့်ခိုင်းဘူး — ရှိပြီးသား draft တွေ post ပြီးမှ ကျန်တာ ပို့ပါ"
            _tk_tg.update(state="failed", msg=f"⚠️ {sent}/{len(todo)} ခု ပို့ပြီး — {msg}")

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True, count=len(todo))


@app.get("/api/tiktok/telegram/status")
def tiktok_tg_status():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    return jsonify(state=_tk_tg["state"], msg=_tk_tg["msg"])


@app.get("/tiktok/login")
def tiktok_login():
    if not _admin_ok():
        return "not found", 404
    if not tiktok.configured():
        return "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET (.env) and NGROK_DOMAIN are needed", 400
    return redirect(tiktok.login_url())


@app.get("/tiktok/callback")
def tiktok_callback():
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<body style='font-family:sans-serif;background:#08080f;color:#f2f2f8;padding:40px;text-align:center'>"
            "<h2>{}</h2><p>{}</p></body>")
    if request.args.get("error"):
        return page.format("❌ TikTok", request.args.get("error_description") or request.args["error"]), 400
    if not tiktok.check_state(request.args.get("state")):
        return page.format("❌ Link expired", "Settings မှာ Connect TikTok ကို ပြန်နှိပ်ပါ"), 400
    try:
        tok = tiktok.exchange_code(request.args.get("code") or "")
        user = tiktok.user_info(tok["access_token"])
    except tiktok.TikTokError as e:
        return page.format("❌ TikTok", str(e)[:200]), 400
    _settings.update(tiktok_token=tok, tiktok_user=user.get("display_name") or tok.get("open_id", ""))
    _save_settings()
    return page.format(f"✅ TikTok connected — {_settings['tiktok_user']}",
                       "ဒီ tab ကို ပိတ်ပြီး DubLab Settings ကို ပြန်သွားလို့ရပါပြီ")


@app.post("/api/tiktok/settings")
def tiktok_settings():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    data = request.get_json(force=True, silent=True) or {}
    if "auto" in data:
        _settings["tiktok_auto"] = bool(data["auto"])
    if "daily" in data:
        _settings["tiktok_daily"] = max(0, min(50, int(data.get("daily") or 0)))
    _save_settings()
    return jsonify(ok=True)


@app.post("/api/tiktok/disconnect")
def tiktok_disconnect():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    _settings.pop("tiktok_token", None)
    _settings.pop("tiktok_user", None)
    _save_settings()
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/tiktok")
def tiktok_job(jid):
    """Body {upto: n} (parts 1..n are already on TikTok) and/or {want: bool}."""
    if not _admin_ok():
        return jsonify(error="not found"), 404
    job = jobs.get(jid)
    if not job:
        return jsonify(error="job မတွေ့ဘူး"), 404
    data = request.get_json(force=True, silent=True) or {}
    tk = job.setdefault("tk", {"want": True, "upto": 0, "error": ""})
    if "upto" in data:
        tk["upto"] = max(0, min(len(_tk_parts(job)), int(data.get("upto") or 0)))
    if "want" in data:
        tk["want"] = bool(data["want"])
    tk["error"] = ""
    _save_meta(job)
    return jsonify(ok=True, tk=tk)


_MODE_TAG = {"recap": "မြန်မာ Recap", "orig": "မြန်မာစာတန်း", "dub": "မြန်မာ အသံ"}


def _ready_title(job):
    """A YouTube-ready file name (YouTube takes the file name as the video's title):
    "စွန်းဝူခုန်း - 1 | Journey to the West (မြန်မာ Recap)" (the cover's title). None when there is no title."""
    title = (job.get("title_mm") or "").strip()
    if not title or job.get("kind") == "fix":
        return None
    ep = job.get("episode")
    film = (job.get("film_name") or "").strip()
    tag = _MODE_TAG["recap" if job.get("recap") or job.get("scene_recap") else "orig" if job.get("orig_audio") else "dub"]
    out = title + (f" - {ep}" if ep else "")          # the same as on the cover: "Title - 1"
    if film and film.casefold() not in title.casefold():
        out += f" | {film}"
    out = re.sub(r'[\\/:*?"<>\x00-\x1f]+', " ", out + f" ({tag})")   # "|" is fine in a Mac file name
    return re.sub(r"\s+", " ", out).strip()[:120]


def _download_name(job, ext, part=None, tiktok=False):
    ready = _ready_title(job)
    if ready:                                   # "… (မြန်မာ Recap).mp4", "… (မြန်မာ Recap) - TikTok 2.mp4"
        return ready + (" - TikTok" if tiktok else "") + (f" {part}" if tiktok and part else
                                                        f" - {part}" if part else "") + ext
    """Short, file-system-safe name from the source video's title or file name,
    e.g. "Bullies Picked on the Wrong Girl - mm.mp4"."""
    name = re.sub(r"#\S+", "", job.get("name") or "")        # drop hashtags
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)       # characters files can't use
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if len(name) > 50:                                       # cut at a word boundary
        name = name[:50].rsplit(" ", 1)[0].strip(" .-_,") or name[:50]
    ep = job.get("episode")                                  # a series episode: "… - mm-3" / "… - mm-3-2"
    num = f"-{ep}-{part}" if ep and part else f"-{ep}" if ep else f"-{part}" if part else ""
    return f"{name or 'dublab'} - mm" + num + ext


@app.get("/r/<jid>/cover/<int:n>")
def result_cover(jid, n):
    job = jobs.get(jid)
    covers = ((job or {}).get("covers") or []) + ((job or {}).get("covers_tt") or [])   # landscape, then 9:16
    if not job or not 1 <= n <= len(covers):
        return jsonify(error="မရှိဘူး"), 404
    tag = "tiktok-cover" if n > len(job.get("covers") or []) else "cover"
    name = _download_name(job, ".jpg")
    name = (name.replace(" - mm.jpg", f" - {tag}" + (f"-{n}" if len(covers) > 1 else "") + ".jpg") if " - mm.jpg" in name
            else name[:-4] + f" - cover {tag}" + (f" {n}" if len(covers) > 1 else "") + ".jpg")
    return send_file(job["dir"] / covers[n - 1], mimetype="image/jpeg",
                     as_attachment=request.args.get("dl") == "1", download_name=name)


@app.get("/r/<jid>.srt")
def result_srt(jid):
    job = jobs.get(jid)
    if not job or job["status"] != "done" or not (job["dir"] / "captions.srt").exists():
        return jsonify(error="မရှိဘူး"), 404
    return send_file(job["dir"] / "captions.srt", as_attachment=True,
                     download_name=_download_name(job, ".srt"), mimetype="application/x-subrip")


@app.get("/r/<jid>")
@app.get("/r/<jid>/<int:n>")
def result(jid, n=1):
    """The dubbed video, or part n of a split one."""
    job = jobs.get(jid)
    if not job or job["status"] != "done":
        return jsonify(error="အဆင်သင့် မဖြစ်သေးဘူး"), 404
    outs = _outputs(job)
    if not 1 <= n <= len(outs):
        return jsonify(error="မရှိဘူး"), 404
    path, name = outs[n - 1]
    return send_file(path, as_attachment=True, download_name=name)
