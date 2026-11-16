"""Dubbing pipeline: extract audio -> transcribe -> translate -> TTS -> mux."""
import os
import re
import subprocess
from pathlib import Path

MAX_ATEMPO = 1.65  # never speed speech up more than this (1.5->1.8 on 2026-10-26 to fix ~1.7s drift, then 1.8->1.65 same night: 1.8 sounded too fast; 1.65 fits every ratio measured on the Oct 25 test video, 1.61-1.67, with ~0.04s total drift)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffmpeg failed").strip()[-400:])
    return p


def probe_duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(p.stdout.strip())


def has_video_stream(path):
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)])
    return "video" in p.stdout


def has_audio_stream(path):
    p = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)])
    return "audio" in p.stdout


def extract_audio(src, dst):
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", str(dst)])


from gemini_tts import QuotaExhausted
from gemini_tts import configure as _tts_configure
from gemini_tts import synthesize as _gemini_tts

# Edge-TTS voice name -> Gemini prebuilt voice (male/female pairs)
GEMINI_VOICE_FOR = {    "my-MM-NilarNeural": "Aoede",    # female Burmese
    "my-MM-ThihaNeural": "Charon",   # male Burmese
    "en-US-AvaNeural": "Aoede",      # female English
    "en-US-AndrewNeural": "Charon",  # male English
}

# When Edge-TTS recovers, the recovery watcher touches this flag file and the
# TTS order flips back to Edge-first automatically (no redeploy needed).
EDGE_PRIMARY_FLAG = Path(__file__).parent / ".edge_primary"


def _edge_first():
    """Edge-TTS (free, fast, no daily quota) is primary unless
    DUBLAB_TTS_PRIMARY=gemini; the .edge_primary flag file also forces Edge."""
    if (os.environ.get("DUBLAB_TTS_PRIMARY") or "edge").strip().lower() != "gemini":
        return True
    try:
        return EDGE_PRIMARY_FLAG.exists()
    except OSError:
        return False


def configure_tts(keys):
    """Configure Gemini TTS key rotation. Call once at startup."""
    _tts_configure(keys)


WHISPER_MODELS = ("base", "small")   # offered in the UI: fast / accurate
_models = {}          # model name -> loaded WhisperModel (kept for reuse)
_cpu_only = False     # set after a GPU failure


def default_whisper_model():
    return os.environ.get("DUBLAB_WHISPER_MODEL") or "small"


def transcribe(wav, lang_hint=None, progress=None, model_name=None):
    """Return ([{start, end, text}], language).

    lang_hint: 'en', 'my', 'zh'… or None to let whisper detect the
    language; the returned language is the hint or the detected code.
    model_name: 'base' (fast) or 'small' (more accurate); default from .env."""
    global _cpu_only
    name = model_name or default_whisper_model()
    try:
        return _transcribe(wav, lang_hint, progress, name)
    except Exception as e:  # noqa: BLE001 - GPU backends are experimental
        if _cpu_only:
            raise
        print(f"[whisper] GPU failed ({type(e).__name__}: {e}); retrying on CPU")
        _cpu_only = True
        _models.clear()
        return _transcribe(wav, lang_hint, progress, name)


def _whisper_device():
    forced = os.environ.get("DUBLAB_WHISPER_DEVICE", "").strip()
    if forced:
        return forced
    import platform
    import shutil
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mps"      # Apple GPU: ~10x faster than CPU on an M1
    if shutil.which("nvidia-smi"):
        return "cuda"
    return "cpu"


def _get_model(name):
    global _cpu_only
    if name in _models:
        return _models[name]
    from faster_whisper import WhisperModel
    device = "cpu" if _cpu_only else _whisper_device()
    try:
        if device == "cpu":
            m = WhisperModel(name, device="cpu", compute_type="int8",
                             cpu_threads=os.cpu_count() or 4)
        else:
            m = WhisperModel(name, device=device, compute_type="float16")
    except Exception as e:  # noqa: BLE001
        if device == "cpu":
            raise
        print(f"[whisper] GPU load failed ({e}); using CPU")
        _cpu_only = True
        m = WhisperModel(name, device="cpu", compute_type="int8",
                         cpu_threads=os.cpu_count() or 4)
        device = "cpu"
    print(f"[whisper] model {name} on {device}")
    _models[name] = m
    return m


