"""DubLab — short-video dubbing website. Upload video -> dubbed video."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file

import telegram

from dub import (WHISPER_MODELS, build_dub_track, cleanup_segments, configure_tts,
                 normalize_logo, render_captions, video_size, write_srt,
                 default_whisper_model, demucs_available, detect_gender,
                 download_url, extract_audio,
                 has_audio_stream, has_video_stream, is_supported_url, merge_segments,
                 build_recap_video, make_covers, mux, synthesize, probe_duration, probe_url, separate_music, split_points,
                 split_video, transcribe)
from gemini import (TTS_CPS, RateLimited, pick_cover_frames, translate_lines, write_intro, write_recap,
                    write_title)

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
SPLIT_CHOICES = (0, 10, 15, 20, 30)
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
             "channel_name": (os.environ.get("DUBLAB_CHANNEL_NAME") or "Marco Recap").strip()[:CHANNEL_MAX],
             "logo_position": "top-right",
             "logo_size": 12,   # % of the video width
             # Burmese captions: text size (% of video height); blur band that
             # hides burned-in original captions (% from bottom, % tall)
             "caption_size": 5.5, "caption_bottom": 8, "blur_bottom": 4, "blur_height": 16,
             # portrait (TikTok/Shorts) videos: TikTok's own UI covers the bottom
             # ~20%, and burned-in captions there usually sit higher up
             "caption_bottom_v": 24, "blur_bottom_v": 20, "blur_height_v": 12}
_RANGES = {"caption_size": (3, 10), "caption_bottom": (0, 40),
           "blur_bottom": (0, 60), "blur_height": (4, 40),
           "caption_bottom_v": (0, 60), "blur_bottom_v": (0, 70), "blur_height_v": (4, 40)}
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
LANG_NAMES = {"zh": "Chinese (Mandarin)", "yue": "Cantonese", "en": "English",
              "ja": "Japanese", "ko": "Korean", "th": "Thai", "hi": "Hindi",
              "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
              "ru": "Russian", "es": "Spanish", "fr": "French", "de": "German"}
MAX_LINE = 500          # chars per edited line
STAGE_LABEL = {
    "queue": "တန်းစီနေတယ်",
    "download": "ဗီဒီယို ဒေါင်းနေတယ်",
    "audio": "အသံထုတ်ယူနေတယ်",
    "transcribe": "စကားပြောမှတ်နေတယ်",
    "translate": "ဘာသာပြန်နေတယ်",
    "review": "ဘာသာပြန်စာ ပြင်ဖို့ စောင့်နေတယ်",
    "music": "နောက်ခံ music ခွဲထုတ်နေတယ်",
    "voice": "အသံဖော်နေတယ်",
    "mux": "ဗီဒီယိုပေါင်းနေတယ်",
    "split": "အပိုင်းခွဲနေတယ်",
    "recap": "Recap အခန်းတွေ ဖြတ်ဆက်နေတယ်",
    "done": "ပြီးပြီ",
}
STAGE_RANGE = {"transcribe": (8, 44), "voice": (60, 90)}  # overall pct span

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = max_mb() * 1024 * 1024

jobs = {}
queue = []
lock = threading.Condition()


def jlog(job, msg):
    """Append a timestamped line to the job's live log file."""
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
              "part_times", "covers", "cover_pick", "created", "owner", "recap", "tg", "url", "files_gone")


def _save_meta(job):
    try:
        meta = {k: job[k] for k in _META_KEYS if k in job}
        if (meta.get("tg") or {}).get("state") == "sending":   # can't resume a send
            meta["tg"] = {"state": "none"}
        (job["dir"] / "job.json").write_text(json.dumps(meta, ensure_ascii=False))
    except (OSError, TypeError, ValueError):
        pass


def _load_saved_jobs():
    """Finished jobs from before a restart come back (until the TTL sweep)."""
    for f in JOBS.glob("*/job.json"):
        try:
            meta = json.loads(f.read_text())
            if meta.get("status") == "done" and time.time() - meta.get("created", 0) < TTL:
                meta["dir"] = f.parent
                jobs.setdefault(meta["id"], meta)
        except (OSError, ValueError, KeyError):
            pass


_load_saved_jobs()


