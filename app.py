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
                 mux, probe_duration, probe_url, separate_music, transcribe)
from gemini import translate_lines

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
             "caption_size": 5.5, "caption_bottom": 8, "blur_bottom": 4, "blur_height": 16}
_RANGES = {"caption_size": (3, 10), "caption_bottom": (0, 40),
           "blur_bottom": (0, 60), "blur_height": (4, 40)}
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
    src = d / ("input" + job["ext"])
    total = probe_duration(src)
    job["total"] = total
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

    job.update(stage="translate", pct=45)
    jlog(job, "translating with gemini…")
    texts = translate_lines([u["text"] for u in utter], direction, api_key,
                            LANG_NAMES.get(lang, lang), _settings["channel_name"] or None)
    for u, t in zip(utter, texts):
        u["orig"] = u["text"]
        u["text"] = t or u["text"]
    job["utter"] = utter
    jlog(job, f"translated {len(texts)} lines")


def _ends_with_channel_promo(utter, total):
    """True when the last minute or so already names the owner's channel
    (the translator swapped it in for the original creator's sign-off)."""
    name = (_settings["channel_name"] or "").casefold()
    if not name:
        return False
    tail_from = max(0.0, total - max(60.0, total * 0.1))
    return any(name in (u.get("text") or "").casefold()
               for u in utter if u["end"] >= tail_from)


def _voice_and_mux(job):
    d = job["dir"]
    utter = job["utter"]
    voice_name = VOICES[job["direction"]][job["voice"]]
    src = d / ("input" + job["ext"])
    bg_wav = None
    if job.get("keep_bg", True) and BG_VOLUME > 0:
        if demucs_available():
            job.update(stage="music", pct=56)
            jlog(job, "removing original voices (demucs)…")
            bg_wav = separate_music(src, d)
        else:
            jlog(job, "demucs not installed — background music skipped "
                      "(pip install demucs)")
    job.update(stage="voice", pct=60)
    jlog(job, f"synthesizing {len(utter)} utterances ({voice_name})…")
    def tts_prog(i, n):
        job.update(pct=60 + int(30 * i / max(n, 1)))
        if i % 5 == 0 or i == n:
            jlog(job, f"tts {i}/{n}")
    outro = _settings["outro"] if job.get("outro") else ""
    if outro and _ends_with_channel_promo(utter, job["total"]):
        jlog(job, "video already ends with a channel shout-out — outro skipped")
        outro = ""
    if outro:
        jlog(job, f"adding outro: {outro}")
    dub_wav, cues = build_dub_track(utter, voice_name, d, job["total"], tts_prog, outro or None)
    write_srt(cues, d / "captions.srt")          # always offered as a download

    job.update(stage="mux", pct=93)
    jlog(job, "muxing dubbed audio with video…")
    logo = None
    if job.get("logo") and LOGO_FILE.exists():
        logo = {"path": LOGO_FILE, "position": _settings["logo_position"],
                "size": _settings["logo_size"] / 100}
        jlog(job, f"adding logo ({_settings['logo_position']}, {_settings['logo_size']}%)")
    captions = blur = None
    if job.get("blur"):
        blur = {"bottom": _settings["blur_bottom"] / 100, "height": _settings["blur_height"] / 100}
        jlog(job, f"blurring original captions (bottom {_settings['blur_bottom']}%, "
                  f"{_settings['blur_height']}% tall)")
    if job.get("captions"):
        try:
            vw, vh = video_size(src)
            lst, strip_h = render_captions(cues, vw, vh, d / "caps", _settings["caption_size"])
            captions = {"list": lst, "height": strip_h,
                        "bottom": _settings["caption_bottom"] / 100}
            jlog(job, f"burning {len(cues)} Burmese captions")
        except Exception as e:  # noqa: BLE001 - never lose the dub over captions
            jlog(job, f"captions skipped: {e}")
    mux(src, dub_wav, d / "dubbed.mp4", bg_wav=bg_wav, bg_volume=BG_VOLUME, logo=logo,
        captions=captions, blur=blur)
    shutil.rmtree(d / "sep", ignore_errors=True)
    cleanup_segments(d)
    (d / "dub.wav").unlink(missing_ok=True)
    (d / "audio.wav").unlink(missing_ok=True)
    for tmp in ("orig_stereo.wav",):
        (d / tmp).unlink(missing_ok=True)

    if job.get("telegram"):          # mark before "done" so the page never offers a 2nd send
        job["tg"] = {"state": "sending", "error": "", "pct": 0}
    job.update(status="done", stage="done", pct=100)
    jlog(job, "done ✅")
    if job.get("telegram"):
        threading.Thread(target=_telegram_send, args=(job,), daemon=True).start()


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
    logo = request.form.get("logo", "1") == "1"
    send_tg = request.form.get("telegram") == "1" and _admin_ok() and telegram.configured()
    captions = request.form.get("captions", "1") == "1"
    blur = request.form.get("blur", "0") == "1"
    model = request.form.get("model") or default_whisper_model()
    if model not in WHISPER_MODELS:
        model = default_whisper_model()
    if direction not in VOICES or voice not in ("auto", "female", "male"):
        return jsonify(error="ရွေးချယ်မှု မှားနေတယ်"), 400
    max_sec = max_minutes() * 60
    too_long = f"{max_minutes()} မိနစ်ထက် ရှည်တဲ့ ဗီဒီယို လက်မခံဘူး"
    if url:
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
        if not url:
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
                 "model": model, "outro": outro, "logo": logo, "name": name,
                 "id": jid, "telegram": send_tg,
                 "captions": captions, "blur": blur,
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
    return jsonify(status=job["status"], stage=job["stage"], pct=job["pct"],
                   stage_pct=stage_pct,
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
                          for u in job["utter"]])


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
                   caption_bottom=_settings["caption_bottom"],
                   blur_bottom=_settings["blur_bottom"], blur_height=_settings["blur_height"],
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
    return jsonify(ok=True, **_settings)


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
    return jsonify(configured=True, authorized=True, bot=bot,
                   connected=bool(_settings.get("telegram_chat_id")),
                   chat=_settings.get("telegram_chat_name", ""))