def _transcribe(wav, lang_hint, progress, name):
    model = _get_model(name)
    # beam 1 (greedy) is ~3x faster than the default 5 with near-identical text
    beam = int(os.environ.get("DUBLAB_WHISPER_BEAM") or 1)
    segments, info = model.transcribe(
        str(wav), language=lang_hint, vad_filter=True, beam_size=beam,
        vad_parameters=dict(min_silence_duration_ms=400))
    segs = []
    for s in segments:  # lazy generator: decoding happens while we iterate
        if s.text and s.text.strip():
            segs.append({"start": float(s.start), "end": float(s.end),
                         "text": s.text.strip()})
        if progress and info.duration:
            progress(min(float(s.end) / info.duration, 1.0))
    return segs, (lang_hint or info.language)


def detect_gender(wav, segs, max_speech=120.0):
    """Guess the main speaker's gender from voice pitch.

    Looks at up to max_speech seconds of the speech segments in a 16 kHz mono
    wav, estimates the pitch of each voiced 40 ms frame by autocorrelation and
    compares the median with ~165 Hz (typical male 85-155 Hz, female
    165-255 Hz). Returns ("male" | "female", median_hz); falls back to
    ("female", 0) when too little voiced speech is found.
    """
    import wave
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            return "female", 0.0
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = audio.astype(np.float32) / 32768.0
    frame, hop = int(0.04 * sr), int(0.02 * sr)
    lo, hi = int(sr / 400), int(sr / 70)          # pitch search range 70-400 Hz
    pitches, used = [], 0.0
    for s in segs:
        if used >= max_speech:
            break
        a, b = int(s["start"] * sr), int(s["end"] * sr)
        used += s["end"] - s["start"]
        for i in range(a, max(a, b - frame), hop):
            x = audio[i:i + frame]
            if len(x) < frame or np.sqrt(np.mean(x * x)) < 0.01:
                continue                          # silence
            x = x - x.mean()
            ac = np.correlate(x, x, "full")[frame - 1:]
            if ac[0] <= 0:
                continue
            ac = ac / ac[0]
            lag = lo + int(np.argmax(ac[lo:hi]))
            if ac[lag] > 0.45:                    # clearly voiced frame
                pitches.append(sr / lag)
    if len(pitches) < 20:
        return "female", 0.0
    f0 = float(np.median(pitches))
    return ("male" if f0 < 165 else "female"), f0


def merge_segments(segs, max_gap=0.6, max_len=10.0):
    """Merge close whisper segments into speakable utterances."""
    merged = []
    for s in segs:
        if (merged and s["start"] - merged[-1]["end"] <= max_gap
                and s["end"] - merged[-1]["start"] <= max_len):
            merged[-1]["end"] = s["end"]
            merged[-1]["text"] = merged[-1]["text"] + " " + s["text"]
        else:
            merged.append(dict(s))
    return merged


def _synth_gemini(text, voice, lang, out_path):
    _gemini_tts(text, GEMINI_VOICE_FOR.get(voice, "Aoede"), lang, out_path)


_edge_urls = None   # [current, other] — the working endpoint moves to the front


def _synth_edge(text, voice, out_path):
    import asyncio
    import edge_tts
    import edge_tts.communicate as _comm
    import edge_tts.constants as _C
    global _edge_urls

    # Two endpoints serve the same voices: the library's stock
    # speech.platform.bing.com one, and api.msedgeservices.com (token as
    # Ocp-Apim-Subscription-Key). 2026-10: stock answered 403 in some regions;
    # 2026-11: the alternate one answered 429 while stock worked. Try the last
    # one that worked first, then the other.
    if _edge_urls is None:
        _edge_urls = [_C.WSS_URL,
                      "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
                      f"?Ocp-Apim-Subscription-Key={_C.TRUSTED_CLIENT_TOKEN}"]

    async def _go():
        await edge_tts.Communicate(text, voice).save(str(out_path))

    err = None
    for url in list(_edge_urls):
        _comm.WSS_URL = _C.WSS_URL = url
        try:
            asyncio.run(_go())
            if _edge_urls[0] != url:
                _edge_urls.remove(url)
                _edge_urls.insert(0, url)
            return
        except Exception as e:  # noqa: BLE001
            err = e
    raise err