def sweep_old():
    now = time.time()
    for jid, job in list(jobs.items()):
        if now - job["created"] > TTL and job["status"] in ("done", "failed", "review"):
            shutil.rmtree(job["dir"], ignore_errors=True)
            jobs.pop(jid, None)


def process_job(jid):
    job = jobs[jid]
    try:
        if job.get("phase") == 2:
            _voice_and_mux(job)
        else:
            _transcribe_and_translate(jid, job)
            if job.get("review"):
                job.update(status="review", stage="review", pct=55)
                jlog(job, "waiting for you to check the translation ✏️")
                return
            _voice_and_mux(job)
    except Exception as e:  # noqa: BLE001 - surfaced to UI
        err = clean_err(e)
        jlog(job, f"FAILED: {type(e).__name__}: {err}")
        job.update(status="failed", error=err)
        # a failed job can't be resumed (🔁 starts over): free the disk now
        for f in ("input" + job.get("ext", ".mp4"), "audio.wav"):
            (job["dir"] / f).unlink(missing_ok=True)


def _transcribe_and_translate(jid, job):
    d = job["dir"]
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("server config missing (GEMINI_API_KEY)")
    direction = job["direction"]
    jlog(job, f"job {jid} started · {direction} · {job['voice']} voice")
    if job.get("url"):
        job.update(stage="download", pct=2)
        jlog(job, "downloading video from link…")
        try:
            download_url(job["url"], d, max_mb())
        except Exception as e:  # noqa: BLE001
            jlog(job, f"download error: {str(e)[:300]}")
            raise ValueError("ဗီဒီယို ဒေါင်းမရဘူး — link စစ်ပါ၊ "
                             "ခဏနေ ပြန်စမ်းပါ") from None
    if job.get("tg_src"):
        job.update(stage="download", pct=2)
        jlog(job, "downloading video from Telegram…")
        last = [0]
        def dl_prog(f):
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
    total = probe_duration(src)
    job["total"] = total
    if job.get("recap") and job["recap"] * RECAP_AIM * 60 >= total * 0.85:
        jlog(job, f"video is only {total / 60:.0f} min — no shorter recap possible, doing a full dub")
        job["recap"] = 0
    jlog(job, f"video duration: {total:.1f}s")
    if not has_video_stream(src):
        raise ValueError("ဗီဒီယိုဖိုင် မဟုတ်ဘူး")
    if not has_audio_stream(src):
        raise ValueError("ဒီဗီဒီယိုမှာ အသံမပါဘူး — အသံပါတဲ့ ဗီဒီယိုမှ dub လို့ရမယ်")

    job.update(stage="audio", pct=4)
    jlog(job, "extracting audio…")
    wav = d / "audio.wav"
    extract_audio(src, wav)

    job.update(stage="transcribe", pct=8)
    jlog(job, "transcribing with whisper…")
    last_log = [0]
    def stt_prog(frac):  # 8% → 44% while whisper works through the audio
        job.update(pct=8 + int(36 * frac))
        if frac * 100 >= last_log[0] + 10:
            last_log[0] = int(frac * 10) * 10
            jlog(job, f"transcribing… {last_log[0]}%")
    jlog(job, f"speech model: {job.get('model') or default_whisper_model()}")
    segs, lang = transcribe(wav, SRC_LANG[direction], stt_prog, job.get("model"))
    if not segs:
        raise ValueError("အသံထဲမှာ စကားပြောသံ မတွေ့ဘူး")
    if direction == "auto2mm":
        jlog(job, f"detected language: {lang}")
        if lang == "my":
            raise ValueError("ဒီဗီဒီယိုက မြန်မာလို ပြောထားပြီးသားပါ — "
                             "တရုတ်/English စတဲ့ တခြားဘာသာ ဗီဒီယိုပဲ dub လို့ရတယ်")
        direction = {"zh": "zh2mm", "en": "en2mm"}.get(lang, "any2mm")
    utter = merge_segments(segs)
    jlog(job, f"transcribed {len(segs)} segments → {len(utter)} utterances")
    if job["voice"] == "auto":
        try:
            g, f0 = detect_gender(wav, segs)
        except Exception as e:  # noqa: BLE001 - never fail a job over this
            g, f0 = "female", 0.0
            jlog(job, f"voice detection error: {e}")
        job["voice"] = g
        jlog(job, f"auto voice: {g} (speaker pitch ≈ {f0:.0f} Hz)")

    if job.get("recap"):
        _write_recap(job, utter, lang, api_key)
        utter = job["utter"]
    else:
        _translate(job, utter, direction, lang, api_key)
    if job.get("intro"):
        try:
            job["intro_text"] = write_intro([u["text"] for u in utter], api_key)
            jlog(job, f"intro: {job['intro_text']}")
        except Exception as e:  # noqa: BLE001 - the dub works without it
            job["intro_text"] = ""
            jlog(job, f"intro skipped: {e}")
    if job.get("cover"):
        try:
            job["title_mm"] = write_title([u["text"] for u in utter], api_key)
            jlog(job, f"cover title: {job['title_mm']}")
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