def _save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(_settings, ensure_ascii=False))
    except OSError:
        pass


@app.post("/api/telegram/connect")
def telegram_connect():
    if not _admin_ok() or not telegram.configured():
        return jsonify(error="not found"), 404
    try:
        chat_id, name = telegram.find_chat()
        telegram.send_message(chat_id, "✅ DubLab connected — finished dubs will be sent here.")
    except telegram.TelegramError as e:
        return jsonify(error=str(e)), 400
    _settings.update(telegram_chat_id=chat_id, telegram_chat_name=name)
    _save_settings()
    return jsonify(ok=True, chat=name)


@app.post("/api/telegram/disconnect")
def telegram_disconnect():
    if not _admin_ok():
        return jsonify(error="not found"), 404
    _settings.pop("telegram_chat_id", None)
    _settings.pop("telegram_chat_name", None)
    _save_settings()
    return jsonify(ok=True)


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


def _telegram_send(job):
    tg = job["tg"] = {"state": "sending", "error": "", "pct": 0}
    try:
        chat = _settings.get("telegram_chat_id")
        if not chat:
            raise telegram.TelegramError("Telegram not connected (admin page → Telegram)")
        video = job["dir"] / "dubbed.mp4"
        name = _download_name(job, ".mp4")
        size = video.stat().st_size
        size_mb = size / 1048576
        jlog(job, f"sending to Telegram ({size_mb:.0f} MB)…")
        if size <= telegram.MAX_UPLOAD:
            telegram.send_file(chat, video, name, caption=f"🎬 {name[:-4]}")
            tg["how"] = "file"
        elif size <= telegram.BIG_MAX and telegram.big_available():
            w, h = video_size(video)
            thumb = _video_thumb(video, job["dir"] / "tg_thumb.jpg")
            def prog(f):
                tg["pct"] = int(f * 100)
            telegram.send_big_video(chat, video, name, caption=f"🎬 {name[:-4]}",
                                    duration=probe_duration(video), width=w, height=h,
                                    thumb=thumb, progress=prog)
            tg["how"] = "file"
        else:
            link = _public_url()
            if link:
                text = (f"🎬 {name[:-4]}\n⬇️ {link}/r/{job['id']}\n"
                        f"({size_mb:.0f} MB — Telegram bot can send files up to 50 MB, "
                        f"so here is the download link; it works for 24 hours)")
            else:
                text = (f"🎬 {name[:-4]} is ready ({size_mb:.0f} MB) — too big for a Telegram "
                        f"bot (50 MB limit); download it from DubLab.")
            telegram.send_message(chat, text)
            tg["how"] = "link"
        tg["state"] = "done"
        jlog(job, f"sent to Telegram ({tg['how']})")
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
    return jsonify(job.get("tg") or {"state": "none"})


def _download_name(job, ext):
    """Short, file-system-safe name from the source video's title or file name,
    e.g. "Bullies Picked on the Wrong Girl - mm.mp4"."""
    name = re.sub(r"#\S+", "", job.get("name") or "")        # drop hashtags
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name)       # characters files can't use
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if len(name) > 50:                                       # cut at a word boundary
        name = name[:50].rsplit(" ", 1)[0].strip(" .-_,") or name[:50]
    return f"{name or 'dublab'} - mm{ext}"


@app.get("/r/<jid>.srt")
def result_srt(jid):
    job = jobs.get(jid)
    if not job or job["status"] != "done" or not (job["dir"] / "captions.srt").exists():
        return jsonify(error="မရှိဘူး"), 404
    return send_file(job["dir"] / "captions.srt", as_attachment=True,
                     download_name=_download_name(job, ".srt"), mimetype="application/x-subrip")


@app.get("/r/<jid>")
def result(jid):
    job = jobs.get(jid)
    if not job or job["status"] != "done":
        return jsonify(error="အဆင်သင့် မဖြစ်သေးဘူး"), 404
    return send_file(job["dir"] / "dubbed.mp4", as_attachment=True,
                     download_name=_download_name(job, ".mp4"))