def synthesize(text, voice, out_path):
    """TTS one utterance.

    Order: Edge-TTS first when the .edge_primary flag file exists (Microsoft
    recovered — the recovery watcher creates it), else Gemini TTS (primary)
    -> Edge-TTS -> gTTS (last resort, robotic single voice).
    """
    lang = "my" if voice.startswith("my-MM") else "en"
    err = None
    order = ("edge", "gemini") if _edge_first() else ("gemini", "edge")
    for which in order:
        try:
            if which == "edge":
                _synth_edge(text, voice, out_path)
            else:
                _synth_gemini(text, voice, lang, out_path)
            return
        except QuotaExhausted:
            # Don't silently fall through on quota - the user should know to wait.
            # Edge-TTS/gTTS can't cover a sustained quota outage anyway.
            raise
        except Exception as e:  # noqa: BLE001
            err = e
            print(f"[tts] {which} failed ({type(e).__name__}), trying next")
    # fallback: gTTS (free, single voice per language — no male/female choice)
    try:
        from gtts import gTTS
        gTTS(text, lang=lang).save(str(out_path))
        print(f"[tts] used gTTS fallback (previous: {type(err).__name__})")
    except Exception:
        raise err


def build_dub_track(utterances, voice, tmpdir, total_dur, progress=None, outro=None):
    """TTS each utterance, time-fit into its slot, mix into one dub track.

    Finish-the-sentence rule (2026-10-25, user order): an utterance is
    NEVER trimmed. It plays to completion (after atempo speed-up, capped
    at MAX_ATEMPO). Sync rule (2026-10-26, user order): the speed-up is
    tried FIRST, so an utterance fits its own slot whenever possible and
    stays in sync with the picture; only if it still overruns (ratio >
    MAX_ATEMPO) does the next utterance start after it finishes (cascade),
    so drift is limited to the excess over the cap. The tail may run a
    little past the video end, but no sentence is ever cut off mid-word.
    """
    tmpdir = Path(tmpdir)
    cursor = 0.0  # earliest start for the next utterance
    # GAP is 0 on purpose: any fixed gap accumulates over dozens of
    # utterances (0.15s x 37 = ~4-5s drift by the end). With 0, the total
    # drift is only the sum of genuine overruns past MAX_ATEMPO (~0.04s at
    # 1.65 on the Oct 25 test video; was ~1.7s at 1.5, ~0.03s at 1.8).
    # The next utterance starts the instant the previous ends.
    GAP = 0.0
    # Synthesize every line first, several at once (network-bound), then
    # place them in order — placement depends on the previous line's end.
    from concurrent.futures import ThreadPoolExecutor
    import threading
    done = [0]
    done_lock = threading.Lock()
    n = len(utterances)

    def _tts(i):
        synthesize(utterances[i]["text"], voice, tmpdir / f"u{i}.mp3")
        with done_lock:
            done[0] += 1
            if progress:
                progress(done[0], n)

    workers = max(1, int(os.environ.get("DUBLAB_TTS_WORKERS") or 8))
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(_tts, range(n)))   # list() re-raises the first error
        durs = list(ex.map(lambda i: probe_duration(tmpdir / f"u{i}.mp3"), range(n)))

    # placement (sequential: each start depends on where the previous ended)
    plan = []
    cues = []      # (start, end, text) of each spoken line, for captions
    for i, u in enumerate(utterances):
        tts_dur = durs[i]
        slot = max(u["end"] - u["start"], 0.5)
        ratio = tts_dur / slot
        atempo = 1.0 if ratio <= 1.0 else min(ratio, MAX_ATEMPO)
        fitted = tts_dur / atempo
        start = max(u["start"], cursor)
        plan.append((tmpdir / f"u{i}.mp3", atempo, start))
        cues.append((start, start + fitted, u["text"]))
        cursor = start + fitted + GAP

    # Channel outro ("like and follow…") in the same voice: after the last
    # line, ending right at the video end when there is room; if the speech
    # runs to the end it plays just after (players hold the last frame).
    if outro:
        op = tmpdir / "outro.mp3"
        synthesize(outro, voice, op)
        odur = probe_duration(op)
        ostart = max(cursor + 0.5, total_dur - odur - 0.3)
        plan.append((op, 1.0, ostart))
        cues.append((ostart, ostart + odur, outro))

    # Decode + time-fit every line to raw samples in parallel, then add them
    # into one in-memory track. (The old way wrote one wav per line padded
    # with silence from 0:00 — ~25 GB of temp files for a 24-minute video.)
    import numpy as np
    SR = 44100

    def _fit(item):
        path, atempo, start = item
        cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
        if atempo > 1.001:
            cmd += ["-af", f"atempo={atempo:.3f}"]
        cmd += ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
        p = subprocess.run(cmd, capture_output=True)
        if p.returncode != 0:
            raise RuntimeError((p.stderr.decode(errors="replace") or "ffmpeg failed")[-400:])
        return int(round(start * SR)), np.frombuffer(p.stdout, dtype=np.float32)

    with ThreadPoolExecutor(workers) as ex:
        pieces = list(ex.map(_fit, plan))
    end = max([total_dur * SR] + [s0 + len(x) for s0, x in pieces])
    track = np.zeros(int(end) + 1, dtype=np.float32)
    for s0, x in pieces:
        track[s0:s0 + len(x)] += x
    pcm = (np.clip(track, -1.0, 1.0) * 32767).astype(np.int16)

    import wave
    dub = tmpdir / "dub.wav"
    with wave.open(str(dub), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return dub, cues


# ---- Burmese captions ----
def _srt_time(t):
    ms = int(round(max(t, 0) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as fh:
        for n, (a, b, text) in enumerate(cues, 1):
            fh.write(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n\n")


_CAPTION_FONTS = [  # (path, face index) — Unicode Burmese, bold first
    ("/System/Library/Fonts/NotoSansMyanmar.ttc", 1),          # macOS: Bold
    ("/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansMyanmar-Bold.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Myanmar Sangam MN.ttc", 0),
]


def _caption_font(size):
    from PIL import ImageFont, features
    if not features.check("raqm"):
        raise RuntimeError("Burmese captions need libraqm (macOS: brew install libraqm)")
    forced = os.environ.get("DUBLAB_CAPTION_FONT")
    for path, idx in ([(forced, 0)] if forced else []) + _CAPTION_FONTS:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size, index=idx,
                                      layout_engine=ImageFont.Layout.RAQM)
    raise RuntimeError("no Burmese font found (set DUBLAB_CAPTION_FONT)")


# Burmese syllable start: a consonant/independent vowel not stacked under a
# virama and not killed by an asat — safe places to break a long word.
_MY_BREAK = re.compile(r"(?<!\u1039)(?=[\u1000-\u102A\u103F\u104C-\u104F](?![\u103A\u1039]))")


def _split_wide(draw, word, font, max_w):
    """Break one over-wide word at Burmese syllable starts (or characters)."""
    parts = [p for p in _MY_BREAK.split(word) if p] or list(word)
    if len(parts) == 1:
        parts = list(word)
    out, cur = [], ""
    for p in parts:
        if cur and draw.textlength(cur + p, font=font) > max_w:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    return out


def _wrap(draw, text, font, max_w):
    """Greedy word wrap on spaces (Burmese phrases are space-separated);
    words wider than a line are broken at syllable starts."""
    words = []
    for word in text.split():
        if draw.textlength(word, font=font) > max_w:
            words += _split_wide(draw, word, font, max_w)
        else:
            words.append(word)
    lines, cur = [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def render_captions(cues, video_w, video_h, out_dir, size_pct=5.5):
    """Draw each cue as a transparent PNG strip (white text, dark outline) and
    write an ffconcat list that shows them at their times.
    Returns (concat_list_path, strip_height)."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    fsize = max(14, int(video_h * size_pct / 100))
    font = _caption_font(fsize)
    stroke = max(2, fsize // 12)
    line_h = int(fsize * 1.75)                 # Burmese stacks marks above/below
    max_lines = 2
    strip_h = line_h * max_lines + stroke * 2
    max_w = int(video_w * 0.9)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    blank = out_dir / "blank.png"
    Image.new("RGBA", (video_w, strip_h), (0, 0, 0, 0)).save(blank)
    # A long line becomes several captions shown one after another, each at
    # most two lines, with the cue's time shared by text length.
    pages = []
    for a, b, text in cues:
        lines = _wrap(probe, text, font, max_w)
        groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        total = sum(len("".join(g)) for g in groups) or 1
        t0 = a
        for g in groups:
            t1 = t0 + (b - a) * len("".join(g)) / total
            pages.append((t0, t1, g))
            t0 = t1

    items, t = [], 0.0
    for n, (a, b, lines) in enumerate(pages):
        if b <= t:
            continue
        a = max(a, t)
        if a > t + 0.01:
            items.append((blank, a - t))
        img = Image.new("RGBA", (video_w, strip_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        y = strip_h - stroke - line_h * len(lines)   # bottom-aligned
        for ln in lines:
            w = d.textlength(ln, font=font)
            d.text(((video_w - w) / 2, y), ln, font=font, fill=(255, 255, 255, 255),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 230))
            y += line_h
        p = out_dir / f"c{n}.png"
        img.save(p)
        items.append((p, b - a))
        t = b
    items.append((blank, 1.0))
    lst = out_dir / "captions.txt"
    with open(lst, "w") as fh:
        fh.write("ffconcat version 1.0\n")
        for p, dur in items:
            fh.write(f"file '{p.name}'\nduration {dur:.3f}\n")
        fh.write(f"file '{items[-1][0].name}'\n")   # concat needs the last file twice
    return lst, strip_h


def cleanup_segments(job_dir):
    """Drop the per-utterance TTS intermediates once the mux is done.

    build_dub_track leaves u*.mp3 (raw TTS) and p*.wav (time-fitted
    segments) in the job dir; on a 3-minute video that is ~1GB of files
    that nothing serves afterwards — /r/<jid> only hands out dubbed.mp4.
    Set DUBLAB_KEEP_SEGMENTS=1 to keep them for debugging.
    """
    if os.environ.get("DUBLAB_KEEP_SEGMENTS"):
        return 0
    d = Path(job_dir)
    removed = 0
    import shutil as _sh
    _sh.rmtree(d / "caps", ignore_errors=True)
    for pat in ("u*.mp3", "p*.wav", "outro.mp3"):
        for f in d.glob(pat):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


WATERMARK_TEXT = os.environ.get("DUBLAB_WATERMARK", "made with DubLab")
_WM_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_has_drawtext = None


def _drawtext_ok():
    """Some ffmpeg builds (e.g. Homebrew without freetype) lack drawtext."""
    global _has_drawtext
    if _has_drawtext is None:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True)
        _has_drawtext = " drawtext " in p.stdout
        if not _has_drawtext:
            print("[mux] ffmpeg has no drawtext filter — watermark skipped")
    return _has_drawtext


# ---- background music: remove the original voices with Demucs ----
def demucs_available():
    import importlib.util
    return importlib.util.find_spec("demucs") is not None


def _demucs_devices():
    forced = os.environ.get("DUBLAB_DEMUCS_DEVICE")
    if forced:
        return [forced]
    import platform
    import shutil
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return ["mps", "cpu"]          # Apple GPU, ~4x faster than CPU
    if shutil.which("nvidia-smi"):
        return ["cuda", "cpu"]
    return ["cpu"]


def separate_music(src_video, job_dir):
    """Return a wav of the original audio with all voices removed
    (music + sound effects only), or None if Demucs isn't installed."""
    import sys
    if not demucs_available():
        return None
    d = Path(job_dir)
    wav = d / "orig_stereo.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src_video),
         "-vn", "-ac", "2", "-ar", "44100", str(wav)])
    err = None
    for dev in _demucs_devices():
        try:
            run([sys.executable, "-m", "demucs", "--two-stems=vocals",
                 "-n", "htdemucs", "-d", dev, "-o", str(d / "sep"), str(wav)])
            out = d / "sep" / "htdemucs" / "orig_stereo" / "no_vocals.wav"
            if out.exists():
                return out
        except RuntimeError as e:
            err = e
    raise RuntimeError(f"music separation failed: {err}")


def mux(src_video, dub_wav, out_mp4, bg_wav=None, bg_volume=0.8, logo=None,
        captions=None, blur=None):
    """Combine picture + dub track.

    bg_wav: music/effects with the original voices removed (separate_music).
    It goes under the dub at bg_volume and dips a little whenever the dub
    voice speaks (sidechain compressor) so the new voice stays clear.
    Without bg_wav the original audio is dropped entirely — the original
    voices are never mixed in.
    logo: optional dict(path=PNG, position="top-right"|"top-left"|
    "bottom-right"|"bottom-left", size=fraction of video width).
    captions: optional dict(list=ffconcat file from render_captions, height=strip px).
    blur: optional dict(bottom=fraction, height=fraction) — blurs a full-width
    band (to hide burned-in original captions); captions are centred on it.
    """
    # Burned-in credit watermark, bottom-right; scale guard keeps even dims
    # for yuv420p.
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
    watermark = bool(WATERMARK_TEXT and _drawtext_ok())
    if watermark:
        font = f"fontfile={_WM_FONT}:" if os.path.exists(_WM_FONT) else ""
        text = WATERMARK_TEXT.replace("\\", "").replace("'", "").replace(":", "\\:")
        vf += (f"drawtext={font}text='{text}':"
               "fontsize=h/40:fontcolor=white@0.72:"
               "shadowcolor=black@0.55:shadowx=1:shadowy=1:"
               "x=w-text_w-h/40:y=h-text_h-h/56,")
    vf += "format=yuv420p"
    if logo:
        vw = _video_width(src_video)
        lw = max(16, int(vw * float(logo.get("size", 0.12))) // 2 * 2)
        m = max(8, int(vw * 0.02))                 # margin from the edges
        x = f"{m}" if "left" in logo.get("position", "") else f"W-w-{m}"
        y = f"H-h-{m}" if "bottom" in logo.get("position", "") else f"{m}"
    bg_in = []
    if bg_wav and bg_volume > 0:
        bg_in = ["-i", str(bg_wav)]
        afc = ("[1:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[dub][sc];"
               f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={bg_volume:.2f}[bg];"
               "[bg][sc]sidechaincompress=threshold=0.02:ratio=4:attack=20:release=400[duck];"
               "[duck][dub]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[a]")
        amap = ["-map", "[a]"]
    else:
        afc, amap = None, ["-map", "1:a:0"]

    logo_in = ["-i", str(logo["path"])] if logo else []
    logo_idx = 2 + (1 if bg_in else 0)
    cap_in = ["-f", "concat", "-safe", "0", "-i", str(captions["list"])] if captions else []
    cap_idx = logo_idx + (1 if logo else 0)
    draw = bool(logo or captions or blur)

    def _cmd(copy_video, encoder="libx264"):
        if copy_video:   # nothing to draw: keep the original picture, no re-encode
            fc = ["-filter_complex", afc] if afc else []
            vid = ["-map", "0:v:0", "-c:v", "copy"]
        else:
            if draw:
                parts = [f"[0:v]{vf.replace(',format=yuv420p', '')}[s0]"]
                cur = "s0"
                if blur:
                    b0, bh = float(blur["bottom"]), float(blur["height"])
                    by = f"ih*{1 - b0 - bh:.4f}"
                    parts.append(f"[{cur}]split[bm][bb]")
                    parts.append(f"[bb]crop=iw:ih*{bh:.4f}:0:{by},boxblur=luma_radius=18:luma_power=3[bz]")
                    parts.append(f"[bm][bz]overlay=0:H*{1 - b0 - bh:.4f}[s1]")
                    cur = "s1"
                if logo:
                    parts.append(f"[{logo_idx}:v]scale={lw}:-1,format=rgba[lg]")
                    parts.append(f"[{cur}][lg]overlay={x}:{y}[s2]")
                    cur = "s2"
                if captions:
                    ch = int(captions["height"])
                    if blur:      # centre the text strip on the blurred band
                        cy = f"H*{1 - b0 - bh / 2:.4f}-{ch // 2}"
                    else:
                        cy = f"H-{ch}-H*0.05"
                    parts.append(f"[{cap_idx}:v]format=rgba[cp]")
                    parts.append(f"[{cur}][cp]overlay=0:{cy}:eof_action=pass[s3]")
                    cur = "s3"
                parts.append(f"[{cur}]format=yuv420p[v]")
                vfc = ";".join(parts)
            else:
                vfc = f"[0:v]{vf}[v]"
            fc = ["-filter_complex", vfc + (";" + afc if afc else "")]
            if encoder == "h264_videotoolbox":     # Apple hardware encoder: ~5x faster
                enc = ["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"]
            else:
                enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
            vid = ["-map", "[v]", *enc]
        return ["ffmpeg", "-y", "-v", "error", "-i", str(src_video), "-i", str(dub_wav),
                *bg_in, *logo_in, *cap_in, *fc, *vid, *amap, "-c:a", "aac", "-b:a", "128k",
                # No -shortest: the finish-the-sentence rule lets the tail of the
                # last utterance run a little past the video end; -shortest would
                # cut it off again.
                "-movflags", "+faststart", str(out_mp4)]

    if not watermark and not draw:
        try:
            run(_cmd(copy_video=True))
            return
        except RuntimeError as e:   # codec not allowed in MP4 etc. — re-encode
            print(f"[mux] stream copy failed ({e}); re-encoding")
    if _has_encoder("h264_videotoolbox"):
        try:
            run(_cmd(copy_video=False, encoder="h264_videotoolbox"))
            return
        except RuntimeError as e:
            print(f"[mux] hardware encoder failed ({e}); using libx264")
    run(_cmd(copy_video=False))


_encoders = None


def _has_encoder(name):
    global _encoders
    if _encoders is None:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True)
        _encoders = p.stdout
    return f" {name} " in _encoders


def _video_width(path):
    return video_size(path)[0]


def video_size(path):
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)])
    w, h = p.stdout.strip().split(",")[:2]
    return int(w) // 2 * 2, int(h) // 2 * 2      # mux scales to even dims