def _translate(job, utter, direction, lang, api_key):
    job.update(stage="translate", pct=45)
    jlog(job, "translating with gemini…")
    try:
        texts = translate_lines([u["text"] for u in utter], direction, api_key,
                                LANG_NAMES.get(lang, lang), _settings["channel_name"] or None)
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


def _write_recap(job, utter, lang, api_key):
    """Recap mode: Gemini writes a shorter Burmese narration in beats, each
    tied to the scene (transcript lines) it describes."""
    job.update(stage="translate", pct=45)
    mins = job["recap"] * RECAP_AIM
    jlog(job, f"writing a Burmese recap of at least {job['recap']} min (aiming ~{mins:.0f}) with gemini…")
    try:
        beats = write_recap([(u["start"], u["text"]) for u in utter], mins, api_key,
                            LANG_NAMES.get(lang, lang), _settings["channel_name"] or None,
                            min_minutes=job["recap"], log=lambda m: jlog(job, m))
    except RateLimited as e:
        raise ValueError("Gemini quota ဒီနေ့အတွက် ကုန်သွားပြီ — မနက်ဖြန် ပြန်စမ်းပါ" if e.daily else
                         "Gemini က ခဏ ပိတ်ထားတယ် — ၁–၂ မိနစ်နေရင် 🔁 ပြန်စမ်းပါ") from None
    if not beats:
        raise ValueError("Recap ရေးလို့ မရဘူး — ပြန်စမ်းပါ")
    job["utter"] = [{"start": utter[b["from"]]["start"], "end": utter[b["to"]]["end"],
                     "text": b["say"][:MAX_LINE],
                     "orig": " ".join(u["text"] for u in utter[b["from"]:b["to"] + 1])[:300]}
                    for b in beats]
    est = sum(len(u["text"]) for u in job["utter"]) / TTS_CPS / 60
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
        synthesize(utter[i]["text"], voice_name, p)
        done[0] += 1
        job.update(pct=58 + int(20 * done[0] / len(utter)))
        return p, probe_duration(p)
    with ThreadPoolExecutor(max(1, int(os.environ.get("DUBLAB_TTS_WORKERS") or 8))) as ex:
        voiced = list(ex.map(tts, range(len(utter))))
    GAP = 0.35
    clips = [(u["start"], dur + GAP) for u, (_, dur) in zip(utter, voiced)]
    job.update(stage="recap", pct=78)
    jlog(job, f"cutting {len(clips)} scenes into the recap…")
    def prog(i, n):
        job.update(pct=78 + int(12 * i / max(n, 1)))
    out = d / "recap.mp4"
    total = build_recap_video(src, clips, out, d / "clips", prog)
    new, t = [], 0.0
    for u, (p, dur) in zip(utter, voiced):
        new.append({"start": t, "end": t + dur, "text": u["text"], "audio": str(p)})
        t += dur + GAP
    jlog(job, f"recap video: {total / 60:.1f} min")
    return out, new, total