def normalize_logo(src, dst):
    """Convert an uploaded image to PNG (alpha kept), at most 1000 px wide."""
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-vf", "scale='min(1000,iw)':-1", "-frames:v", "1", "-pix_fmt", "rgba", str(dst)])


# ---- download from a YouTube / TikTok link (yt-dlp) ----
URL_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
             "music.youtube.com", "youtu.be",
             "tiktok.com", "www.tiktok.com", "m.tiktok.com",
             "vm.tiktok.com", "vt.tiktok.com"}


def is_supported_url(url):
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "").lower() in URL_HOSTS


def _ydl_opts(**extra):
    return {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3, **extra}


def probe_url(url):
    """Return (title, duration_seconds, is_live) without downloading."""
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    return (info.get("title") or "?", float(info.get("duration") or 0),
            bool(info.get("is_live")))


def download_url(url, job_dir, max_mb):
    """Download the video (≤720p) into job_dir as input.mp4; return its path."""
    import yt_dlp
    out = Path(job_dir) / "input.mp4"
    opts = _ydl_opts(
        format="bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        merge_output_format="mp4",
        outtmpl=str(Path(job_dir) / "input.%(ext)s"),
        max_filesize=max_mb * 1024 * 1024)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    if not out.exists():
        raise RuntimeError("video download failed")
    return out