def _voice_and_mux(job):
    d = job["dir"]
    utter = job["utter"]
    voice_name = VOICES[job["direction"]][job["voice"]]
    src = d / ("input" + job["ext"])
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
    if not job.get("recap"):
        job.update(stage="voice", pct=60)
        jlog(job, f"synthesizing {len(utter)} utterances ({voice_name})…")
    def tts_prog(i, n):
        if job.get("recap"):
            return
        job.update(pct=60 + int(30 * i / max(n, 1)))
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
    dub_wav, cues, lead = build_dub_track(utter, voice_name, d, job["total"], tts_prog,
                                          outro or None, intro or None)
    total = job["total"] + lead                 # the held first frame makes it longer
    write_srt(cues, d / "captions.srt")          # always offered as a download

    job.update(stage="mux", pct=93)
    jlog(job, "muxing dubbed audio with video…")
    logo = None
    if job.get("logo") and LOGO_FILE.exists():
        logo = {"path": LOGO_FILE, "position": _settings["logo_position"],
                "size": _settings["logo_size"] / 100}
        jlog(job, f"adding logo ({_settings['logo_position']}, {_settings['logo_size']}%)")
    captions = blur = None
    try:
        vw, vh = video_size(src)
    except Exception:  # noqa: BLE001
        vw, vh = 1920, 1080
    sfx = "_v" if vh > vw else ""        # portrait video → its own positions
    if job.get("blur"):
        bb, bh = _settings["blur_bottom" + sfx], _settings["blur_height" + sfx]
        blur = {"bottom": bb / 100, "height": bh / 100}
        jlog(job, f"blurring original captions ({'portrait' if sfx else 'landscape'}: "
                  f"bottom {bb}%, {bh}% tall)")
    if job.get("captions"):
        try:
            lst, strip_h = render_captions(cues, vw, vh, d / "caps", _settings["caption_size"])
            captions = {"list": lst, "height": strip_h,
                        "bottom": _settings["caption_bottom" + sfx] / 100}
            jlog(job, f"burning {len(cues)} Burmese captions")
        except Exception as e:  # noqa: BLE001 - never lose the dub over captions
            jlog(job, f"captions skipped: {e}")
    if job.get("cover") and job.get("title_mm"):
        # one cover for the whole film — every part uses the same one; made
        # before the mux so cover 1 can also be the picture under the intro
        try:
            key = os.environ.get("GEMINI_API_KEY")
            covers = make_covers(src, job["title_mm"], d, job["total"] * 0.04, job["total"] * 0.9,
                                 portrait=bool(sfx), blur=blur,
                                 logo=LOGO_FILE if job.get("logo") and LOGO_FILE.exists() else None,
                                 count=1, chooser=(lambda shots: pick_cover_frames(shots, key)) if key else None,
                                 log=lambda m: jlog(job, m))
            job["covers"] = [c.name for c in covers]
            job["cover_pick"] = 1
            jlog(job, f"made {len(covers)} cover images")
        except Exception as e:  # noqa: BLE001 - never lose the dub over a cover
            jlog(job, f"cover skipped: {e}")
    intro_img = d / job["covers"][0] if lead and job.get("covers") else None
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
    if cuts:
        jlog(job, f"splitting into {len(cuts) + 1} parts of ~{job['split']} min "
                  f"(cuts at {', '.join(f'{c / 60:.1f}' for c in cuts)} min)")
    mux(src, dub_wav, d / "dubbed.mp4", bg_wav=bg_wav, bg_volume=BG_VOLUME, logo=logo,
        captions=captions, blur=blur, keyframes=cuts or None, lead=lead, tail=tail,
        intro_img=intro_img)
    if cuts:
        job.update(stage="split", pct=97)
        jlog(job, "cutting the parts…")
        parts = split_video(d / "dubbed.mp4", cuts, d)
        (d / "dubbed.mp4").unlink(missing_ok=True)        # the parts replace it
        job["parts"] = [p.name for p in parts]
        job["part_times"] = list(zip([0.0, *cuts], [*cuts, total]))
    shutil.rmtree(d / "sep", ignore_errors=True)
    cleanup_segments(d)
    (d / "dub.wav").unlink(missing_ok=True)
    (d / "audio.wav").unlink(missing_ok=True)
    (d / ("input" + job["ext"])).unlink(missing_ok=True)   # the source: covers/parts are made, not needed now
    for tmp in ("orig_stereo.wav",):
        (d / tmp).unlink(missing_ok=True)

    (d / "recap.mp4").unlink(missing_ok=True)          # recap intermediate
    for f in d.glob("r*.mp3"):
        f.unlink(missing_ok=True)
    if job.get("telegram"):          # mark before "done" so the page never offers a 2nd send
        job["tg"] = {"state": "sending", "error": "", "pct": 0}
    job.update(status="done", stage="done", pct=100)
    _save_meta(job)
    jlog(job, "done ✅")
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
        # -i: no idle sleep; -s: no system sleep while on AC power
        return subprocess.Popen(["caffeinate", "-i", "-s"])
    except OSError:
        return None


def worker():
    while True:
        with lock:
            while not queue:
                lock.wait()
            jid = queue.pop(0)
        jobs[jid]["status"] = "processing"
        awake = _stay_awake()
        try:
            process_job(jid)
        finally:
            if awake:
                awake.terminate()
        sweep_old()


threading.Thread(target=worker, daemon=True).start()


@app.get("/")
def index():
    return render_template("index.html", max_minutes=max_minutes(), max_mb=max_mb(),
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
    intro = request.form.get("intro", "1") == "1"
    cover = request.form.get("cover", "1") == "1"
    logo = request.form.get("logo", "1") == "1"
    send_tg = request.form.get("telegram") == "1" and _admin_ok() and telegram.configured()
    captions = request.form.get("captions", "1") == "1"
    try:
        split = int(request.form.get("split") or 0)
    except ValueError:
        split = 0
    split = split if split in SPLIT_CHOICES else 0
    blur = request.form.get("blur", "0") == "1"
    model = request.form.get("model") or default_whisper_model()
    if model not in WHISPER_MODELS:
        model = default_whisper_model()
    if direction not in VOICES or voice not in ("auto", "female", "male"):
        return jsonify(error="ရွေးချယ်မှု မှားနေတယ်"), 400
    try:
        recap = int(request.form.get("recap") or 0)
    except ValueError:
        recap = 0
    recap = recap if recap in RECAP_CHOICES else 0
    # the owner (admin password) may dub/recap any length up to OWNER_MAX_MINUTES
    lim = OWNER_MAX_MINUTES if _admin_ok() else max_minutes()
    max_sec = lim * 60
    too_long = f"{lim} မိနစ်ထက် ရှည်တဲ့ ဗီဒီယို လက်မခံဘူး"
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
                 "model": model, "outro": outro, "intro": intro, "cover": cover, "logo": logo, "name": name,
                 "recap": recap,
                 "tg_src": tg_msg,
                 "id": jid, "telegram": send_tg, "owner": _admin_ok(),
                 "captions": captions, "blur": blur, "split": split,
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
    parts = []
    if job["status"] == "done" and job.get("parts"):
        parts = [{"n": i, "name": name, "start": round(a), "end": round(b)}
                 for i, ((_, name), (a, b)) in enumerate(zip(_outputs(job), job["part_times"]), 1)]
    return jsonify(status=job["status"], stage=job["stage"], pct=job["pct"],
                   stage_pct=stage_pct, parts=parts,
                   covers=len(job.get("covers") or []) if job["status"] == "done" else 0,
                   cover_pick=job.get("cover_pick", 1), title=job.get("title_mm", ""),
                   name=job.get("name", ""), created=int(job.get("created", 0)),
                   gone=bool(job.get("files_gone")),
                   label=STAGE_LABEL.get(job["stage"], job["stage"]),
                   error=job["error"])


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


HISTORY_MAX = 10


@app.get("/admin/api/history")
def admin_api_history():
    """The newest jobs on the server (finished ones survive restarts for 24 h),
    so a result lost to a refresh or a dropped connection can be fetched again."""
    if not _admin_ok():
        return jsonify(error="not found"), 404
    rows = []
    for jid, job in sorted(jobs.items(), key=lambda kv: kv[1].get("created", 0), reverse=True):
        if len(rows) >= HISTORY_MAX:
            break
        done = job["status"] == "done"
        outs = []
        if done:
            try:
                outs = [{"n": i, "name": name, "mb": round(path.stat().st_size / 1048576)}
                        for i, (path, name) in enumerate(_outputs(job), 1) if path.exists()]
            except OSError:
                outs = []
        tg = job.get("tg") or {}
        rows.append({"id": jid, "name": job.get("name") or "", "title": job.get("title_mm") or "",
                     "url": job.get("url") or "", "created": int(job.get("created", 0)),
                     "status": job["status"], "pct": job.get("pct", 0),
                     "label": STAGE_LABEL.get(job["stage"], job["stage"]), "error": job.get("error", ""),
                     "recap": job.get("recap") or 0, "outputs": outs,
                     "covers": len(job.get("covers") or []) if done else 0,
                     "cover_pick": job.get("cover_pick", 1),
                     "tg": tg.get("state", "none"), "tg_pct": tg.get("pct", 0),
                     "tg_error": tg.get("error", ""), "gone": bool(job.get("files_gone"))})
    return jsonify(jobs=rows, ttl_hours=TTL // 3600)


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
                _settings[k] = max(lo, min(hi, round(float(data[k]), 1)))
            except (TypeError, ValueError):
                pass
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
    return jsonify(videos=vids, bot=bot, max_minutes=max_minutes())


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


def _cover_file(job):
    """The film's one cover (the same for every part), or None."""
    covers = job.get("covers") or []
    return job["dir"] / covers[0] if covers and (job["dir"] / covers[0]).exists() else None


def _cover_thumb(job):
    """The cover, shrunk to Telegram's thumbnail size (≤320 px JPEG)."""
    covers = job.get("covers") or []
    n = 1
    if not covers:
        return None
    src, out = job["dir"] / covers[n - 1], job["dir"] / f"tg_cover{n}.jpg"
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
    """[(path, download name)] — one file, or the parts of a split video."""
    d = job["dir"]
    if job.get("parts"):
        return [(d / f, _download_name(job, ".mp4", i)) for i, f in enumerate(job["parts"], 1)]
    return [(d / "dubbed.mp4", _download_name(job, ".mp4"))]


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


def _send_one(job, chat, path, name, caption, prog):
    size = path.stat().st_size
    cover = _cover_file(job)
    thumb = _cover_thumb(job) or _video_thumb(path, job["dir"] / f"tg_thumb_{path.stem}.jpg")
    if size <= telegram.MAX_UPLOAD:
        telegram.send_file(chat, path, name, caption=caption, thumb=thumb, cover=cover)
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
        hows = []
        for i, (path, name) in enumerate(outs):
            jlog(job, f"sending {name} to Telegram ({path.stat().st_size / 1048576:.0f} MB)…")
            caption = f"🎬 {name[:-4]}" + (f"  ({i + 1}/{len(outs)})" if len(outs) > 1 else "")
            def prog(f, i=i):
                tg["pct"] = int((i + f) * 100 / len(outs))
            hows.append(_send_one(job, chat, path, name, caption, prog))
            tg["pct"] = int((i + 1) * 100 / len(outs))
            tg["sent"] = i + 1
        tg["how"] = "link" if "link" in hows else "file"
        tg["state"] = "done"
        jlog(job, f"sent {len(outs)} file(s) to Telegram ({tg['how']})")
        if tg["how"] == "file":       # the videos are on Telegram now: free the Mac's disk
            for path, _ in outs:      # (a "link" send still needs the file for the download link)
                path.unlink(missing_ok=True)
            for f in job["dir"].glob("tg_thumb_*.jpg"):
                f.unlink(missing_ok=True)
            job["files_gone"] = True
            tg["gone"] = True
            jlog(job, "videos deleted from this computer (they are on Telegram)")
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


def _download_name(job, ext, part=None):
    """Short, file-system-safe name from the source video's title or file name,
    e.g. "Bullies Picked on the Wrong Girl - mm.mp4"."""
    name = re.sub(r"#\S+", "", job.get("name") or "")        # drop hashtags
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)       # characters files can't use
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if len(name) > 50:                                       # cut at a word boundary
        name = name[:50].rsplit(" ", 1)[0].strip(" .-_,") or name[:50]
    return f"{name or 'dublab'} - mm" + (f"-{part}" if part else "") + ext


@app.get("/r/<jid>/cover/<int:n>")
def result_cover(jid, n):
    job = jobs.get(jid)
    covers = (job or {}).get("covers") or []
    if not job or not 1 <= n <= len(covers):
        return jsonify(error="မရှိဘူး"), 404
    name = _download_name(job, ".jpg").replace(" - mm.jpg", " - cover.jpg")
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
