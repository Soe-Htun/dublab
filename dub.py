"""Dubbing pipeline: extract audio -> transcribe -> translate -> TTS -> mux."""
import os
import re
import subprocess
from pathlib import Path

SPEAK_SPEED = float(os.environ.get("DUBLAB_SPEAK_SPEED") or 1.15)   # every voice line is sped up this much (1.0 = TTS's own pace, felt slow)


def set_speak_speed(v):
    """The admin Settings page changes the voice speed (no restart needed)."""
    global SPEAK_SPEED
    SPEAK_SPEED = float(v)
    import gemini
    gemini.TTS_CPS = 15.0 * SPEAK_SPEED          # the recap length estimate follows
MAX_ATEMPO = 1.65  # never speed speech up more than this (1.5->1.8 on 2026-10-26 to fix ~1.7s drift, then 1.8->1.65 same night: 1.8 sounded too fast; 1.65 fits every ratio measured on the Oct 25 test video, 1.61-1.67, with ~0.04s total drift)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ffmpeg failed").strip()[-400:])
    return p


def run_progress(cmd, length, cb):
    """run() for a long ffmpeg encode, calling cb(fraction 0..1) as it goes."""
    import tempfile
    if not cb or not length:
        return run(cmd)
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    with tempfile.TemporaryFile("w+") as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        last = -1
        for line in p.stdout:
            if line.startswith("out_time_us="):
                try:
                    f = min(1.0, int(line.split("=", 1)[1]) / 1e6 / length)
                except ValueError:
                    continue
                if int(f * 100) != last:
                    last = int(f * 100)
                    try:
                        cb(f)
                    except Exception:  # noqa: BLE001 - progress must never stop the encode
                        pass
        p.wait()
        if p.returncode != 0:
            err.seek(0)
            raise RuntimeError((err.read() or "ffmpeg failed").strip()[-400:])
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


def extract_audio(src, dst, length=0, progress=None):
    run_progress(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                  "-vn", "-ac", "1", "-ar", "16000", str(dst)], length, progress)


from gemini_tts import QuotaExhausted
from gemini_tts import configure as _tts_configure
from gemini_tts import synthesize as _gemini_tts

# Edge-TTS voice name -> Gemini prebuilt voice (male/female pairs)
# 🎙️ the narrator of the commentary breaks: a third voice, apart from the characters'
# (Gemini TTS "Puck"; Edge has only two Burmese voices, so there it falls back to Thiha)
NARRATOR_VOICE = "my-MM-ThihaNeural#narrator"

GEMINI_VOICE_FOR = {    "my-MM-ThihaNeural#narrator": "Puck",   # narrator
    "my-MM-NilarNeural": "Aoede",    # female Burmese
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


def transcribe(wav, lang_hint=None, progress=None, model_name=None, title="", words=False):
    """Return ([{start, end, text}], language).

    lang_hint: 'en', 'my', 'zh'… or None to let whisper detect the
    language; the returned language is the hint or the detected code.
    model_name: 'base' (fast) or 'small' (more accurate); default from .env."""
    global _cpu_only
    name = model_name or default_whisper_model()
    try:
        return _transcribe(wav, lang_hint, progress, name, title, words)
    except Exception as e:  # noqa: BLE001 - GPU backends are experimental
        if _cpu_only:
            raise
        print(f"[whisper] GPU failed ({type(e).__name__}: {e}); retrying on CPU")
        _cpu_only = True
        _models.clear()
        return _transcribe(wav, lang_hint, progress, name, title, words)


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


MLX_REPOS = {"base": "mlx-community/whisper-base-mlx", "small": "mlx-community/whisper-small-mlx"}
MLX_CHUNK = 600.0     # seconds per piece (lets us report progress; mlx has no callback)
_mlx_broken = False


def _use_mlx():
    # opt-in only: on an M1 (8 GB) it ran at ~2x real time on a 2.5 h film (temperature
    # fallbacks on music, no VAD) vs ~15x for faster-whisper on the CPU (2026-11-17)
    if _mlx_broken or os.environ.get("DUBLAB_WHISPER_ENGINE", "").strip() != "mlx":
        return False
    import platform
    if not (platform.system() == "Darwin" and platform.machine() == "arm64"):
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _mlx_model_ready(repo, timeout=240):
    """Make sure the model is on disk. A download that stalls (it once hung
    at 3.8 MB for good) must not hang the job: give up and use the CPU engine."""
    from huggingface_hub import snapshot_download
    try:
        snapshot_download(repo, local_files_only=True)
        return
    except Exception:  # noqa: BLE001 - not downloaded yet
        pass
    print(f"[whisper] downloading {repo} (once)…")
    if not _in_thread(lambda: snapshot_download(repo, etag_timeout=30), timeout):
        raise TimeoutError(f"{repo} download took over {timeout}s")


def _transcribe_mlx(wav, lang_hint, progress, name):
    """Whisper on the Apple GPU (mlx-whisper): the CPU engine below can't use it.
    The audio goes in 10-minute pieces so progress can be shown."""
    import tempfile
    import mlx_whisper
    repo = MLX_REPOS.get(name, MLX_REPOS["small"])
    _mlx_model_ready(repo)
    total = probe_duration(wav)
    segs, lang, t0 = [], lang_hint, 0.0
    with tempfile.TemporaryDirectory() as tmp:
        while t0 < total - 0.5:
            piece = Path(tmp) / "piece.wav"
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t0:.2f}", "-t", f"{MLX_CHUNK:.0f}",
                 "-i", str(wav), "-ac", "1", "-ar", "16000", str(piece)])
            r = mlx_whisper.transcribe(str(piece), path_or_hf_repo=repo, language=lang,
                                       condition_on_previous_text=False,   # no runaway repeats
                                       no_speech_threshold=0.6, verbose=None)
            lang = lang or r.get("language")
            for sg in r.get("segments", []):
                text = (sg.get("text") or "").strip()
                if text and sg.get("no_speech_prob", 0) < 0.8:
                    segs.append({"start": t0 + float(sg["start"]), "end": t0 + float(sg["end"]),
                                 "text": text})
            t0 += MLX_CHUNK
            if progress:
                progress(min(t0 / total, 1.0))
    return segs, lang


def _collapse_repeats(text, keep=2):
    """'बजाँ बजाँ बजाँ बजाँ …' → 'बजाँ बजाँ': whisper sometimes gets stuck on one
    word; a line that is nothing but that is dropped."""
    words = text.split()
    out = []
    for w in words:
        if len(out) >= keep and all(x == w for x in out[-keep:]):
            continue
        out.append(w)
    if len(words) >= 6 and len(set(words)) <= 2:
        return ""
    return " ".join(out).strip()


def burmese_title(title):
    """The video's own title is mostly Burmese and doesn't say it's a subtitled /
    dubbed foreign film ("မြန်မာစာတန်းထိုး", "[DUB]"…)."""
    t = title or ""
    mm = sum(1 for c in t if "\u1000" <= c <= "\u109f")
    letters = sum(1 for c in t if c.isalpha())
    foreign = re.search(r"စာတန်းထိုး|ဘာသာပြန်|subtitle|\bsub\b|dub|translated|hindi|chinese|korean|thai|"
                        r"english|movie\s*recap", t, re.I)
    return letters > 0 and mm / letters > 0.4 and not foreign


def _vote_language(wav, name, spots=(0.15, 0.3, 0.45, 0.6, 0.75, 0.9), clip=30, title=""):
    """Whisper guesses the language from the first 30 s only — a Burmese ad or
    channel intro before an Indian film made it 'my'. Listen at several points
    through the video instead and add up the votes."""
    import tempfile
    from faster_whisper import decode_audio
    model = _get_model(name)
    total = probe_duration(wav)
    if total < clip * 4:
        return None                      # short clip: whisper's own guess is fine
    votes, my_score, heard = {}, [0.0], [0]
    with tempfile.TemporaryDirectory() as tmp:
        for f in spots:
            piece = Path(tmp) / "spot.wav"
            try:
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{total * f:.1f}", "-t", str(clip),
                     "-i", str(wav), "-ac", "1", "-ar", "16000", str(piece)])
                lang, prob, allp = model.detect_language(decode_audio(str(piece)), vad_filter=True)
            except Exception:  # noqa: BLE001 - e.g. a silent spot
                continue
            for l, q in allp[:5]:        # add up whole guesses, not just the winner (Burmese
                votes[l] = votes.get(l, 0.0) + q   # often comes second to Chinese)
            my_score[0] += dict(allp).get("my", 0.0)
            heard[0] += 1
    if not votes:
        return None
    best = max(votes, key=votes.get)
    # whisper hears little Burmese and often calls it Chinese: a Burmese-titled
    # video whose speech isn't clearly another language is Burmese
    conf = votes[best] / max(1, heard[0])
    if best != "my" and burmese_title(title) and (conf < 0.85 or my_score[0] / max(1, heard[0]) > 0.05):
        print(f"[whisper] Burmese title and unsure speech ({best} {conf:.2f}) → my")
        best = "my"
    print(f"[whisper] language votes: { {k: round(v, 2) for k, v in votes.items()} } → {best}")
    return best


def _transcribe(wav, lang_hint, progress, name, title="", words=False):
    global _mlx_broken
    if not lang_hint:
        try:
            lang_hint = _vote_language(wav, name, title=title)
        except Exception as e:  # noqa: BLE001 - fall back to whisper's own guess
            print(f"[whisper] language vote failed: {e}")
    if _use_mlx():
        try:
            out = _transcribe_mlx(wav, lang_hint, progress, name)
            print(f"[whisper] {name} on Apple GPU (mlx)")
            return out
        except Exception as e:  # noqa: BLE001 - fall back to the CPU engine
            print(f"[whisper] mlx failed ({type(e).__name__}: {e}); using faster-whisper")
            _mlx_broken = True
    model = _get_model(name)
    # beam 1 (greedy) is ~3x faster than the default 5 with near-identical text
    beam = int(os.environ.get("DUBLAB_WHISPER_BEAM") or 1)
    # temperature 0 only + no conditioning on the previous text (2026-11-17,
    # Jaat test clip): whisper's default retries at higher temperatures on noisy
    # film audio (music, songs) ran at 2.9x real time and wrote garbage; this ran
    # at 14x and wrote clean sentences. The one downside, a stuck repeated word,
    # is cleaned up by _collapse_repeats.
    segments, info = model.transcribe(
        str(wav), language=lang_hint, vad_filter=True, beam_size=beam,
        temperature=0.0, condition_on_previous_text=False, word_timestamps=words,
        vad_parameters=dict(min_silence_duration_ms=400))
    segs = []
    for s in segments:  # lazy generator: decoding happens while we iterate
        if words and s.words:
            # captions: one line per stretch of talking — whisper joins lines across
            # a long silence ("Look at those papayas." … 8 s … "What are you doing?")
            run = []
            for w in list(s.words) + [None]:
                if run and (w is None or w.start - run[-1].end > 0.7):
                    text = _collapse_repeats("".join(x.word for x in run))
                    if text:
                        segs.append({"start": float(run[0].start), "end": float(run[-1].end), "text": text})
                    run = []
                if w is not None:
                    run.append(w)
        else:
            text = _collapse_repeats(s.text or "")
            if text:
                segs.append({"start": float(s.start), "end": float(s.end), "text": text})
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


def line_genders(wav, utterances, min_frames=12):
    """👥 Who speaks each line, by voice pitch: "male" / "female" per utterance, None
    when too little clear voice (the line then keeps the main voice). A woman's line
    gets the female Burmese voice, a man's the male one — not one voice for everybody."""
    out = []
    for u in utterances:
        try:
            g, f0 = detect_gender(wav, [u], max_speech=12.0)
            out.append(g if f0 else None)
        except Exception:  # noqa: BLE001
            out.append(None)
    # one odd line inside a run of the other gender is usually a pitch mistake: smooth it
    for i in range(1, len(out) - 1):
        if out[i] and out[i - 1] and out[i - 1] == out[i + 1] != out[i]:
            gap = utterances[i]["start"] - utterances[i - 1]["end"]
            if gap < 0.4 and utterances[i]["end"] - utterances[i]["start"] < 1.5:
                out[i] = out[i - 1]
    return out


def comment_gaps(utterances, total, every=150.0, min_gap=3.5, first=45.0):
    """🎙️ Where the narrator can speak without talking over anyone: the longest pause
    between lines in each `every`-second stretch (at least `min_gap` s long).
    Returns [(start, end, index of the line before)]."""
    gaps = []
    for i in range(len(utterances) - 1):
        a, b = utterances[i]["end"] + 0.25, utterances[i + 1]["start"] - 0.25
        if b - a >= min_gap and a >= first:
            gaps.append((a, b, i))
    out, t = [], first
    while t < total - 30:
        win = [g for g in gaps if t <= g[0] < t + every]
        if win:
            out.append(max(win, key=lambda g: g[1] - g[0]))
        t += every
    return out


def mix_comments(base_audio, clips, out, duck=0.25):
    """Mix narrator clips [(path, start)] over a sound track, the track turned down
    to `duck` while each one speaks (0.15 s fades). Returns out."""
    if not clips:
        return base_audio
    ins = ["-i", str(base_audio)]
    for p, _ in clips:
        ins += ["-i", str(p)]
    wins = []
    for p, st in clips:
        d = probe_duration(p)
        wins.append(f"between(t,{st - 0.15:.3f},{st + d + 0.15:.3f})")
    fc = [f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,"
          f"volume='if({'+'.join(wins)},{duck},1)':eval=frame[b]"]
    labels = ["[b]"]
    for k, (_, st) in enumerate(clips, 1):
        ms = int(st * 1000)
        fc.append(f"[{k}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={ms}|{ms},volume=1.6[c{k}]")
        labels.append(f"[c{k}]")
    fc.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[a]")
    run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(fc), "-map", "[a]",
         "-c:a", "aac", "-b:a", "160k", str(out)])
    return out


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
    voice = voice.split("#")[0]          # "…#narrator": Edge has no third Burmese voice
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


TTS_WALL = int(os.environ.get("DUBLAB_TTS_TIMEOUT") or 150)   # seconds one line may take, all retries included


def _in_thread(fn, timeout):
    """Run fn() in a daemon thread; True if it finished in time (re-raises its
    error). A network call that hangs is left behind instead of hanging the job."""
    import threading
    box = {}

    def go():
        try:
            fn()
            box["ok"] = True
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if "err" in box:
        raise box["err"]
    return bool(box.get("ok"))


NET_RETRIES = 8          # a Wi-Fi drop must not kill a 2-hour job: wait for the internet
NET_WAIT = 20            # seconds between tries (8 x 20 s ≈ 3 minutes in all)


def _net_down(e):
    t = f"{type(e).__name__} {e}".lower()
    return any(k in t for k in ("dns", "nodename", "servname", "name resolution", "connect",
                                "network is unreachable", "timed out", "timeout", "connection"))


def synthesize(text, voice, out_path):
    """TTS one utterance at SPEAK_SPEED (the TTS's own pace is a bit slow).
    When every TTS service fails because the internet dropped, wait and try again."""
    import time
    for attempt in range(NET_RETRIES + 1):
        try:
            _synthesize_raw(text, voice, out_path)
            break
        except QuotaExhausted:
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == NET_RETRIES or not _net_down(e):
                raise
            print(f"[tts] internet looks down ({str(e)[:80]}) — retrying in {NET_WAIT}s")
            time.sleep(NET_WAIT)
    if abs(SPEAK_SPEED - 1.0) < 0.01:
        return
    out_path = Path(out_path)
    tmp = out_path.with_name(f"{out_path.stem}.fast{out_path.suffix}")
    try:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(out_path),
             "-af", f"atempo={SPEAK_SPEED:.3f}", str(tmp)])
        tmp.replace(out_path)
    except Exception as e:  # noqa: BLE001 - normal speed is still fine
        print(f"[tts] speed-up skipped: {e}")


def _synthesize_raw(text, voice, out_path):
    """TTS one utterance, never hanging: the normal chain gets TTS_WALL
    seconds, then Edge alone, then gTTS. Each try writes its own temp file,
    so a late, abandoned try can't overwrite the result."""
    out_path = Path(out_path)
    tries = [("main", lambda o: _synthesize_once(text, voice, o), TTS_WALL),
             ("edge", lambda o: _synth_edge(text, voice, o), 75)]
    err = None
    for n, (name, fn, limit) in enumerate(tries):
        tmp = out_path.with_name(f"{out_path.stem}.try{n}{out_path.suffix}")
        try:
            if _in_thread(lambda: fn(tmp), limit) and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(out_path)
                return
            print(f"[tts] {name} took over {limit}s — trying the next way")
        except QuotaExhausted as e:
            err = e
        except Exception as e:  # noqa: BLE001
            err = e
    from gtts import gTTS                       # last resort: always answers quickly
    lang = "my" if voice.startswith("my-MM") else "en"
    try:
        gTTS(text, lang=lang).save(str(out_path))
    except Exception:
        raise err or RuntimeError("TTS timed out")


def _synthesize_once(text, voice, out_path):
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


def build_dub_track(utterances, voice, tmpdir, total_dur, progress=None, outro=None,
                    intro=None):
    """TTS each utterance, time-fit into its slot, mix into one dub track.

    intro: spoken before the video starts. Everything after it moves later by
    `lead` seconds (mux(lead=...) holds the first frame that long).
    Returns (dub_wav, cues, lead).

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

    def _path(i):           # recap mode hands in narration it already voiced
        return Path(utterances[i].get("audio") or tmpdir / f"u{i}.mp3")

    def _tts(i):
        out = tmpdir / f"u{i}.mp3"
        if not utterances[i].get("audio") and not (out.exists() and out.stat().st_size > 0):
            synthesize(utterances[i]["text"], utterances[i].get("voice") or voice, out)   # (▶️ resume keeps voiced lines)
        with done_lock:
            done[0] += 1
            if progress:
                progress(done[0], n)

    workers = max(1, int(os.environ.get("DUBLAB_TTS_WORKERS") or 8))
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(_tts, range(n)))   # list() re-raises the first error
        durs = list(ex.map(lambda i: probe_duration(_path(i)), range(n)))

    plan = []
    cues = []      # (start, end, text) of each spoken line, for captions
    lead = 0.0
    if intro:      # story hook over the held first frame, then the video starts
        ip = tmpdir / "intro.mp3"
        synthesize(intro, voice, ip)
        idur = probe_duration(ip)
        plan.append((ip, 1.0, 0.3))
        cues.append((0.3, 0.3 + idur, intro))
        lead = round(idur + 0.8, 3)
        total_dur += lead
        cursor = lead

    # placement (sequential: each start depends on where the previous ended)
    for i, u in enumerate(utterances):
        tts_dur = durs[i]
        slot = max(u["end"] - u["start"], 0.5)
        ratio = tts_dur / slot
        # lines are already at SPEAK_SPEED: cap the extra so the total stays ≤ MAX_ATEMPO
        atempo = 1.0 if ratio <= 1.0 else min(ratio, max(1.0, MAX_ATEMPO / SPEAK_SPEED))
        fitted = tts_dur / atempo
        start = max(u["start"] + lead, cursor)
        plan.append((_path(i), atempo, start))
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
    return dub, cues, lead


# ---- Burmese captions ----
def _srt_time(t):
    ms = int(round(max(t, 0) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def speech_regions(wav, min_silence=0.25):
    """[(start, end)] seconds where someone is actually talking (Silero VAD,
    the same one whisper uses) — to keep captions off the silent stretches."""
    import wave
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=int(min_silence * 1000),
                                                 speech_pad_ms=120))
    return [(t["start"] / sr, t["end"] / sr) for t in ts]


def fit_cues_to_speech(cues, regions, min_dur=0.8, max_dur=7.0):
    """Whisper's lines often start early and run on through the silence after
    them (a 2-word line shown for 10 s). Each caption is trimmed to the talking
    inside its own time span; a line with no talking in it keeps its span, and
    no caption stays up longer than max_dur."""
    out = []
    for a, b, text in cues:
        inside = [(max(a, s), min(b, e)) for s, e in regions if e > a and s < b]
        # talking stretches inside this line, joined where the pause is short
        runs = []
        for s, e in inside:
            if runs and s - runs[-1][1] < 1.2:
                runs[-1] = (runs[-1][0], e)
            else:
                runs.append((s, e))
        words = text.split()
        if len(runs) > 1 and len(words) >= len(runs):
            # one line over a long silence: its words shared out over the talking
            # stretches by length, so nothing shows while nobody speaks
            total = sum(e - s for s, e in runs)
            k = 0
            for i, (s, e) in enumerate(runs):
                take = len(words) - k if i == len(runs) - 1 else max(1, round(len(words) * (e - s) / total))
                take = min(take, len(words) - k - (len(runs) - 1 - i))
                out.append((s, min(max(e, s + min_dur), s + max_dur), " ".join(words[k:k + take])))
                k += take
            continue
        if runs:
            a, b = runs[0][0], runs[-1][1]
        b = min(max(b, a + min_dur), a + max_dur)
        out.append((a, b, text))
    for k in range(len(out) - 1):              # never overlap the next line
        a, b, t = out[k]
        if b > out[k + 1][0]:
            out[k] = (a, max(a + 0.3, out[k + 1][0] - 0.05), t)
    return out


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


_LATIN_FONTS = [  # Noto Sans Myanmar has no Latin letters: English words use one of these
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


class _MixFont:
    """A Burmese font plus a Latin one: text is drawn in runs, so English
    words ("like and follow", channel names) never come out as □□□ boxes."""
    def __init__(self, my, lat):
        self.my, self.lat = my, lat or my
        self.asc = my.getmetrics()[0]


_LATIN_RUN = re.compile(r"[\x21-\x7E](?:[\x20-\x7E]*[\x21-\x7E])?")


def _runs(text, f):
    pos = 0
    for m in _LATIN_RUN.finditer(text):
        if m.start() > pos:
            yield text[pos:m.start()], f.my
        yield m.group(0), f.lat
        pos = m.end()
    if pos < len(text):
        yield text[pos:], f.my


def _tlen(d, text, f):
    return sum(d.textlength(seg, font=ft) for seg, ft in _runs(text, f))


def _tbbox(d, xy, text, f, **kw):
    x, base = xy[0], xy[1] + f.asc
    box = None
    for seg, ft in _runs(text, f):
        b = d.textbbox((x, base), seg, font=ft, anchor="ls", **kw)
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
        x += d.textlength(seg, font=ft)
    return box or (xy[0], xy[1], xy[0], xy[1])


def _tdraw(d, xy, text, f, **kw):
    x, base = xy[0], xy[1] + f.asc
    for seg, ft in _runs(text, f):
        d.text((x, base), seg, font=ft, anchor="ls", **kw)
        x += d.textlength(seg, font=ft)


def _caption_font(size):
    from PIL import ImageFont, features
    if not features.check("raqm"):
        raise RuntimeError("Burmese captions need libraqm (macOS: brew install libraqm)")
    forced = os.environ.get("DUBLAB_CAPTION_FONT")
    for path, idx in ([(forced, 0)] if forced else []) + _CAPTION_FONTS:
        if path and os.path.exists(path):
            my = ImageFont.truetype(path, size, index=idx, layout_engine=ImageFont.Layout.RAQM)
            lat = next((ImageFont.truetype(lp, int(size * 0.92), layout_engine=ImageFont.Layout.RAQM)
                        for lp in _LATIN_FONTS if os.path.exists(lp)), None)
            return _MixFont(my, lat)
    raise RuntimeError("no Burmese font found (set DUBLAB_CAPTION_FONT)")


# Burmese syllable start: a consonant/independent vowel not stacked under a
# virama and not killed by an asat — safe places to break a long word.
_MY_BREAK = re.compile(r"(?<!\u1039)(?=[\u1000-\u102A\u103F\u104C-\u104F]"
                       r"(?![\u102B-\u1038\u103B-\u103E]*[\u103A\u1039]))")   # not "ည့်": a killed consonant


_NON_MY = re.compile(r"[^\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF]+")


def _split_wide(draw, word, font, max_w):
    """Break one over-wide word at Burmese syllable starts — never inside an English
    word or a number ("important" stays whole, it moves to the next line) and never
    between a consonant and its marks (ေ ် ့ ျ… stay with their syllable)."""
    parts = []
    pos = 0
    for m in _NON_MY.finditer(word):                     # Latin / digits / punctuation: one piece
        if m.start() > pos:
            parts += [p for p in _MY_BREAK.split(word[pos:m.start()]) if p]
        if parts and not re.match(r"[A-Za-z0-9]", m.group(0)):
            parts[-1] += m.group(0)                      # "…တယ်။" / "…," stays on its line
        else:
            parts.append(m.group(0))
        pos = m.end()
    if pos < len(word):
        parts += [p for p in _MY_BREAK.split(word[pos:]) if p]
    parts = parts or [word]
    for k in range(len(parts) - 2, -1, -1):     # a lone "အ" is a prefix: keep it with what follows
        if parts[k] == "အ":
            parts[k:k + 2] = [parts[k] + parts[k + 1]]
    # a piece starting with a mark (ေ ် ့ ာ …) belongs to the one before
    k = 1
    while k < len(parts):
        if re.match("[\u102B-\u103E\u1056-\u1059\u105E-\u1060\u1062-\u1064\u1067-\u106D\u1071-\u1074\u1082-\u108D]", parts[k]):
            parts[k - 1:k + 1] = [parts[k - 1] + parts[k]]
        else:
            k += 1
    if len(parts) == 1 and _tlen(draw, word, font) > max_w and not re.search("[\u1000-\u109F]", word):
        parts = list(word)                      # one huge English word: no other way
    out, cur = [], ""
    for p in parts:
        if cur and _tlen(draw, cur + p, font) > max_w:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    return out


def _wrap(draw, text, font, max_w):
    """Greedy word wrap on spaces (Burmese phrases are space-separated);
    words wider than a line are broken at syllable starts. Two lines are
    evened out so line 2 is never a lone syllable."""
    toks = []                                   # (piece, joins the previous piece with a space?)
    for word in text.split():
        parts = _split_wide(draw, word, font, max_w) if _tlen(draw, word, font) > max_w else [word]
        toks += [(p, k == 0) for k, p in enumerate(parts)]

    def join(ts):
        return "".join((" " if sp and i else "") + p for i, (p, sp) in enumerate(ts))

    lines, cur = [], []
    for t in toks:
        if cur and _tlen(draw, join(cur + [t]), font) > max_w:
            lines.append(cur)
            cur = [t]
        else:
            cur.append(t)
    if cur:
        lines.append(cur)
    if len(lines) == 2:
        flat = lines[0] + lines[1]
        best = min(range(1, len(flat)), key=lambda k: max(_tlen(draw, join(flat[:k]), font),
                                                          _tlen(draw, join(flat[k:]), font)))
        if max(_tlen(draw, join(flat[:best]), font), _tlen(draw, join(flat[best:]), font)) <= max_w:
            lines = [flat[:best], flat[best:]]
    return [join(ln) for ln in lines] or [""]


def render_captions(cues, video_w, video_h, out_dir, size_pct=5.5, box=True):
    """Draw each cue as a transparent PNG strip (white text on a translucent
    dark box, like YouTube captions — or, box=False, on the blurred band:
    no box, a thick dark outline and a soft shadow instead) and
    write an ffconcat list that shows them at their times.
    Returns (concat_list_path, strip_height)."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    # % of the short side: same as height for landscape, and keeps portrait
    # (TikTok 1080x1920) captions from coming out twice as big
    fsize = max(14, int(min(video_w, video_h) * size_pct / 100))
    font = _caption_font(fsize)
    stroke = max(1, fsize // 24)               # thin edge; the box does the work
    line_h = int(fsize * 1.75)                 # Burmese stacks marks above/below
    max_lines = 2
    pad_x, pad_y = int(fsize * 0.6), int(fsize * 0.3)
    strip_h = line_h * max_lines + pad_y * 4
    max_w = int(video_w * 0.9) - pad_x * 2
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
        y0 = strip_h - pad_y - line_h * len(lines)   # bottom-aligned
        placed, y = [], y0
        for ln in lines:
            w = _tlen(d, ln, font)
            placed.append(((video_w - w) / 2, y, ln))
            y += line_h
        # translucent box around the real ink of the text block
        boxes = [_tbbox(d, (x, yy), ln, font) for x, yy, ln in placed]
        top = min(b[1] for b in boxes) - pad_y
        bot = max(b[3] for b in boxes) + pad_y
        # boxed: sit on the strip's bottom edge; on the blur: centred in the strip
        dy = (strip_h - 1 - bot) if box else ((strip_h - (bot - top)) / 2 - top)
        placed = [(x, yy + dy, ln) for x, yy, ln in placed]
        top, bot = top + dy, bot + dy
        left = min(b[0] for b in boxes) - pad_x
        right = max(b[2] for b in boxes) + pad_x
        if box:
            bx = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(bx).rounded_rectangle(
                (max(0, left), max(0, top), min(video_w, right), min(strip_h, bot)),
                radius=int(fsize * 0.25), fill=(8, 8, 8, 185))
            img = Image.alpha_composite(img, bx)
            sw = stroke
        else:                                 # on the blur: outline + shadow keep it readable
            from PIL import ImageFilter
            sw = max(2, fsize // 11)
            sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
            sd = ImageDraw.Draw(sh)
            for x, yy, ln in placed:
                _tdraw(sd, (x + sw, yy + sw), ln, font, fill=(0, 0, 0, 200),
                       stroke_width=sw * 2, stroke_fill=(0, 0, 0, 200))
            img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(sw)))
        d = ImageDraw.Draw(img)
        for x, yy, ln in placed:
            _tdraw(d, (x, yy), ln, font, fill=(255, 255, 255, 255),
                   stroke_width=sw, stroke_fill=(0, 0, 0, 230))
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
    for pat in ("u*.mp3", "p*.wav", "outro.mp3", "intro.mp3"):
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


def separate_vocals(src_video, job_dir, progress=None):
    """The original audio with its MUSIC removed — voices only (Demucs), so a
    copyrighted song under the dialogue is not heard any more. None without Demucs."""
    out = separate_music(src_video, job_dir, progress=progress)
    if out is None:
        return None
    voc = Path(out).with_name("vocals.wav")
    return voc if voc.exists() else None


def separate_music(src_video, job_dir, chunk=300.0, progress=None):
    """Return a wav of the original audio with all voices removed
    (music + sound effects only), or None if Demucs isn't installed. The
    voices-only track is left next to it as vocals.wav.
    Long audio is done in `chunk`-second pieces: a 37-min film in one go needs
    ~5 GB of memory, so on an 8 GB Mac the GPU run failed and the CPU one crawled."""
    import sys
    import shutil as _sh
    if not demucs_available():
        return None
    d = Path(job_dir)
    wav = d / "orig_stereo.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src_video),
         "-vn", "-ac", "2", "-ar", "44100", str(wav)])
    total = probe_duration(wav)
    sep = d / "sep"
    _sh.rmtree(sep, ignore_errors=True)
    pieces = d / "sep_in"
    pieces.mkdir(exist_ok=True)
    n = max(1, int(-(-total // chunk)))
    names = []
    for i in range(n):
        p = pieces / f"c{i:03d}.wav"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{i * chunk:.3f}", "-t", f"{chunk:.3f}", "-i", str(wav),
             "-c:a", "pcm_s16le", str(p)])
        names.append(p)
    devices = _demucs_devices()
    for i, p in enumerate(names):
        err = None
        for dev in list(devices):
            try:
                run([sys.executable, "-m", "demucs", "--two-stems=vocals",
                     "-n", "htdemucs", "-d", dev, "-o", str(sep), str(p)])
                if (sep / "htdemucs" / p.stem / "no_vocals.wav").exists():
                    err = None
                    break
                err = RuntimeError("no output")
            except RuntimeError as e:
                err = e
            print(f"[demucs] {dev} failed on piece {i + 1}/{n}: {str(err)[-300:]}")
            if dev != devices[-1]:
                devices.remove(dev)        # don't try the failing device on every piece
        if err:
            raise RuntimeError(f"music separation failed: {err}")
        if progress:
            progress((i + 1) / n)
    out_dir = sep / "htdemucs" / "orig_stereo"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem in ("no_vocals", "vocals"):
        lst = pieces / f"{stem}.txt"
        lst.write_text("".join(f"file '{(sep / 'htdemucs' / p.stem / (stem + '.wav')).resolve()}'\n" for p in names))
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:a", "pcm_s16le", str(out_dir / f"{stem}.wav")])
    for p in names:
        _sh.rmtree(sep / "htdemucs" / p.stem, ignore_errors=True)
    _sh.rmtree(pieces, ignore_errors=True)
    return out_dir / "no_vocals.wav"


FRAME_W, FRAME_H = 1080, 1920      # TikTok / Shorts frame for landscape films


def frame_layout(vw, vh, blur=None):
    """Where a landscape picture goes in the 9:16 frame: full width, a little
    above the middle (TikTok's own buttons and text cover the bottom).
    blur: the old captions' band — when it is at the bottom, that strip is cut
    off the picture instead of blurred, and our captions go below the picture."""
    keep = 1.0
    if blur:
        b0, bh = float(blur["bottom"]), float(blur["height"])
        if b0 < 0.15 and 1 - b0 - bh >= 0.5:
            keep = 1 - b0 - bh
    hv = int(round(FRAME_W * vh * keep / vw / 2)) * 2
    vy = max(480, int(940 - hv / 2)) // 2 * 2       # room above for the tag + title, below TikTok's top bar
    # captions are drawn for the picture's full size (not the tall frame), so
    # they stay in proportion to it; cap_w/cap_h go to render_captions
    full = int(round(FRAME_W * vh / vw))
    return {"w": FRAME_W, "h": FRAME_H, "vh": hv, "vy": vy, "keep": keep,
            "cap_w": FRAME_W, "cap_h": max(full, FRAME_W * 9 // 16)}


def frame_caption_y(frame, strip_h, blur=None):
    """Caption strip y in the 9:16 frame: on the blurred band of the picture
    (over the old captions), like the normal video — or near the picture's
    bottom when nothing is blurred."""
    vy, hv = frame["vy"], frame["vh"]
    if frame.get("keep", 1.0) < 1.0:     # old captions cut off: ours go just below the picture
        return int(vy + hv + 6)
    if blur:      # centred on the blurred band (the text is centred in its strip)
        b0, bh = float(blur["bottom"]), float(blur["height"])
        return int(vy + hv * (1 - b0 - bh / 2) - strip_h / 2)
    return int(vy + hv * 0.97 - strip_h)


def _recap_tag(img, cx, cy, text=None):
    """A small pill at the top of the TikTok frame saying this is a film recap —
    so neither viewers nor TikTok's checks take the story for real news."""
    from PIL import Image, ImageDraw, ImageFont
    text = text or "MOVIE RECAP  |  ရုပ်ရှင်ဇာတ်လမ်း"
    try:
        f = _caption_font(34)
    except Exception:  # noqa: BLE001 - no Burmese shaping here: English only
        text, f = ("MOVIE RECAP  |  FILM STORY" if "RECAP" in text else "MOVIE  |  BURMESE SUB"), None
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if f is None:
        lat = next((p for p in _LATIN_FONTS if os.path.exists(p)), None)
        font = ImageFont.truetype(lat, 32) if lat else ImageFont.load_default()
        w, h = d.textlength(text, font=font), 32
    else:
        w, h = sum(d.textlength(seg, font=ft) for seg, ft in _runs(text, f)), f.asc
    pad_x, pad_y = 28, 14
    d.rounded_rectangle((cx - w / 2 - pad_x, cy - h / 2 - pad_y, cx + w / 2 + pad_x, cy + h / 2 + pad_y + 6),
                        radius=(h + 2 * pad_y) // 2, fill=(255, 77, 109, 235))
    if f is None:
        d.text((cx, cy + 3), text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    else:
        _tdraw(d, (cx - w / 2, cy - h / 2 - 2), text, f, fill=(255, 255, 255, 255))
    img.alpha_composite(layer)


def render_frames(title, windows, out_dir, layout, logo=None, extra=30.0, tag=None, episode=None):
    """One 1080x1920 frame per part (dark background, channel logo, gold title
    "title - N" like the covers), with a see-through window where the picture goes.
    windows: [(start, end)] of each part on the video's timeline.
    Returns an ffconcat list that shows each part's frame during that part."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    W, H, vy, hv = layout["w"], layout["h"], layout["vy"], layout["vh"]
    n = len(windows)
    base = Image.new("RGBA", (W, H))
    px = base.load()
    for y in range(H):                       # deep navy → plum, top to bottom
        f = y / H
        c = (int(10 + 18 * f), int(9 + 4 * f), int(22 + 16 * f), 255)
        for x in range(W):
            px[x, y] = c
    top_y = 60          # the channel logo sits on the picture (top-right), not up here
    # below TikTok's own top bar (status bar + search ≈ the top 12% of the screen)
    tag_cy = int(H * 0.145)
    _recap_tag(base, W // 2, tag_cy, tag)  # "MOVIE RECAP · ရုပ်ရှင်ဇာတ်လမ်း": plainly a film story, not news
    top_y = tag_cy + 45
    files = []
    for i in range(1, n + 1):
        img = base.copy().convert("RGB")
        title_cy = (top_y + vy - 30) // 2
        if title:                            # "Jaat - 1", "Jaat - 2"… like the covers
            _gold_title(img, title, int(W * 0.9), W // 2, title_cy, 2, 92, part_suffix(i, n, episode))
        img = img.convert("RGBA")
        hole = Image.new("RGBA", (W, hv), (0, 0, 0, 0))
        img.paste(hole, (0, vy))             # the picture shows through here
        f = out_dir / f"frame{i}.png"
        img.save(f)
        files.append(f)
    lst = out_dir / "frames.txt"
    with open(lst, "w") as fh:
        fh.write("ffconcat version 1.0\n")
        for k, (f, (a, b)) in enumerate(zip(files, windows)):
            dur = (b - a) + (extra if k == n - 1 else 0.0)
            fh.write(f"file '{f.name}'\nduration {max(dur, 0.1):.3f}\n")
        fh.write(f"file '{files[-1].name}'\n")
    return lst


def _mosaic_parts(censor, w, h, cur, lead=0.0):
    """ffmpeg filter steps that pixelate each (start, end, x0, y0, x1, y1) box of the
    picture (w x h) while it shows. Returns (steps, label of the result)."""
    parts = []
    w, h = w // 2 * 2, h // 2 * 2
    for k, (a, b, x0, y0, x1, y1) in enumerate(censor):
        bx, by = int(w * x0) // 2 * 2, int(h * y0) // 2 * 2
        bw = min(max(16, int(w * (x1 - x0)) // 2 * 2), w - bx)
        bh = min(max(16, int(h * (y1 - y0)) // 2 * 2), h - by)
        when = f"between(t,{a + lead:.2f},{b + lead:.2f})"
        parts.append(f"[{cur}]split[cm{k}][cc{k}];"
                     f"[cc{k}]crop={bw}:{bh}:{bx}:{by},"          # mosaic, like TV censoring
                     f"scale='max(2,trunc(iw/24))':'max(2,trunc(ih/24))',"
                     f"scale={bw}:{bh}:flags=neighbor[cb{k}];"
                     f"[cm{k}][cb{k}]overlay={bx}:{by}:enable='{when}'[cz{k}]")
        cur = f"cz{k}"
    return parts, cur


def cut_ranges(src, out, cuts, progress=None):
    """The video without the given (start, end) ranges (picture and sound)."""
    total = probe_duration(src)
    keep, t = [], 0.0
    for a, b in sorted(cuts):
        if a > t + 0.05:
            keep.append((t, a))
        t = max(t, b)
    if t < total - 0.05:
        keep.append((t, total))
    if not keep:
        raise RuntimeError("nothing would be left of the video")
    fc = []
    for i, (a, b) in enumerate(keep):
        fc.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                  f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=1[v][a]")
    enc = ["-c:v", "h264_videotoolbox", "-b:v", "3500k"] if _has_encoder("h264_videotoolbox") else ["-c:v", "libx264", "-crf", "21"]
    run_progress(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", ";".join(fc),
                  "-map", "[v]", "-map", "[a]", *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                  "-movflags", "+faststart", str(out)], sum(b - a for a, b in keep), progress)


def edit_video(src, out, censor=None, hide=None, overlays=None, progress=None):
    """A finished video, edited in one pass (sound untouched): a mosaic over each
    (start, end, box), logos erased (delogo) in each (x0, y0, x1, y1) box, and PNG
    overlays [(png, x, y, enable expression or None)]."""
    w, h = video_size(src)
    w2, h2 = w // 2 * 2, h // 2 * 2
    steps, cur = [], "0:v"
    if censor:
        st, cur = _mosaic_parts(censor, w, h, cur)
        steps += st
    if hide:
        dl = []
        for x0, y0, x1, y1 in hide:
            dx, dy = max(1, int(w2 * x0)), max(1, int(h2 * y0))
            dw, dh = min(int(w2 * x1), w2 - 2) - dx, min(int(h2 * y1), h2 - 2) - dy
            if dw > 4 and dh > 4:
                dl.append(f"delogo=x={dx}:y={dy}:w={dw}:h={dh}")
        if dl:
            steps.append(f"[{cur}]{','.join(dl)}[dl]")
            cur = "dl"
    inputs = []
    for k, (png, x, y, when) in enumerate(overlays or []):
        inputs += ["-i", str(png)]
        steps.append(f"[{k + 1}:v]format=rgba[ov{k}];[{cur}][ov{k}]overlay={x}:{y}"
                     + (f":enable='{when}'" if when else "") + f"[o{k}]")
        cur = f"o{k}"
    if not steps:
        import shutil
        shutil.copy2(src, out)
        return
    enc = ["-c:v", "h264_videotoolbox", "-b:v", "3500k"] if _has_encoder("h264_videotoolbox") else ["-c:v", "libx264", "-crf", "21"]
    run_progress(["ffmpeg", "-y", "-v", "error", "-i", str(src), *inputs, "-filter_complex", ";".join(steps),
                  "-map", f"[{cur}]", "-map", "0:a?", *enc, "-pix_fmt", "yuv420p", "-c:a", "copy",
                  "-movflags", "+faststart", str(out)], probe_duration(src), progress)


def fiction_notice(video_w, video_h, out_dir):
    """PNGs for a misinformation fix: a small banner for the whole video and a
    larger card for its first seconds, both saying it is a recap of a fictional film.
    Returns [(png, x, y, enable)] for edit_video."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(exist_ok=True)
    f_small = _caption_font(max(22, int(video_w * 0.032)))
    f_big = _caption_font(max(28, int(video_w * 0.045)))

    def pill(text, font, pad, fill):
        tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        tw = sum(tmp.textlength(s, font=ft) for s, ft in _runs(text, font))
        th = font.asc + int(font.asc * 0.45)
        im = Image.new("RGBA", (int(tw + 2 * pad), int(th + pad)), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=im.height // 2, fill=fill)
        _tdraw(d, (pad, pad * 0.35), text, font, fill=(255, 255, 255, 255))
        return im
    banner = pill("FICTIONAL FILM RECAP  |  ဇာတ်ကား ဇာတ်လမ်းသာ", f_small, int(video_w * 0.025), (37, 99, 235, 225))
    if banner.width > video_w * 0.92:
        banner = banner.resize((int(video_w * 0.92), int(banner.height * video_w * 0.92 / banner.width)))
    b = out_dir / "notice_banner.png"
    banner.save(b)
    lines = ["ဒီဗီဒီယိုက ရုပ်ရှင် ဇာတ်လမ်းကို ပြန်ပြောပြထားတာပါ", "အဖြစ်မှန် သတင်း မဟုတ်ပါ",
             "A recap of a fictional film, not real news"]
    parts = [pill(t, f_big if i < 2 else f_small, int(video_w * 0.03), (8, 12, 24, 235)) for i, t in enumerate(lines)]
    cw = min(int(video_w * 0.94), max(p.width for p in parts))
    parts = [p if p.width <= cw else p.resize((cw, int(p.height * cw / p.width))) for p in parts]
    gap = int(video_h * 0.012)
    card = Image.new("RGBA", (cw, sum(p.height for p in parts) + gap * (len(parts) - 1)), (0, 0, 0, 0))
    y = 0
    for p in parts:
        card.alpha_composite(p, ((cw - p.width) // 2, y))
        y += p.height + gap
    c = out_dir / "notice_card.png"
    card.save(c)
    top = int(video_h * 0.02)
    return [(b, (video_w - banner.width) // 2, top, None),
            (c, (video_w - card.width) // 2, (video_h - card.height) // 2, "lt(t,4)")]


def mosaic_video(src, out, censor, progress=None):
    """A finished video with a mosaic over each (start, end, box) — sound untouched."""
    w, h = video_size(src)
    steps, cur = _mosaic_parts(censor, w, h, "0:v")
    enc = ["-c:v", "h264_videotoolbox", "-b:v", "3500k"] if _has_encoder("h264_videotoolbox") else ["-c:v", "libx264", "-crf", "21"]
    run_progress(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", ";".join(steps),
                  "-map", f"[{cur}]", "-map", "0:a?", *enc, "-pix_fmt", "yuv420p", "-c:a", "copy",
                  "-movflags", "+faststart", str(out)], probe_duration(src), progress)


def mux(src_video, dub_wav, out_mp4, bg_wav=None, bg_volume=0.8, logo=None,
        captions=None, blur=None, keyframes=None, lead=0.0, tail=0.0, intro_img=None, hide=None,
        frame=None, progress=None, censor=None):
    """Combine picture + dub track. progress: optional cb(fraction) while encoding.
    censor: optional [(start, end, x0, y0, x1, y1)]: that box (fractions) of the picture
    blurred while it shows — blood / nudity hidden like TV censoring.

    bg_wav: music/effects with the original voices removed (separate_music).
    It goes under the dub at bg_volume and dips a little whenever the dub
    voice speaks (sidechain compressor) so the new voice stays clear.
    Without bg_wav the original audio is dropped entirely — the original
    voices are never mixed in.
    logo: optional dict(path=PNG, position="top-right"|"top-left"|
    "bottom-right"|"bottom-left", size=fraction of video width).
    captions: optional dict(list=ffconcat file from render_captions, height=strip px,
    bottom=gap below the text as a fraction of the video height).
    blur: optional dict(bottom=fraction, height=fraction) — blurs a full-width
    band (to hide burned-in original captions); captions are centred on it.
    lead: seconds to hold the first frame before the video starts (intro).
    tail: seconds to hold the last frame after the video ends, so a dub that
    runs past the picture (the channel outro) is still seen and heard.
    intro_img: optional image (the cover) shown full-frame during the lead
    instead of the held first frame, which is often black.
    hide: optional [(x0, y0, x1, y1)] fractions — the video's own logos, blurred.
    frame: optional frame_layout() dict plus list=render_frames() ffconcat — the
    picture goes into a 9:16 TikTok frame; captions go below the picture and
    the logo is already drawn in the frame.
    """
    # Burned-in credit watermark, bottom-right; scale guard keeps even dims
    # for yuv420p.
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
    pad = []
    if lead:
        pad.append(f"start_duration={lead:.3f}:start_mode=clone")
    if tail:
        pad.append(f"stop_duration={tail:.3f}:stop_mode=clone")
    if pad:
        vf = f"tpad={':'.join(pad)}," + vf
    if not lead:
        intro_img = None
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
        sw_, sh_ = video_size(src_video)
        # 9:16 output (TikTok frame, or a portrait upload): phones taller than 9:16 make
        # TikTok trim the sides, and its buttons sit on the right — keep well inside
        tall = bool(frame) or sh_ > sw_
        # the round logo has no empty border of its own, and phone players crop a
        # little off 16:9 video's sides — keep it a bit inside the edges
        m = max(8, int(vw * 0.035))                      # margin from the top / bottom
        mx = max(8, int(vw * (0.08 if tall else 0.04)))  # from the sides (more on 9:16)
        x = f"{mx}" if "left" in logo.get("position", "") else f"W-w-{mx}"
        # inside the moving picture, not on bars around it (a portrait upload
        # with the film in the middle of coloured bars put the logo on the bar)
        top, bot = logo.get("area") or (0.0, 1.0)
        y = (f"H*{bot:.4f}-h-{m}" if "bottom" in logo.get("position", "") else f"H*{top:.4f}+{m}")
    bg_in = []
    if bg_wav and bg_volume > 0:
        bg_in = ["-i", str(bg_wav)]
        afc = ("[1:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[dub][sc];"
               f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={bg_volume:.2f}"
               + (f",adelay={int(lead * 1000)}:all=1" if lead else "") + "[bg];"
               "[bg][sc]sidechaincompress=threshold=0.02:ratio=4:attack=20:release=400[duck];"
               "[duck][dub]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[a]")
        amap = ["-map", "[a]"]
    else:
        afc, amap = None, ["-map", "1:a:0"]

    logo_in = ["-i", str(logo["path"])] if logo else []
    logo_idx = 2 + (1 if bg_in else 0)
    cap_in = ["-f", "concat", "-safe", "0", "-i", str(captions["list"])] if captions else []
    cap_idx = logo_idx + (1 if logo else 0)
    intro_in = (["-loop", "1", "-framerate", "25", "-t", f"{lead:.3f}", "-i", str(intro_img)]
                if intro_img else [])
    intro_idx = cap_idx + (1 if captions else 0)
    frame_in = ["-f", "concat", "-safe", "0", "-i", str(frame["list"])] if frame else []
    frame_idx = intro_idx + (1 if intro_img else 0)
    if intro_img:
        iw, ih = video_size(src_video)
    # ✨ a low-quality source (short side under 720 px): a light denoise first, and at the
    # end an upscale to 1080p (lanczos) with a mild sharpen — a bit crisper, and YouTube
    # treats a 1080p upload as HD (better encoding) instead of SD
    try:
        _sw, _sh = video_size(src_video)
    except Exception:  # noqa: BLE001
        _sw = _sh = 1080
    enhance = min(_sw, _sh) < 700
    draw = bool(logo or captions or blur or intro_img or hide or frame or censor or enhance)

    def _cmd(copy_video, encoder="libx264"):
        if copy_video:   # nothing to draw: keep the original picture, no re-encode
            fc = ["-filter_complex", afc] if afc else []
            vid = ["-map", "0:v:0", "-c:v", "copy"]
        else:
            if draw:
                parts = [f"[0:v]{'hqdn3d=1.5:1.5:6:6,' if enhance else ''}{vf.replace(',format=yuv420p', '')}[s0]"]
                cur = "s0"
                if censor:        # blood / nudity: just that part pixelated while it shows
                    cw_, ch_ = video_size(src_video)
                    steps, cur = _mosaic_parts(censor, cw_, ch_, cur, lead)
                    parts += steps
                if blur:
                    b0, bh = float(blur["bottom"]), float(blur["height"])
                    by = f"ih*{1 - b0 - bh:.4f}"
                    parts.append(f"[{cur}]split[bm][bb]")
                    # strong blur (radius grows with the band, chroma too) so the old letters
                    # can't be read through it
                    # (done on a 1/8-size copy of the band and scaled back: the same look,
                    # far less work than a big blur at full size)
                    parts.append(f"[bb]crop=iw:ih*{bh:.4f}:0:{by},split[bc][bref];"
                                 "[bc]scale=trunc(iw/12)*2:trunc(ih/12)*2,"
                                 "boxblur=luma_radius='min(h/4,4)':luma_power=2:"
                                 "chroma_radius='min(ch/4,2)':chroma_power=2[bs];"
                                 "[bs][bref]scale2ref=flags=bicubic[bz][bdrop];[bdrop]nullsink")
                    # only while the film's own captions are on screen (= while someone talks
                    # in it): no needless blur band over scenes without them
                    when = blur.get("when")
                    en = (":enable='" + "+".join(f"between(t,{a + lead:.2f},{b + lead:.2f})" for a, b in when) + "'"
                          if when else "")
                    parts.append(f"[bm][bz]overlay=0:H*{1 - b0 - bh:.4f}{en}[s1]")
                    cur = "s1"
                if hide:          # the video's own logos: erased (delogo fills each box in from
                    sw, sh = video_size(src_video)            # its edges) — a blur left them readable
                    sw, sh = sw // 2 * 2, sh // 2 * 2
                    dl = []
                    for box in hide:                # (own names: x / y are the logo's spot)
                        x0, y0, x1, y1 = box[:4]
                        dx, dy = max(1, int(sw * x0)), max(1, int(sh * y0))
                        dw = min(int(sw * x1), sw - 2) - dx
                        dh = min(int(sh * y1), sh - 2) - dy
                        if dw > 4 and dh > 4:
                            when = (f":enable='between(t,{box[4] + lead:.2f},{box[5] + lead:.2f})'"
                                    if len(box) >= 6 else "")      # a moving logo: only while it shows
                            dl.append(f"delogo=x={dx}:y={dy}:w={dw}:h={dh}{when}")
                    if dl:
                        parts.append(f"[{cur}]{','.join(dl)}[hd]")
                        cur = "hd"
                if intro_img:     # the cover over the held intro frame (it has the logo already)
                    parts.append(f"[{intro_idx}:v]scale={iw}:{ih}:force_original_aspect_ratio=decrease,"
                                 f"pad={iw}:{ih}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[ic]")
                    parts.append(f"[{cur}][ic]overlay=0:0:enable='lt(t,{lead:.3f})':eof_action=pass[si]")
                    cur = "si"
                if logo:
                    parts.append(f"[{logo_idx}:v]scale={lw}:-1,format=rgba[lg]")
                    parts.append(f"[{cur}][lg]overlay={x}:{y}"
                                 + (f":enable='gte(t,{lead:.3f})'" if intro_img else "") + "[s2]")
                    cur = "s2"
                if frame:         # picture (with its logo) into the 9:16 frame (the frame has a see-through window)
                    crop = (f"crop=iw:trunc(ih*{frame['keep']:.4f}/2)*2:0:0,"
                            if frame.get("keep", 1.0) < 1.0 else "")
                    parts.append(f"[{cur}]{crop}scale={frame['w']}:{frame['vh']},setsar=1,"
                                 f"pad={frame['w']}:{frame['h']}:0:{frame['vy']}:black[pv]")
                    parts.append(f"[{frame_idx}:v]format=rgba[fr]")
                    parts.append("[pv][fr]overlay=0:0:eof_action=repeat:shortest=1[sf]")
                    cur = "sf"
                if captions:
                    ch = int(captions["height"])
                    cy = frame_caption_y(frame, ch, blur) if frame else caption_y(ch, blur, captions.get("bottom", 0.08))
                    parts.append(f"[{cap_idx}:v]format=rgba[cp]")
                    parts.append(f"[{cur}][cp]overlay=0:{cy}:eof_action=pass[s3]")
                    cur = "s3"
                if enhance:
                    up = ("" if frame else            # the 9:16 frame is 1080 wide already
                          "scale=-2:1080:flags=lanczos," if _sw >= _sh else "scale=1080:-2:flags=lanczos,")
                    parts.append(f"[{cur}]{up}unsharp=5:5:0.6:5:5:0[en]")
                    cur = "en"
                parts.append(f"[{cur}]format=yuv420p[v]")
                vfc = ";".join(parts)
            else:
                vfc = f"[0:v]{vf}[v]"
            fc = ["-filter_complex", vfc + (";" + afc if afc else "")]
            if encoder == "h264_videotoolbox":     # Apple hardware encoder: ~5x faster
                enc = ["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"]
            else:
                enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
            if keyframes:     # exact cut points for split_video
                enc += ["-force_key_frames", ",".join(f"{t:.3f}" for t in keyframes)]
            vid = ["-map", "[v]", *enc]
        return ["ffmpeg", "-y", "-v", "error", "-i", str(src_video), "-i", str(dub_wav),
                *bg_in, *logo_in, *cap_in, *intro_in, *frame_in, *fc, *vid, *amap, "-c:a", "aac", "-b:a", "128k",
                # No -shortest: the finish-the-sentence rule lets the tail of the
                # last utterance run a little past the video end; -shortest would
                # cut it off again.
                "-movflags", "+faststart", str(out_mp4)]

    if not watermark and not draw and not keyframes and not lead and not tail:
        try:
            run(_cmd(copy_video=True))
            return
        except RuntimeError as e:   # codec not allowed in MP4 etc. — re-encode
            print(f"[mux] stream copy failed ({e}); re-encoding")
    try:
        length = probe_duration(dub_wav) + lead
    except Exception:  # noqa: BLE001
        length = 0
    if _has_encoder("h264_videotoolbox"):
        try:
            run_progress(_cmd(copy_video=False, encoder="h264_videotoolbox"), length, progress)
            return
        except RuntimeError as e:
            print(f"[mux] hardware encoder failed ({e}); using libx264")
    run_progress(_cmd(copy_video=False), length, progress)


def split_points(cues, total, minutes, min_tail=240.0, window=90.0, max_over=1.3):
    """Cut times for parts of about `minutes`, moved into the nearest pause between
    captions (within `window` s) so no sentence is cut in half. No part comes out
    tiny: when an even split would leave a last part under `min_tail` (4 min), it is
    merged into the one before if that stays under `max_over` x the length (12 min
    with 10-min parts → one 12-min video); otherwise all parts are made the same
    length (25 min with 10-min parts → 3 × ~8 min, not 10 + 10 + 5)."""
    import math
    step = minutes * 60.0
    if not minutes or total <= step:
        return []
    min_tail = min(min_tail, step * 0.5)                  # 3-min parts: a tail under 1.5 min is merged
    n = math.ceil(total / step)
    if total - (n - 1) * step < min_tail and n > 1:
        if total / (n - 1) <= step * max_over:
            n -= 1                                        # the short tail goes into the part before
    if n <= 1:
        return []
    part = total / n                                      # equal parts
    gaps = [(b, a2) for (_, b, _), (a2, _, _) in zip(cues, cues[1:]) if a2 - b > 0.25]
    cuts = []
    for k in range(1, n):
        target = part * k
        near = [g for g in gaps if abs((g[0] + g[1]) / 2 - target) <= window
                and (g[0] + g[1]) / 2 > (cuts[-1] if cuts else 0) + part / 2]
        t = min(((g[0] + g[1]) / 2 for g in near), key=lambda m: abs(m - target)) if near else target
        cuts.append(round(t, 3))
    return cuts


def split_video(src, cuts, out_dir, stem="part"):
    """Cut src at `cuts` (seconds) without re-encoding; returns the part paths.
    mux(keyframes=cuts) put keyframes exactly there, so the cuts are exact."""
    out_dir = Path(out_dir)
    bounds = [0.0, *cuts, None]
    paths = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        out = out_dir / f"{stem}{i}.mp4"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-i", str(src)]
        if b is not None:
            cmd += ["-t", f"{b - a:.3f}"]
        cmd += ["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", str(out)]
        run(cmd)
        paths.append(out)
    return paths


def add_outro_tail(part, outro_audio, out, caption_png=None, caption_y=None):
    """Append the channel outro to the end of a part: the last frame is held
    while the outro voice plays (with its caption strip, if given).
    Fast way: encode only the few-second tail and join it to the untouched part
    (stream copy) — re-encoding a whole 10-minute part took ~1 min each."""
    try:
        _outro_tail_fast(part, outro_audio, out, caption_png, caption_y)
        return
    except Exception as e:  # noqa: BLE001 - fall back to re-encoding the whole part
        print(f"[outro] fast tail failed ({str(e)[:200]}); re-encoding the part")
    _outro_tail_slow(part, outro_audio, out, caption_png, caption_y)


def _stream_info(path):
    import json as _json
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,time_base,sample_rate,channels,pix_fmt",
             "-of", "json", str(path)])
    info = {}
    for st in _json.loads(p.stdout).get("streams", []):
        info.setdefault(st.get("codec_type"), st)
    return info


def _outro_tail_fast(part, outro_audio, out, caption_png, caption_y):
    info = _stream_info(part)
    v, a = info["video"], info["audio"]
    if v.get("codec_name") != "h264" or a.get("codec_name") != "aac":
        raise RuntimeError("not h264/aac")
    w, h, fps = int(v["width"]), int(v["height"]), v["r_frame_rate"]
    tb = int(v["time_base"].split("/")[1])
    sr, ch = int(a.get("sample_rate", 44100)), int(a.get("channels", 2))
    odur = probe_duration(outro_audio)
    hold = odur + 1.2                      # 0.3 s pause, the outro, a short rest
    tmp = Path(out).with_name(Path(part).stem + "_tail")
    tmp.mkdir(exist_ok=True)
    try:
        last = tmp / "last.png"
        run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.5", "-i", str(part), "-update", "1",
             "-frames:v", "100", str(last)])
        ins = ["-loop", "1", "-framerate", fps, "-t", f"{hold:.3f}", "-i", str(last), "-i", str(outro_audio)]
        vf = f"[0:v]scale={w}:{h},setsar=1,format=yuv420p[v0]"
        if caption_png:
            ins += ["-loop", "1", "-t", f"{hold:.3f}", "-i", str(caption_png)]
            vf += f";[2:v]format=rgba[cp];[v0][cp]overlay=0:{caption_y}:enable='gte(t,0.3)'[v1];[v1]format=yuv420p[v]"
        else:
            vf += ";[v0]null[v]"
        lay = "stereo" if ch == 2 else "mono"
        af = (f"[1:a]aformat=sample_rates={sr}:channel_layouts={lay},adelay=300:all=1,"
              f"apad=whole_dur={hold:.3f}[a]")
        enc = (["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"] if _has_encoder("h264_videotoolbox")
               else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        tail = tmp / "tail.mp4"
        run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", vf + ";" + af,
             "-map", "[v]", "-map", "[a]", *enc, "-pix_fmt", "yuv420p", "-r", fps,
             "-c:a", "aac", "-b:a", "128k", "-ar", str(sr), "-ac", str(ch),
             "-video_track_timescale", str(tb), "-t", f"{hold:.3f}", str(tail)])
        lst = tmp / "list.txt"
        lst.write_text(f"file '{Path(part).resolve()}'\nfile '{tail.resolve()}'\n")
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", str(out)])
        want = probe_duration(part) + hold
        got = probe_duration(out)
        if abs(got - want) > 1.0:
            raise RuntimeError(f"joined length {got:.1f}s, expected {want:.1f}s")
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def quiet_points(video, a, b, noise="-32dB", min_len=0.25):
    """Middles of the pauses (no speech) in video between a and b seconds,
    as times in the video. [] on error or when it is never quiet."""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{max(0.0, a):.3f}", "-t", f"{b - a:.3f}",
                            "-i", str(video), "-vn", "-af", f"silencedetect=n={noise}:d={min_len}", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return []
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", p.stderr)]
    off = max(0.0, a)
    return [off + (s0 + e0) / 2 for s0, e0 in zip(starts, ends)]


def hook_span(video, t, dur_total, lo=4.0, hi=10.0, aim=6.5, cues=None):
    """(start, length) of a teaser around moment t that starts and ends in a
    pause, so no sentence is cut in half; about `aim` s, between lo and hi.
    cues: the captions [(start, end)] on this video — then the teaser starts where a
    caption line STARTS and ends where one ends (a pause inside a long narrated line
    began the teaser mid-sentence: "ဆိုတဲ့ …" without the name before it).
    Falls back to a plain aim-second window."""
    if cues:
        starts = [a for a, b in cues if t - 12 <= a <= t + 1 and a >= 0]
        if starts:
            st = max(starts)
            ends = [b for a, b in cues if st + lo <= b <= st + hi + 4]
            if ends:
                en = min(ends, key=lambda x: abs(x - st - aim))
            else:                                       # one long line: cut at its first pause
                q = [x for x in quiet_points(video, st + lo, st + hi + 2) if st + lo <= x <= st + hi + 2]
                en = q[0] if q else st + aim
            return st, max(lo, min(en, dur_total - 1) - st)
    q = quiet_points(video, t - 6, t + hi + 2)
    before = [x for x in q if t - 6 <= x <= t - 0.5]
    start = max(before) if before else t - 2.5
    after = [x for x in q if start + lo <= x <= start + hi]
    end = min(after, key=lambda x: abs(x - start - aim)) if after else start + aim
    start = max(0.0, start)
    end = min(end, dur_total - 1)
    return start, max(lo, end - start)


def add_hook(video, start, out, dur=6.0, fade=0.35):
    """Open the video with a short teaser: `dur` seconds from `start` (inside the
    video, with its voice and captions) play first, fade out, then the video plays
    from the beginning as usual (the teaser moment comes again in its place).
    Fast way: encode only the teaser and join it to the untouched video."""
    try:
        _hook_fast(video, start, out, dur, fade)
        return
    except Exception as e:  # noqa: BLE001 - re-encode the whole video instead
        print(f"[hook] fast join failed ({str(e)[:200]}); re-encoding")
    total = probe_duration(video)
    fo = dur - fade
    vf = (f"[0:v]trim={start:.3f}:{start + dur:.3f},setpts=PTS-STARTPTS,fade=t=out:st={fo:.3f}:d={fade}[hv];"
          f"[0:a]atrim={start:.3f}:{start + dur:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={fo:.3f}:d={fade}[ha];"
          f"[hv][ha][0:v][0:a]concat=n=2:v=1:a=1[v][a]")
    enc = (["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"] if _has_encoder("h264_videotoolbox")
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-filter_complex", vf,
         "-map", "[v]", "-map", "[a]", *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-t", f"{total + dur:.3f}", "-movflags", "+faststart", str(out)])


def _hook_fast(video, start, out, dur, fade):
    info = _stream_info(video)
    v, a = info["video"], info["audio"]
    if v.get("codec_name") != "h264" or a.get("codec_name") != "aac":
        raise RuntimeError("not h264/aac")
    w, h, fps = int(v["width"]), int(v["height"]), v["r_frame_rate"]
    tb = int(v["time_base"].split("/")[1])
    sr, ch = int(a.get("sample_rate", 44100)), int(a.get("channels", 2))
    fo = dur - fade
    tmp = Path(out).with_name(Path(video).stem + "_hook")
    tmp.mkdir(exist_ok=True)
    try:
        lay = "stereo" if ch == 2 else "mono"
        enc = (["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"] if _has_encoder("h264_videotoolbox")
               else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        clip = tmp / "hook.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{dur:.3f}",
             "-vf", f"scale={w}:{h},setsar=1,fade=t=out:st={fo:.3f}:d={fade},format=yuv420p",
             "-af", f"aformat=sample_rates={sr}:channel_layouts={lay},afade=t=out:st={fo:.3f}:d={fade}",
             *enc, "-pix_fmt", "yuv420p", "-r", fps, "-c:a", "aac", "-b:a", "128k", "-ar", str(sr), "-ac", str(ch),
             "-video_track_timescale", str(tb), str(clip)])
        lst = tmp / "list.txt"
        lst.write_text(f"file '{clip.resolve()}'\nfile '{Path(video).resolve()}'\n")
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", str(out)])
        want = probe_duration(video) + probe_duration(clip)
        got = probe_duration(out)
        if abs(got - want) > 1.0:
            raise RuntimeError(f"joined length {got:.1f}s, expected {want:.1f}s")
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def _outro_tail_slow(part, outro_audio, out, caption_png=None, caption_y=None):
    dur = probe_duration(part)
    odur = probe_duration(outro_audio)
    t0, hold = dur + 0.3, odur + 0.9
    ins = ["-i", str(part), "-i", str(outro_audio)]
    vf = f"[0:v]tpad=stop_mode=clone:stop_duration={hold:.3f}[v0]"
    if caption_png:
        ins += ["-loop", "1", "-i", str(caption_png)]
        vf += (f";[2:v]format=rgba[cp];[v0][cp]overlay=0:{caption_y}:"
               f"enable='gte(t,{t0:.3f})':shortest=1[v]")
    else:
        vf += ";[v0]null[v]"
    af = (f"[0:a]apad=pad_dur={hold:.3f}[a0];[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
          f"adelay={int(t0 * 1000)}:all=1[o];[a0][o]amix=inputs=2:duration=first:normalize=0[a]")
    enc = (["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"] if _has_encoder("h264_videotoolbox")
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
    run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", vf + ";" + af,
         "-map", "[v]", "-map", "[a]", *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-t", f"{dur + hold:.3f}", "-movflags", "+faststart", str(out)])


def caption_y(strip_h, blur=None, bottom=0.08, margin=0.035):
    """ffmpeg overlay y for the caption strip. The text box sits at the
    strip's bottom edge, so the strip's bottom goes to the bottom of the
    blurred band (one line covers the band, a second line grows upwards),
    but never closer than `margin` to the frame's bottom — players and phone
    UIs cut the last few percent."""
    if blur:      # text is centred in its strip (box=False): centre the strip on the band
        b0, bh = float(blur["bottom"]), float(blur["height"])
        mid = 1 - b0 - bh / 2
        return f"H*{mid:.4f}-{int(strip_h) // 2}"
    bot = 1 - float(bottom)
    return f"H*{min(bot, 1 - margin):.4f}-{int(strip_h)}"


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


def video_rotation(path):
    """The display rotation a phone video carries (0, 90, -90, 180): its pixels are
    stored sideways and players turn them. 0 when there is none."""
    try:
        p = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream_side_data=rotation:stream_tags=rotate", "-of", "default=nw=1:nk=1", str(path)])
        for v in p.stdout.split():
            if v.lstrip("-").replace(".", "").isdigit() and round(float(v)) % 360:
                return int(round(float(v)))
    except Exception:  # noqa: BLE001
        pass
    return 0


def make_upright(path):
    """A phone video filmed sideways (rotation flag): re-encode it once with the
    picture really turned and no flag left, so sizes, layouts, captions and the
    frame all work on the picture as it is seen — otherwise the finished video came
    out turned on its side (captions running down the left edge). True if changed."""
    rot = video_rotation(path)
    if not rot:
        return False
    path = Path(path)
    tmp = path.with_name(path.stem + ".upright" + path.suffix)
    enc = (["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"] if _has_encoder("h264_videotoolbox")
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"])
    # ffmpeg turns the frames by the flag when decoding (autorotate); the output gets none
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", *enc,
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", str(tmp)])
    if video_rotation(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("rotation flag still there")
    tmp.replace(path)
    return True


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
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3}
    import shutil
    if shutil.which("node"):    # YouTube needs a JS runtime, or only low-quality formats are offered
        opts["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    return {**opts, **extra}


class YouTubeBlocked(RuntimeError):
    """YouTube asks this computer to prove it is not a bot (too many downloads)."""


def _bot_check(e):
    t = str(e)
    return "confirm you" in t or "not a bot" in t or "429" in t


def _ydl_run(url, opts, download):
    """extract_info; when YouTube says "sign in to confirm you're not a bot" (too many
    downloads from this IP), try again with the browser's YouTube sign-in cookies
    (DUBLAB_YT_BROWSER, default chrome)."""
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=download) or {}
    except Exception as e:  # noqa: BLE001
        if not _bot_check(e):
            raise
        first = e
    browser = os.environ.get("DUBLAB_YT_BROWSER", "chrome").strip()
    if browser.lower() in ("", "0", "none", "off"):
        raise YouTubeBlocked(str(first)[:300])
    try:
        with yt_dlp.YoutubeDL({**opts, "cookiesfrombrowser": (browser,)}) as ydl:
            return ydl.extract_info(url, download=download) or {}
    except Exception as e:  # noqa: BLE001
        if _bot_check(e) or "cookie" in str(e).lower():
            raise YouTubeBlocked(str(e)[:300]) from None
        raise


def probe_url(url):
    """Return (title, duration_seconds, is_live) without downloading."""
    info = _ydl_run(url, _ydl_opts(), False)
    return (info.get("title") or "?", float(info.get("duration") or 0),
            bool(info.get("is_live")))


def download_url(url, job_dir, max_mb):
    """Download the video (≤720p) into job_dir as input.mp4; return its path."""
    import yt_dlp
    out = Path(job_dir) / "input.mp4"
    opts = _ydl_opts(
        # up to 720p on the SHORT side: "height<=720" took a portrait (9:16) video
        # down to 360x640, since its 720p version is 1280 tall
        format="bv*+ba/b",
        format_sort=["res:720"],
        merge_output_format="mp4",
        outtmpl=str(Path(job_dir) / "input.%(ext)s"),
        max_filesize=max_mb * 1024 * 1024)
    info = _ydl_run(url, opts, True)
    if not out.exists():
        raise RuntimeError("video download failed")
    # the video's own thumbnail: the covers can be made from it (its look, our title)
    try:
        import io as _io
        import urllib.request as _ur
        from PIL import Image as _Img
        url = info.get("thumbnail") or ""
        if url:
            import ssl as _ssl
            try:
                import certifi as _certifi
                ctx = _ssl.create_default_context(cafile=_certifi.where())
            except ImportError:
                ctx = _ssl.create_default_context()
            data = _ur.urlopen(_ur.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20, context=ctx).read()
            _Img.open(_io.BytesIO(data)).convert("RGB").save(Path(job_dir) / "src_thumb.jpg", quality=95)
    except Exception:  # noqa: BLE001 - the covers are made from frames then
        pass
    # who posted it and under what licence — for the copyright risk note
    try:
        import json as _json
        keep = {k: info.get(k) for k in ("extractor_key", "channel", "uploader", "channel_is_verified",
                                         "channel_follower_count", "license", "title", "categories")}
        (Path(job_dir) / "src_info.json").write_text(_json.dumps(keep, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - only a note
        pass
    return out


# ---- cover image: a strong frame + big Burmese title ----

def _frame_scores(video, times, tmp):
    """Score frames: sharp, contrasty, not too dark/bright. Returns [(score, t)]."""
    import numpy as np
    from PIL import Image
    out = []
    for i, t in enumerate(times):
        p = Path(tmp) / f"cand{i}.png"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=320:-2", str(p)], capture_output=True)
        if r.returncode or not p.exists():
            continue
        g = np.asarray(Image.open(p).convert("L"), dtype=np.float32) / 255.0
        p.unlink(missing_ok=True)
        bright, contrast = g.mean(), g.std()
        lap = (g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:])
        sharp = float(lap.var()) ** 0.5
        light = max(0.0, 1.0 - abs(bright - 0.45) * 2.2)      # best around mid-grey
        out.append((sharp * contrast * light, t))
    return out


def pick_cover_times(video, start, end, count=3, samples=24, tmp="."):
    """The best `count` moments between start and end, spread apart."""
    span = max(end - start, 1.0)
    times = [start + span * (i + 0.5) / samples for i in range(samples)]
    scored = sorted(_frame_scores(video, times, tmp), reverse=True)
    picked = []
    for s, t in scored:
        if all(abs(t - u) > span / (count * 2.5) for u in picked):
            picked.append(t)
        if len(picked) == count:
            break
    return picked or [start + span / 3]


# TikTok covers are 3:4: TikTok's cover picker (and the profile grid) crops a 9:16
# picture to 3:4, cutting the top and bottom off — so the cover is made 3:4 to begin with
TT_COVER = (1080, 1440)


def render_cover(video, t, title, out_jpg, portrait=False, logo=None, blur=None):
    """Frame at t, cropped to 1280x720 (or 1080x1920), darkened at the
    bottom, with the Burmese title in big outlined yellow/white letters."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    W, H = TT_COVER if portrait else (1280, 720)
    tmp = Path(out_jpg).with_suffix(".src.png")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", str(tmp)])
    img = Image.open(tmp).convert("RGB")
    tmp.unlink(missing_ok=True)
    if blur:                      # hide burned-in original captions, as in the video
        b0, bh = float(blur["bottom"]), float(blur["height"])
        box = (0, int(H * (1 - b0 - bh)), W, int(H * (1 - b0)))
        img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(18)), box[:2])
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.15)
    img = img.convert("RGBA")

    # dark gradient under the title
    grad_h = int(H * (0.55 if portrait else 0.6))
    grad = Image.new("L", (1, grad_h))
    for y in range(grad_h):
        grad.putpixel((0, y), int(215 * (y / grad_h) ** 1.6))
    shade = Image.new("RGBA", (W, grad_h), (0, 0, 0, 255))
    shade.putalpha(grad.resize((W, grad_h)))
    bottom_y = H - grad_h - (int(H * 0.18) if portrait else 0)   # TikTok UI covers the bottom
    img.alpha_composite(shade, (0, max(0, bottom_y)))
    if portrait:                  # keep the band below the title dark too
        img.alpha_composite(Image.new("RGBA", (W, H - (bottom_y + grad_h)), (0, 0, 0, 215)),
                            (0, bottom_y + grad_h))

    # title: as big as fits in at most 3 lines
    d = ImageDraw.Draw(img)
    max_w = int(W * 0.9)
    size = int((W * 0.13) if portrait else (H * 0.15))
    while True:
        font = _caption_font(size)
        lines = _wrap(d, title, font, max_w)
        if len(lines) <= (3 if portrait else 2) or size < 40:
            break
        size = int(size * 0.9)
    line_h = int(size * 1.55)
    stroke = max(3, size // 11)
    base = H - (int(H * 0.22) if portrait else int(H * 0.07))
    y = base - line_h * len(lines)
    for k, ln in enumerate(lines):
        w = _tlen(d, ln, font)
        x = (W - w) / 2
        color = (255, 214, 0) if k == 0 else (255, 255, 255)        # first line yellow
        _tdraw(d, (x + stroke, y + stroke * 1.5), ln, font, fill=(0, 0, 0, 160))   # shadow
        _tdraw(d, (x, y), ln, font, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += line_h

    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        lw = int(W * (0.2 if portrait else 0.14))
        lg = lg.resize((lw, max(1, int(lg.height * lw / lg.width))))
        m = int(W * 0.03)
        img.alpha_composite(lg, (W - lw - m, m))
    img.convert("RGB").save(out_jpg, "JPEG", quality=90)
    return Path(out_jpg)


def _grab(video, t, w, h, hide=None, blur=None, cx=0.5, face=None, fy=0.42):
    """Frame at t, black letterbox bars trimmed, scaled and centre-cropped
    to w x h (PIL RGB). hide: [(x0, y0, x1, y1)] fractions blurred first (old logos)."""
    from PIL import Image, ImageOps
    import io
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if r.returncode or not r.stdout:
        raise RuntimeError("frame grab failed")
    img = Image.open(io.BytesIO(r.stdout)).convert("RGB")
    fpx = None        # the face box in pixels (moved along with every crop below)
    if face:
        Wo, Ho = img.size
        fpx = [face[0] * Wo, face[1] * Ho, face[2] * Wo, face[3] * Ho]
    if hide:          # old logos erased like ffmpeg's delogo: filled in from the box edges
        a = np.asarray(img, dtype=np.float32).copy()
        H0, W0 = a.shape[:2]
        for x0, y0, x1, y1 in hide:
            l, t = max(1, int(W0 * x0)), max(1, int(H0 * y0))
            r, b = min(W0 - 2, int(W0 * x1)), min(H0 - 2, int(H0 * y1))
            if r - l < 3 or b - t < 3:
                continue
            top, bot = a[t - 1, l:r + 1], a[b + 1, l:r + 1]          # edge rows / columns
            lef, rig = a[t:b + 1, l - 1], a[t:b + 1, r + 1]
            fy = np.linspace(0, 1, b - t + 1)[:, None, None]
            fx = np.linspace(0, 1, r - l + 1)[None, :, None]
            vert = top[None] * (1 - fy) + bot[None] * fy
            horz = lef[:, None] * (1 - fx) + rig[:, None] * fx
            a[t:b + 1, l:r + 1] = (vert + horz) / 2
        img = Image.fromarray(a.clip(0, 255).astype(np.uint8))
    if blur:          # the old captions: cut the band off (it sits at the bottom) — a photo
        b0, bh = float(blur["bottom"]), float(blur["height"])          # looks better without
        W0, H0 = img.size                                              # a smudge across it
        if b0 < 0.15 and 1 - b0 - bh > 0.45:
            img = img.crop((0, 0, W0, int(H0 * (1 - b0 - bh))))
        else:
            from PIL import ImageFilter
            box = (0, int(H0 * (1 - b0 - bh)), W0, int(H0 * (1 - b0)))
            img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(24)), box[:2])
    # trim real black bars only: a bar row/column is dark AND flat (a dark part of
    # the scene has texture — trimming it slid the face off the photo)
    g = np.asarray(img.convert("L"), dtype=np.float32)
    bar_r = (g.mean(1) <= 18) & (g.std(1) < 6)
    bar_c = (g.mean(0) <= 18) & (g.std(0) < 6)
    rows, cols = np.where(~bar_r)[0], np.where(~bar_c)[0]
    if len(rows) > g.shape[0] * 0.4 and len(cols) > g.shape[1] * 0.4:
        W1 = img.size[0]
        img = img.crop((cols[0], rows[0], cols[-1] + 1, rows[-1] + 1))
        cx = (cx * W1 - cols[0]) / max(1, cols[-1] + 1 - cols[0])   # the face, in the trimmed frame
        if fpx:
            fpx = [fpx[0] - cols[0], fpx[1] - rows[0], fpx[2] - cols[0], fpx[3] - rows[0]]
    if fpx and fpx[3] > fpx[1] and fpx[1] < img.size[1]:
        # 👤 frame the face: a window of the panel's shape around it, the face about a third
        # of its height and at `fy` down it — clear of the soft edge where two photos blend
        # (a face up in that edge came out as half a face)
        W0, H0 = img.size
        fh = fpx[3] - fpx[1]
        hw = min(H0, max(fh / 0.34, h / 2.6, 120.0))
        ww = hw * w / h
        if ww > W0:
            ww, hw = W0, W0 * h / w
        fcx, fcy = (fpx[0] + fpx[2]) / 2, (fpx[1] + fpx[3]) / 2
        x0 = min(max(fcx - ww / 2, 0), W0 - ww)
        y0 = min(max(fcy - fy * hw, 0), H0 - hw)
        return img.crop((int(x0), int(y0), int(x0 + ww), int(y0 + hw))).resize((w, h), Image.LANCZOS)
    # scale to cover w x h, then cut the window so the person (cx = their x across
    # the frame, 0-1) sits in its middle. (ImageOps.fit's "centering" is not that:
    # 0.15 meant "15% of the spare width", which slid the face off the side.)
    W0, H0 = img.size
    k = max(w / W0, h / H0)
    sw, sh = max(w, round(W0 * k)), max(h, round(H0 * k))
    img = img.resize((sw, sh), Image.LANCZOS)
    left = int(min(max(cx * sw - w / 2, 0), sw - w))
    top = int(min(max(0.4 * sh - h * 0.4, 0), sh - h))
    return img.crop((left, top, left + w, top + h))


def _layout(n, W, H, portrait):
    """Panel boxes (x, y, w, h) for n = 1..5 photos. 4 = an even 2x2 grid;
    2 = side by side (landscape) or one above the other (portrait)."""
    g = 8
    if n == 1:
        return [(0, 0, W, H)]
    if n == 2:
        g = 6
        if portrait:              # TikTok: two photos stacked, the title UNDER them (in the middle it
            h1 = int(H * 0.39)    # covered the lower photo's eyes); each crop around its face
            return [(0, 0, W, h1), (0, h1 + g, W, int(H * 0.39))]
        w2 = (W - g) // 2
        return [(0, 0, w2, H), (w2 + g, 0, W - w2 - g, H)]
    if n == 4 and not portrait:   # poster style: the main character big on the left,
        lw = int(W * 0.44)        # the movie's action wide on the top right, two more below it
        th = int(H * 0.55)
        rw = W - lw
        return [(0, 0, lw, H), (lw, 0, rw, th), (lw, th, rw // 2, H - th), (lw + rw // 2, th, rw - rw // 2, H - th)]
    if portrait and n == 4:       # four film-shaped rows: whole scenes, no faces cut off
        g = 6
        hh = (H - 3 * g) // 4
        return [(0, k * (hh + g), W, hh if k < 3 else H - 3 * (hh + g)) for k in range(4)]
    if portrait:
        top = int(H * 0.42)
        rows = {3: [(0, 0, W, top), (0, top + g, W // 2 - g // 2, H - top - g),
                    (W // 2 + g // 2, top + g, W - W // 2 - g // 2, H - top - g)],
                4: [(0, 0, W, top)] + [(i * (W + g) // 3, top + g, W // 3 - g, H - top - g) for i in range(3)],
                5: [(0, 0, W // 2 - g // 2, top), (W // 2 + g // 2, 0, W - W // 2 - g // 2, top)]
                   + [(i * (W + g) // 3, top + g, W // 3 - g, H - top - g) for i in range(3)]}
        return rows[n]
    big = int(W * 0.56)
    rw = W - big - g
    if n == 3:
        return [(0, 0, big, H), (big + g, 0, rw, H // 2 - g // 2), (big + g, H // 2 + g // 2, rw, H - H // 2 - g // 2)]
    if n == 4:
        h3 = (H - 2 * g) // 3
        return [(0, 0, big, H)] + [(big + g, i * (h3 + g), rw, h3) for i in range(3)]
    third = (W - 2 * g) // 3
    top = int(H * 0.58)
    return [(0, 0, W // 2 - g // 2, top), (W // 2 + g // 2, 0, W - W // 2 - g // 2, top)] + \
           [(i * (third + g), top + g, third, H - top - g) for i in range(3)]


def _gold_title(img, title, box_w, center_x, center_y, max_lines, size, suffix=""):
    """Big gold-gradient title with a dark outline and glow, its ink centred
    on (center_x, center_y)."""
    from PIL import Image, ImageDraw, ImageFilter
    d = ImageDraw.Draw(img)
    while True:
        font = _caption_font(size)
        lines = _wrap(d, title, font, box_w)
        if suffix:          # the part number stays on the last line ("… - 2")
            lines[-1] += suffix
        if (len(lines) <= max_lines and _tlen(d, lines[-1], font) <= box_w) or size < 40:
            break
        size = int(size * 0.9)
    line_h = int(size * 1.5)
    stroke = max(4, size // 9)
    W, H = img.size
    block_h = line_h * len(lines)
    ink = [_tbbox(d, (0, k * line_h), ln, font) for k, ln in enumerate(lines)]
    top, bot = min(b[1] for b in ink), max(b[3] for b in ink)
    y0 = int(center_y - (top + bot) / 2)      # centre the real ink, not the line boxes
    mask = Image.new("L", (W, H), 0)
    outline = Image.new("L", (W, H), 0)
    md, od = ImageDraw.Draw(mask), ImageDraw.Draw(outline)
    for k, ln in enumerate(lines):
        w = _tlen(d, ln, font)
        x, y = center_x - w / 2, y0 + k * line_h
        _tdraw(od, (x, y), ln, font, fill=255, stroke_width=stroke, stroke_fill=255)
        _tdraw(md, (x, y), ln, font, fill=255)
    glow = outline.filter(ImageFilter.GaussianBlur(stroke * 1.6))
    img.paste(Image.new("RGB", (W, H), (255, 140, 0)), (0, 0), glow.point(lambda v: int(v * 0.55)))
    img.paste(Image.new("RGB", (W, H), (60, 10, 0)), (0, 0), outline)          # dark outline
    grad = Image.new("RGB", (1, block_h + 2))
    for yy in range(block_h + 2):              # light gold → deep orange, top to bottom (per line)
        f = (yy % line_h) / line_h
        grad.putpixel((0, yy), (255, int(246 - 90 * f), int(170 - 160 * f)))
    gfull = Image.new("RGB", (W, H), (255, 200, 0))
    gfull.paste(grad.resize((W, block_h + 2)), (0, max(0, y0)))
    img.paste(gfull, (0, 0), mask)
    return img


def _soft_mask(w, h, left=0.0, right=0.0, top=0.0, bottom=0.0, oval=False):
    """Alpha mask that fades out toward the given sides (fractions of the size),
    or an oval vignette — so a photo melts into what is behind it."""
    import numpy as np
    from PIL import Image
    if oval:
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        a = np.clip((1.0 - d) / 0.35, 0, 1)
    else:
        ax, ay = np.ones(w, np.float32), np.ones(h, np.float32)
        for frac, arr, rev in ((left, ax, False), (right, ax, True), (top, ay, False), (bottom, ay, True)):
            k = int(len(arr) * frac)
            if k > 1:
                ramp = np.linspace(0, 1, k, dtype=np.float32) ** 1.4
                if rev:
                    arr[-k:] *= ramp[::-1]
                else:
                    arr[:k] *= ramp
        a = np.outer(ay, ax)
    return Image.fromarray((a * 255).astype(np.uint8), "L")


def _poster(video, times, W, H, hide, blur, focus):
    """A movie-poster style cover (no grid). The main character's frame fills the
    cover WHOLE (16:9 into 16:9 — never cropped, so the face is never cut); the
    movie's action and another character float as smaller whole frames on the
    side away from the face, melting into it with soft edges."""
    from PIL import Image, ImageEnhance
    f = focus or {}
    hero, act, sec = times[0], times[1], times[2]
    pop = lambda im: ImageEnhance.Color(ImageEnhance.Contrast(im).enhance(1.15)).enhance(1.2)
    canvas = pop(_grab(video, hero, W, H, hide, blur))
    face_right = f.get(hero, 0.4) > 0.5
    # the free side gets a little darker so the smaller photos stand out
    dark = Image.new("RGB", (W, H), (8, 4, 2))
    side = _soft_mask(W, H, left=0.55) if not face_right else _soft_mask(W, H, right=0.55)
    canvas = Image.composite(canvas, dark, side.point(lambda v: int(255 - (255 - v) * 0.55)))
    for t, wf, yf in ((act, 0.46, 0.04), (sec, 0.38, 0.40)):
        w = int(W * wf)
        h = int(w * H / W)
        im = pop(_grab(video, t, w, h, hide, blur))
        x = (W - w - int(W * 0.02)) if not face_right else int(W * 0.02)
        if t == sec:
            x = (W - w - int(W * 0.12)) if not face_right else int(W * 0.12)
        canvas.paste(im, (x, int(H * yf)), _soft_mask(w, h, left=0.22, right=0.22, top=0.22, bottom=0.22))
    return canvas


def render_collage(video, times, title, out_jpg, portrait=False, logo=None, blur=None, suffix="", hide=None,
                   focus=None, faces=None):
    """3–5 frames in panels with a big gold Burmese title across the bottom."""
    from PIL import Image, ImageEnhance, ImageFilter
    W, H = TT_COVER if portrait else (1280, 720)
    n = len(times) if len(times) in (1, 2) else max(3, min(5, len(times)))
    times = (list(times) * 5)[:n]
    import numpy as np
    if not portrait and n >= 3:
        img = _poster(video, times, W, H, hide, blur, focus)
        if not title:
            return _cover_logo_save(img, logo, W, portrait, out_jpg)
        img = img.convert("RGBA")
        band_h = int(H * 0.5)                        # dark at the bottom, fading out upwards
        grad = Image.new("L", (1, band_h))
        for yy in range(band_h):
            grad.putpixel((0, yy), int(225 * (yy / band_h) ** 1.1))
        shade = Image.new("RGBA", (W, band_h), (12, 4, 0, 255))
        shade.putalpha(grad.resize((W, band_h)))
        img.alpha_composite(shade, (0, H - band_h))
        img = img.convert("RGB")
        _gold_title(img, title, int(W * 0.86), W // 2, int(H * 0.78), 2, int(H * 0.13), suffix)   # ~20% above the bottom
        return _cover_logo_save(img, logo, W, portrait, out_jpg)
    # photos BLEND into each other (soft overlapping edges, like Burmese movie
    # posters) instead of sitting in boxes with gold lines between them
    canvas = Image.new("RGB", (W, H), (12, 8, 6))
    ov = int(min(W, H) * 0.12)                 # how far each photo reaches into its neighbour
    for (x, y, w, h), t in zip(_layout(n, W, H, portrait), times):
        # close the layout's gaps, then reach `ov` further on every inner side
        x1, y1 = (W if x + w >= W - 12 else x + w), (H if y + h >= H - 12 else y + h)
        l = max(0, x - ov) if x > 12 else 0
        tp = max(0, y - ov) if y > 12 else 0
        r = min(W, x1 + ov) if x1 < W else W
        b = min(H, y1 + ov) if y1 < H else H
        # cropped around the person; a lower photo keeps its face below the blend with the one above
        fr = _grab(video, t, r - l, b - tp, hide, blur, (focus or {}).get(t, 0.5),
                   face=(faces or {}).get(t), fy=0.6 if tp > 0 else 0.42)
        fr = ImageEnhance.Color(ImageEnhance.Contrast(fr).enhance(1.15)).enhance(1.2)
        ax = np.ones(r - l, dtype=np.float32)
        ay = np.ones(b - tp, dtype=np.float32)
        fade = lambda k: np.linspace(0.0, 1.0, k, dtype=np.float32) ** 1.3
        if l > 0:
            k = min(2 * ov, r - l); ax[:k] *= fade(k)
        if r < W:
            k = min(2 * ov, r - l); ax[-k:] *= fade(k)[::-1]
        if tp > 0:
            k = min(2 * ov, b - tp); ay[:k] *= fade(k)
        if b < H:
            k = min(2 * ov, b - tp); ay[-k:] *= fade(k)[::-1]
        mask = Image.fromarray((np.outer(ay, ax) * 255).astype(np.uint8), "L")
        canvas.paste(fr, (l, tp), mask)
    img = canvas.convert("RGBA")
    if not title:                  # no title wanted: just the photos (and the logo)
        return _cover_logo_save(img.convert("RGB"), logo, W, portrait, out_jpg)
    # warm dark band behind the title: across the middle (between photos), or in
    # the lower part when a single photo fills the cover (the face is higher up)
    # landscape: along the bottom (faces sit in the upper part of the photos)
    cy = int(H * 0.74) if n == 1 else (int(H * 0.885) if portrait else int(H * 0.78))   # under the photos
    band_h = int(H * (0.26 if portrait else 0.4))
    band_y = min(cy - band_h // 2, H - band_h)
    grad = Image.new("L", (1, band_h))
    for yy in range(band_h):
        f = yy / band_h
        grad.putpixel((0, yy), int(190 * (1 - abs(f - 0.5) * 2) ** 0.8))
    shade = Image.new("RGBA", (W, band_h), (25, 4, 0, 255))
    shade.putalpha(grad.resize((W, band_h)))
    img.alpha_composite(shade, (0, band_y))
    img = img.convert("RGB")
    if portrait:      # big for phones, but at most 2 lines so it never buries the photos
        _gold_title(img, title, int(W * 0.92), W // 2, cy, 2, int(W * 0.15), suffix)
    else:
        _gold_title(img, title, int(W * 0.86), W // 2, cy, 2, int(H * 0.13), suffix)
    return _cover_logo_save(img, logo, W, portrait, out_jpg)


def _trim_bars(img, dark=28, min_keep=0.4):
    """Cut the black bars around a picture (an old 4:3 film inside a 16:9
    thumbnail): rows / columns that are almost all dark at the edges."""
    import numpy as np
    a = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = a.shape
    rows = (a > dark).mean(axis=1)                   # share of bright pixels per row
    cols = (a > dark).mean(axis=0)
    def edges(v):
        lit = np.where(v > 0.08)[0]                    # a logo in the bar is not the picture
        return (int(lit[0]), int(lit[-1]) + 1) if len(lit) else (0, len(v))
    y0, y1 = edges(rows)
    x0, x1 = edges(cols)
    if (y1 - y0) < h * min_keep or (x1 - x0) < w * min_keep:
        return img
    if y0 < 3 and y1 > h - 3 and x0 < 3 and x1 > w - 3:
        return img
    return img.crop((x0 + 2 if x0 else 0, y0 + 2 if y0 else 0, x1 - 2 if x1 < w else w, y1 - 2 if y1 < h else h))


def _clean_crop(w0, h0, boxes, faces, aspect):
    """The biggest part of a thumbnail with NONE of its text in it (title, "EP01",
    an "ENG SUB" ribbon…) that still shows the faces: (x0, y0, x1, y1) in pixels, or
    None. Tries the cover's own shape first, then narrower ones (shown in the middle
    over a blurred copy) — "EP01" top-left plus "ENG SUB" top-right once left only a
    thin middle strip, blown up to a blur of petals above the faces."""
    pad = [(max(0, x0 - 0.015), max(0, y0 - 0.015), min(1, x1 + 0.015), min(1, y1 + 0.015))
           for x0, y0, x1, y1 in boxes]
    # a corner badge is usually on a ribbon wider than its letters
    pad = [(max(0, x0 - 0.04), y0, min(1, x1 + 0.04), min(1, y1 + 0.04)) if y0 < 0.1 and (x0 < 0.05 or x1 > 0.95)
           else (x0, y0, x1, y1) for x0, y0, x1, y1 in pad]
    need = faces
    if faces:
        big = max(faces, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
        need_sets = [faces, [big]]
    else:
        need_sets = [[]]
    fx = ((min(f[0] for f in faces) + max(f[2] for f in faces)) / 2) if faces else 0.5
    fy = ((min(f[1] for f in faces) + max(f[3] for f in faces)) / 2) if faces else 0.4
    best, best_score = None, 0.0
    for need in need_sets:
        for k, asp in enumerate((aspect, aspect * 0.8, aspect * 0.62)):
            for step in range(33):                         # crop height: 100% down to 36%
                ch = 1.0 - step * 0.02
                cw = ch * h0 * asp / w0
                if cw > 1:
                    cw, ch = 1.0, w0 / asp / h0
                score = cw * ch * (asp / aspect) ** 0.5     # big, and as wide as the cover
                if score <= best_score:
                    break
                found = None
                for yi in range(0, int((1 - ch) * 50) + 1):
                    y = yi / 50
                    for xi in range(0, int((1 - cw) * 50) + 1):
                        x = xi / 50
                        if any(x < bx1 and x + cw > bx0 and y < by1 and y + ch > by0 for bx0, by0, bx1, by1 in pad):
                            continue
                        if any(not (f[0] >= x - 0.005 and f[2] <= x + cw + 0.005 and f[1] >= y - 0.005
                                    and f[3] <= y + ch + 0.005) for f in need):
                            continue
                        # faces centred across, and in the upper half of the crop
                        off = abs(x + cw / 2 - fx) + abs(y + ch * 0.42 - fy) * 0.5
                        if found is None or off < found[0]:
                            found = (off, x, y)
                if found:
                    best, best_score = (found[1], found[2], cw, ch), score
                    break
        if best:
            break
    if not best or best_score < 0.12:
        return None
    x, y, cw, ch = best
    return (int(x * w0), int(y * h0), int(min(1, x + cw) * w0), int(min(1, y + ch) * h0))


def thumb_cover(thumb, title, out_jpg, portrait=False, logo=None, suffix=""):
    """A cover from the source video's own thumbnail (the look the uploader chose), with
    its own text taken away cleanly: a corner badge ("ENG SUB") is cut off at the side,
    and only the picture ABOVE its old title is kept — our gold title goes on a dark
    band under it. Portrait (TikTok): the picture over a blurred, dimmed copy of itself."""
    from PIL import Image, ImageFilter, ImageOps
    src = _trim_bars(Image.open(thumb).convert("RGB"))
    try:
        import io as _io
        b = _io.BytesIO(); src.save(b, "JPEG", quality=92)
        boxes = detect_text_boxes([b.getvalue()])[0]
    except Exception:  # noqa: BLE001
        boxes = []
    w0, h0 = src.size
    try:
        import io as _io
        b2 = _io.BytesIO(); src.save(b2, "JPEG", quality=90)
        faces = detect_faces([b2.getvalue()])[0]["faces"]
    except Exception:  # noqa: BLE001
        faces = []
    W, H = TT_COVER if portrait else (1280, 720)
    box_h = int(H * (0.58 if portrait else 0.82))         # the picture's place; our title goes under it
    crop = _clean_crop(w0, h0, boxes, faces, W / box_h)
    if crop is None:                                      # text everywhere: the old way (above its title)
        lower = [bx for bx in boxes if (bx[1] + bx[3]) / 2 >= 0.5]
        keep_to = max(0.5, min([y0 for _, y0, _, _ in lower] + [1.0]) - 0.02)
        crop = (0, 0, w0, int(keep_to * h0))
    pic = src.crop(crop)
    # the clean picture as large as its place allows; a narrower one sits in the middle
    # over a blurred, dimmed copy of itself
    fh = box_h
    fw = min(W, int(pic.width * fh / pic.height))
    fh = int(pic.height * fw / pic.width) if fw == W else fh
    fg = pic.resize((fw, fh), Image.LANCZOS)
    canvas = ImageOps.fit(pic, (W, H)).filter(ImageFilter.GaussianBlur(45))
    canvas = Image.blend(canvas, Image.new("RGB", (W, H), (8, 6, 4) if portrait else (10, 6, 2)), 0.6 if portrait else 0.7)
    top = 0
    # 9:16: faces sit high, so the logo goes low, on the picture's bottom edge (above our title)
    _logo_left = ("at", fg.height) if portrait else False
    if fg is not None:
        canvas.paste(fg, ((W - fg.width) // 2, top))
        pic_bottom = top + fg.height
    else:
        pic_bottom = H
    # a soft fade from the picture into the dark band
    canvas = canvas.convert("RGBA")
    fade = int(H * 0.08)
    sh = Image.new("RGBA", (W, fade), (10, 6, 2, 0))
    a = Image.new("L", (1, fade))
    for yy in range(fade):
        a.putpixel((0, yy), int(255 * (yy / fade) ** 1.3))
    sh.putalpha(a.resize((W, fade)))
    canvas.alpha_composite(sh, (0, pic_bottom - fade))
    img = canvas.convert("RGB")
    if title:
        if portrait:
            band = H - pic_bottom                          # the whole title must fit in the band
            cy = pic_bottom + band // 2
            _gold_title(img, title, int(W * 0.9), W // 2, cy, 2, int(min(W * 0.12, band * 0.34)), suffix)
        else:
            _gold_title(img, title, int(W * 0.92), W // 2, int(H * 0.8), 2, int(H * 0.14), suffix)   # ~20% above the bottom
    return _cover_logo_save(img, logo, W, portrait, out_jpg, left=_logo_left)


def _cover_logo_save(img, logo, W, portrait, out_jpg, left=False):
    """Channel logo top-right, then save the cover."""
    from PIL import Image
    if logo and Path(logo).exists():
        img = img.convert("RGBA")
        lg = Image.open(logo).convert("RGBA")
        lw = int(W * (0.2 if portrait else 0.13))
        lg = lg.resize((lw, max(1, int(lg.height * lw / lg.width))))
        m = int(W * 0.025)
        # 9:16: TikTok's profile grid shows only the middle 3:4 — keep the logo inside it
        my = int(img.size[1] * 0.05) if portrait else m
        mx = int(W * 0.06) if portrait else m
        if isinstance(left, tuple):          # both top corners have faces: right side, on the picture's
            y = int(left[1] - lg.height * 0.75)            # bottom edge (above our title)
            img.alpha_composite(lg, (W - lw - mx, max(my, y)))
        else:
            img.alpha_composite(lg, (mx if left else W - lw - mx, my))   # left: the top-right has a face
        img = img.convert("RGB")
    img.save(out_jpg, "JPEG", quality=90)
    return Path(out_jpg)


def frames_every(video, total, step=2.0, width=256, workers=8):
    """One small JPEG every `step` seconds over the whole video, grabbed in
    parallel (a seek per frame — far faster than decoding everything)."""
    from concurrent.futures import ThreadPoolExecutor
    times = [step * (i + 0.5) for i in range(max(1, int(total // step)))]

    def grab(t):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True)
        return r.stdout if not r.returncode and r.stdout else None
    with ThreadPoolExecutor(workers) as ex:
        return times, list(ex.map(grab, times))


def sample_frames(video, start, end, count=8, width=640):
    """`count` JPEG frames (bytes) spread between start and end."""
    span = max(end - start, 1.0)
    out = []
    for i in range(count):
        t = start + span * (i + 0.5) / count
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True)
        if not r.returncode and r.stdout:
            out.append(r.stdout)
    return out


def _arrange(hero, others, layout=4):
    """Photos for the cover. 4: two of the main character on one diagonal
    (top-left, bottom-right) and two others on the other; 2: the main character
    and one other. Short of either kind, the other kind fills in."""
    def take(main, spare, k):
        got = main[:k]
        return got + [t for t in spare if t not in got][:k - len(got)]
    if layout == 2:
        h = take(hero, others, 1)
        o = take([t for t in others if t not in h], [t for t in hero if t not in h], 1)
        return (h + o)[:2]
    h = take(hero, others, 2)
    o = take([t for t in others if t not in h], [t for t in hero if t not in h], 2)
    if len(h) == 2 and len(o) == 2:
        return [h[0], o[0], o[1], h[1]]      # 2x2 order: TL, TR, BL, BR
    return (h + o)[:4]


_FACES_SRC = Path(__file__).with_name("tools") / "faces.swift"
_FACES_BIN = Path(__file__).with_name("tools") / ".faces-bin"


def detect_text_boxes(jpegs):
    """[[(x0, y0, x1, y1) of each line of text]] per image (0-1, y from the top), Apple Vision."""
    import tempfile
    if not _FACES_BIN.exists() or _FACES_BIN.stat().st_mtime < _FACES_SRC.stat().st_mtime:
        run(["swiftc", "-O", str(_FACES_SRC), "-o", str(_FACES_BIN)])
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, j in enumerate(jpegs):
            p = Path(tmp) / f"{i}.jpg"
            p.write_bytes(j)
            paths.append(str(p))
        out = run([str(_FACES_BIN), "--text", *paths]).stdout.splitlines()
    res = []
    for i in range(len(jpegs)):
        boxes = []
        for part in filter(None, (out[i] if i < len(out) else "").split(";")):
            try:
                boxes.append(tuple(float(v) for v in part.split(",")[:4]))
            except ValueError:
                pass
        res.append(boxes)
    return res


def detect_faces(jpegs):
    """Per image {"faces": [(x0, y0, x1, y1), …] (0-1, y from the top), "subtitle": bool}
    with Apple Vision — far more exact than asking the model where a face is. Compiles the
    small Swift helper on first use; [] for every image when not on a Mac."""
    import tempfile
    try:
        if not _FACES_BIN.exists() or _FACES_BIN.stat().st_mtime < _FACES_SRC.stat().st_mtime:
            run(["swiftc", "-O", str(_FACES_SRC), "-o", str(_FACES_BIN)])
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, j in enumerate(jpegs):
                p = Path(tmp) / f"{i}.jpg"
                p.write_bytes(j)
                paths.append(str(p))
            out = run([str(_FACES_BIN), *paths]).stdout.splitlines()
    except Exception as e:  # noqa: BLE001 - no Swift / not macOS: the model's guess is used
        print(f"[faces] skipped: {e}")
        return [{"faces": [], "subtitle": False} for _ in jpegs]
    res = []
    for i in range(len(jpegs)):
        line = out[i] if i < len(out) else ""
        line, _, sc = line.partition("#")
        try:
            score = float(sc) if sc else None
        except ValueError:
            score = None
        texted = line.startswith("T|")
        line = line[2:] if texted else line
        boxes, closed = [], []
        for part in filter(None, line.split(";")):
            vals = part.split(",")
            try:
                x0, y0, x1, y1 = (float(v) for v in vals[:4])
                boxes.append((x0, y0, x1, y1))
                closed.append(len(vals) > 4 and vals[4] == "c")
            except ValueError:
                pass
        res.append({"faces": boxes, "closed": closed, "subtitle": texted, "score": score})
    return res


def _cover_times_by_ai(video, start, end, chooser, samples=30):
    """Sample frames, let `chooser` (the model) pick the main character's
    close-ups and other key characters: returns (hero times, other times)."""
    span = max(end - start, 1.0)
    cands = [start + span * (i + 0.5) / samples for i in range(samples)]
    shots, times = [], []
    for t in cands:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=384:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True)
        if not r.returncode and r.stdout:
            shots.append(r.stdout)
            times.append(t)
    got = chooser(shots)
    # exact face boxes + subtitle text, from Apple Vision — on bigger copies, since a
    # subtitle in a 384-px thumbnail is too small to be read
    big = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=960:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True)
        big.append(r.stdout or b"")
    faces = detect_faces(big)
    T = lambda xs: [times[i] for i in xs]
    return {"hero": T(got[0]), "others": T(got[1]), "layout": got[2] if len(got) > 2 else 4,
            "action": T(got[3]) if len(got) > 3 else [], "tiktok": got[4] if len(got) > 4 else 2,
            "focus": {times[i]: x for i, x in (got[5] if len(got) > 5 else {}).items()},
            # the biggest face in each frame (the person the shot is about)
            "faces": {times[i]: max(fs["faces"], key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                      for i, fs in enumerate(faces) if fs["faces"]},
            "subtitled": {times[i] for i, fs in enumerate(faces) if fs["subtitle"]},
            # how good a photo each frame is (Vision aesthetics, -1…1): nicer shots first
            "score": {times[i]: fs.get("score") for i, fs in enumerate(faces) if fs.get("score") is not None},
            # the main face has its eyes shut (a blink): such shots go last
            "blink": {times[i] for i, fs in enumerate(faces) if fs["faces"] and fs.get("closed")
                      and fs["closed"][max(range(len(fs["faces"])), key=lambda k: (fs["faces"][k][2] - fs["faces"][k][0])
                                                                                    * (fs["faces"][k][3] - fs["faces"][k][1]))]}}


def render_art_cover(art, title, out_jpg, portrait=False, logo=None, suffix=""):
    """An AI-drawn picture (bytes) as the cover: dark band at the bottom with the
    big gold Burmese title in the lower third, like popular recap thumbnails."""
    from PIL import Image, ImageEnhance, ImageOps
    import io
    W, H = TT_COVER if portrait else (1280, 720)
    img = ImageOps.fit(Image.open(io.BytesIO(art)).convert("RGB"), (W, H), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.05).convert("RGBA")
    band_h = int(H * (0.38 if portrait else 0.48))
    grad = Image.new("L", (1, band_h))
    for yy in range(band_h):
        grad.putpixel((0, yy), int(215 * (yy / band_h) ** 1.4))
    shade = Image.new("RGBA", (W, band_h), (10, 4, 0, 255))
    shade.putalpha(grad.resize((W, band_h)))
    bottom = H - (0 if portrait else 0)            # TikTok buttons cover the very bottom
    img.alpha_composite(shade, (0, bottom - band_h))
    if portrait:
        img.alpha_composite(Image.new("RGBA", (W, H - bottom), (10, 4, 0, 215)), (0, bottom))
    img = img.convert("RGB")
    if portrait:
        _gold_title(img, title, int(W * 0.9), W // 2, int(H * 0.71), 3, int(W * 0.13), suffix)
    else:
        _gold_title(img, title, int(W * 0.92), W // 2, int(H * 0.8), 2, int(H * 0.14), suffix)
    if logo and Path(logo).exists():
        img = img.convert("RGBA")
        lg = Image.open(logo).convert("RGBA")
        lw = int(W * (0.2 if portrait else 0.13))
        lg = lg.resize((lw, max(1, int(lg.height * lw / lg.width))))
        m = int(W * 0.025)
        img.alpha_composite(lg, (W - lw - m, m))
        img = img.convert("RGB")
    img.save(out_jpg, "JPEG", quality=90)
    return Path(out_jpg)


def part_suffix(i, n, episode=None):
    """What follows the title on part i of n: " - 1", " - 2"… and " - End" on the last.
    For an episode of a series (episode=3): " - 3" when not split, else " - 3/1", " - 3/2"…
    and " - 3/End" on the episode's last part (file names keep "-": "/" can't be in them)."""
    if episode:
        return f" - {episode}" if n <= 1 else (f" - {episode}/End" if i == n else f" - {episode}/{i}")
    if n <= 1:
        return ""
    return " - End" if i == n else f" - {i}"


def make_covers(video, title, out_dir, start, end, portrait=False, logo=None, blur=None, parts=1, episode=None,
                chooser=None, log=None, art=None, hide=None, picks=None, single=False):
    """The film's cover: 4 photos in a 2x2 grid with the title in the middle.
    With `chooser` (Gemini) the first photo is the main character and the rest
    show the main scenes / other characters; otherwise the sharpest frames.
    parts > 1: one cover per part, same photos, titled "title - 1", "title - 2"…"""
    out_dir = Path(out_dir)
    if art:                       # an AI-drawn picture: same picture for every part
        names = ["cover1.jpg"] if parts <= 1 else [f"cover{i}.jpg" for i in range(1, parts + 1)]
        return [render_art_cover(art, title, out_dir / n, portrait, logo,
                                 part_suffix(i, parts, episode)) for i, n in enumerate(names, 1)]
    pick, focus, boxes = None, {}, {}
    if chooser:
        try:
            # the YouTube and the TikTok covers use the SAME photos (one choice, made once):
            # `picks` is a dict shared by both calls
            ai = (picks or {}).get("ai") or _cover_times_by_ai(video, start, end, chooser)
            if picks is not None:
                picks["ai"] = ai
            hero, others, action = ai["hero"], ai["others"], ai["action"]
            boxes = {**ai["focus"], **ai.get("faces", {})}   # Vision's exact boxes win over the model's
            # the model often picks close-ups despite being asked not to: put the shots
            # whose face is a good size (15-45% of the frame's height, not at an edge) first
            def good(t):
                b = boxes.get(t)
                if not b:
                    return 3 if ai.get("faces") else 1   # Vision found no face here: last
                fh, cx = b[3] - b[1], (b[0] + b[2]) / 2
                return 0 if 0.15 <= fh <= 0.45 and 0.15 <= cx <= 0.85 and b[1] > 0.03 else 2
            subbed = ai.get("subtitled", set())          # frames with a subtitle line on them go last
            blink = ai.get("blink", set())               # …and so do faces with their eyes shut
            sc = ai.get("score", {})                     # then the nicer photo first (not a snapshot look)
            ugly = lambda t: sc.get(t, 0.0) < -0.2       # a clearly poor / snapshot-like shot
            hero = sorted(hero, key=lambda t: (t in blink, t in subbed, ugly(t), good(t), -sc.get(t, 0.0)))
            action = sorted(action, key=lambda t: (t in blink, t in subbed, ugly(t), -sc.get(t, 0.0)))
            others = sorted(others, key=lambda t: (t in blink, t in subbed, ugly(t), -sc.get(t, 0.0)))
            focus = {t: (b[0] + b[2]) / 2 for t, b in boxes.items()}   # crop around the face
            if single:            # simple: the main character big and clear, plus (usually) one more photo
                big = lambda t: -((boxes.get(t) or (0, 0, 0, 0))[3] - (boxes.get(t) or (0, 0, 0, 0))[1])
                clean = lambda ts: [t for t in ts if t not in blink and t not in subbed]
                pool = clean(hero) or hero or action or others
                # a clearly visible face (not tiny), eyes open, the nicer photo first
                pick = sorted(pool[:6], key=lambda t: (good(t) > 0 and big(t) > -0.12, ugly(t), -sc.get(t, 0.0)))[:1]
                # the 2nd photo: another character with a clear face, else the main action —
                # only a decent one (a poor 2nd photo is worse than one big photo)
                # it must show a face (Apple Vision) of a fair size — not a back, a hand or a crowd
                # — and a DIFFERENT person: the other lead (e.g. the heroine) first, never the main
                # character a second time (two photos of the hero looked like a mistake)
                faced = lambda t: bool(boxes.get(t)) and (boxes[t][3] - boxes[t][1]) >= 0.08
                cands = [t for t in clean(others) if t not in hero and t not in pick and not ugly(t) and faced(t)]
                second = cands[:1]                       # others[0…] = the 2nd character's best shots
                if not second:
                    second = [t for t in clean(action) if t not in hero and t not in pick and not ugly(t) and faced(t)][:1]
                pick += second
                layout = len(pick)
            elif portrait:        # TikTok: the main character's face on top, the movie's main action below
                top = (hero or action or others)[:1]   # (one action photo alone often had no face in it)
                bot = [t for t in action + others + hero if t not in top][:1]
                pick = top + bot
                layout = len(pick)
            else:                 # YouTube poster: main character, the action, 2nd and 3rd characters
                seen = []
                for group in (hero[:1], action, others, hero[1:]):
                    for t in group:
                        if t not in seen:
                            seen.append(t)
                            break
                rest = [t for t in others + hero + action if t not in seen]
                pick = (seen + rest)[:4]
                layout = len(pick)
            if log:
                log(f"cover photos picked by AI: {len(hero)} of the main character, {len(action)} of the "
                    f"main action, {len(others)} others · {layout}-photo cover")
        except Exception as e:  # noqa: BLE001 - fall back to the frame scores
            if log:
                log(f"AI cover pick failed ({str(e)[:120]}) — using the sharpest frames")
    if not pick:
        pick = pick_cover_times(video, start, end, 2 if single or portrait else 4, samples=36, tmp=out_dir)
    if parts <= 1:
        return [render_collage(video, pick, title, out_dir / "cover1.jpg", portrait, logo, blur, hide=hide,
                               focus=focus, suffix=part_suffix(1, 1, episode), faces=boxes if single else None)]
    return [render_collage(video, pick, title, out_dir / f"cover{i}.jpg", portrait, logo, blur,
                           suffix=part_suffix(i, parts, episode), hide=hide, focus=focus,
                           faces=boxes if single else None) for i in range(1, parts + 1)]


# ---- recap version: cut the scenes that go with each narration beat ----

def trim_copy(src, start, end, out):
    """Cut [start, end] out of src without re-encoding (seconds, not minutes,
    for a 2 h film). The cut starts at the first keyframe at/after `start`, so
    nothing from before it (an ad) slips in. Returns the real start used."""
    k = start
    if start > 0:
        p = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                 "-read_intervals", f"{start:.2f}%+30", "-show_entries", "frame=pts_time",
                 "-of", "csv=p=0", str(src)])
        keys = sorted(float(x) for x in p.stdout.split() if x.strip().replace(".", "", 1).isdigit())
        after = [t for t in keys if t >= start - 0.05]
        if after:
            k = after[0]
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{k:.3f}", "-i", str(src)]
    if end:
        cmd += ["-t", f"{end - k:.3f}"]
    run(cmd + ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", str(out)])
    return k


def picture_area(video, start, end, count=12):
    """(top, bottom) fractions of the frame where the picture actually moves —
    uploads often put the film between still bars (black or any colour, often
    with text). Rows that stay the same in every sampled frame are bars.
    (0, 1) when there are none."""
    import io
    import numpy as np
    from PIL import Image
    shots = [np.asarray(Image.open(io.BytesIO(j)).convert("L").resize((96, 170)), dtype=np.float32)
             for j in sample_frames(video, start, end, count, width=192)]
    if len(shots) < 4:
        return 0.0, 1.0
    rows = np.stack(shots).std(axis=0).mean(axis=1)      # how much each row changes over time
    moving = np.where(rows > max(6.0, rows.max() * 0.15))[0]
    if not len(moving):
        return 0.0, 1.0
    top, bot = moving[0] / len(rows), (moving[-1] + 1) / len(rows)
    if bot - top < 0.15 or (top < 0.03 and bot > 0.97):      # (a wide film in 9:16 is ~24% tall)
        return 0.0, 1.0
    return round(float(top), 4), round(float(bot), 4)


def speech_spans(utterances, pad=1.0, join=3.0):
    """Stretches of the film where people talk (from the transcript), merged.
    Music-only moments (moody montages, dances without lyrics, silent ads)
    fall between them."""
    spans = []
    for u in sorted(utterances, key=lambda u: u["start"]):
        a, b = max(0.0, u["start"] - pad), u["end"] + pad
        if spans and a - spans[-1][1] <= join:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return [tuple(x) for x in spans]


def footage(start, dur, spans, min_piece=1.5):
    """[(start, length)] adding up to dur, taken from talking stretches from
    `start` on: a recap beat longer than its scene skips the music-only part
    that follows and goes on with the next talking scene instead."""
    if not spans:
        return [(start, dur)]
    out, need, t = [], dur, start
    for a, b in spans:
        if b <= t:
            continue
        s = max(a, t)
        avail = b - s
        if avail >= min(min_piece, need):
            take = min(avail, need)
            out.append((s, take))
            need -= take
            if need <= 0.05:
                return out
        t = b
    out.append((t, need))           # past the last talk: just carry on
    return out


def _spread(timeline, need, piece=3.0, roomy=1.3):
    """timeline: [(a, b)] real-time stretches in film order. Take `need` seconds from
    them. When there is much more than needed, take short pieces spread EVENLY across
    the whole stretch (not only its beginning): the narration walks through the beat's
    scene in order, so its later sentences meet the later pictures."""
    total = sum(b - a for a, b in timeline)
    if total < need * roomy:
        k, L, g = 1, need, 0.0                         # contiguous from the start
    else:
        k = max(1, int(need // piece))
        L, g = need / k, (total - need) / k
    out = []
    for i in range(k):
        o, left = i * (L + g) + g / 2, L                 # offset on the joined timeline
        for a, b in timeline:
            ln = b - a
            if o >= ln:
                o -= ln
                continue
            take = min(ln - o, left)
            out.append((a + o, take))
            left -= take
            o = 0.0
            if left <= 1e-3:
                break
        if left > 1e-3:                                # ran off the end: last bit from the end
            out.append((max(timeline[0][0], timeline[-1][1] - left), left))
    # drop slivers (a cut under 0.4 s looks like a glitch): give their time to a neighbour
    clean = []
    for a, ln in out:
        if ln < 0.4 and clean:
            pa, pl = clean[-1]
            clean[-1] = (pa, pl + ln)
        else:
            clean.append((a, ln))
    return clean


def beat_footage(start, end, need, spans, max_slow=1.35):
    """Scenes for one recap beat, kept INSIDE that beat's own part of the film
    [start, end) so the picture matches what is being said: the talking parts of that
    stretch first; when they are shorter than the narration, its quiet parts too; when
    even the whole stretch is shorter, it is played a little slower (up to max_slow),
    and only then does it reach a little past `end`. A long stretch is sampled evenly
    from start to end. Returns [(start, len[, out_len])]."""
    end = max(end, start + 0.5)
    talk = [(max(a, start), min(b, end)) for a, b in spans if b > start and a < end] or [(start, end)]
    talk = [(a, b) for a, b in talk if b - a > 0.05] or [(start, end)]
    if sum(b - a for a, b in talk) >= need:          # enough talking in its own scene
        return _spread(talk, need)
    whole = end - start
    if whole >= need:                                 # the quiet moments of that scene fill the rest
        return _spread([(start, end)], need)
    if whole * max_slow >= need:                      # the whole scene, a little slower
        return [(start, whole, need)]
    return [(start, whole, whole * max_slow), (end, need - whole * max_slow)]   # then carry on past it


def game_moments(video, win=0.5):
    """🎮 The action in a gameplay video, from its sound: fights, skills, kills and the
    announcer ("Double Kill!", "Savage!") are the loudest stretches. Returns
    [(time, loudness)] of the peaks, loudest first."""
    import numpy as np
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "8000", str(wav)])
        import wave
        with wave.open(str(wav), "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = int(8000 * win)
    k = len(a) // n
    if k < 4:
        return []
    rms = np.sqrt((a[:k * n].reshape(k, n) ** 2).mean(axis=1) + 1e-9)
    db = 20 * np.log10(rms)
    smooth = np.convolve(db, np.ones(3) / 3, mode="same")
    thr = max(np.percentile(smooth, 75), smooth.mean() + 0.6 * smooth.std())
    peaks = []
    for i in range(1, k - 1):
        if smooth[i] >= thr and smooth[i] >= smooth[i - 1] and smooth[i] >= smooth[i + 1]:
            peaks.append(((i + 0.5) * win, float(smooth[i])))
    # one peak per 3 s (the loudest)
    peaks.sort(key=lambda x: -x[1])
    kept = []
    for t, v in peaks:
        if all(abs(t - u) >= 3.0 for u, _ in kept):
            kept.append((t, v))
    return kept


def game_edit(src, out, music=None, cut=True, slowmo=True, logo=None, logo_size=0.12, game_vol=0.35, log=None,
              moments=None, min_len=62.0, target=None):
    """🎮 Edit a gameplay highlight: keep the action (quiet walking around is cut), the 3
    biggest moments replayed in slow motion with a punch-in zoom, optional background
    music over the game's own (turned-down) sound, the logo. Returns the edited length."""
    import shutil as _sh
    src, out = Path(src), Path(out)
    total = probe_duration(src)
    # moments: [(time, score)] from the AI looking at the screen (kills, team fights…), best first;
    # else the loudest stretches of the game's sound
    peaks = moments if moments else game_moments(src)

    def keep_spans(pad_a, pad_b, pts):
        spans = sorted((max(0.0, t - pad_a), min(total, t + pad_b)) for t, _ in pts)
        merged = []
        for a, b in spans:
            if merged and a - merged[-1][1] < 2.5:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return merged

    # the stretches to keep: the build-up before each moment and a little after it — at least
    # `min_len` s in all (TikTok counts 1 min+ videos): wider margins, then more moments, else all
    merged = [[0.0, total]]
    if cut and peaks and total > min_len + 10:
        # a highlight, not the whole match: the best moments first, added until about `target` s
        # (1.5-3 min; a 20-min match became 10 min when every team fight was kept)
        goal = target or max(min_len, min(180.0, total * 0.15))
        best = sorted(peaks, key=lambda x: -x[1])
        for pa, pb in ((5.0, 3.0), (7.0, 4.0), (10.0, 6.0), (14.0, 8.0)):
            chosen, m = [], []
            for pt in best:
                chosen.append(pt)
                m = keep_spans(pa, pb, chosen)
                if sum(b - a for a, b in m) >= goal:
                    break
            if sum(b - a for a, b in m) >= min_len:
                merged = m
                break
    big = sorted(sorted(peaks, key=lambda x: -x[1])[:3]) if slowmo else []
    # clips in order: normal pieces, and the big moments as an extra slow-motion replay right after
    clips = []
    for a, b in merged:
        clips.append(("n", a, b))
        for t, _ in big:
            if a <= t <= b:
                clips.append(("s", max(a, t - 1.2), min(b, t + 0.8)))
    if log:
        kept = sum(b - a for _, a, b in clips if _ == "n")
        log(f"🎮 keeping {kept:.0f}s of {total:.0f}s (the action), {len(big)} slow-motion replay(s)")
    tmp = out.with_name(out.stem + "_clips")
    tmp.mkdir(exist_ok=True)
    w, h = video_size(src)
    hw = _has_encoder("h264_videotoolbox")
    enc = (["-c:v", "h264_videotoolbox", "-q:v", "62", "-allow_sw", "1"] if hw
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"])
    has_a = has_audio_stream(src)
    parts = []
    try:
        for i, (kind, a, b) in enumerate(clips):
            p = tmp / f"c{i:04d}.mp4"
            ins = ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
            if not has_a:
                ins += ["-f", "lavfi", "-t", f"{(b - a) * (2 if kind == 's' else 1):.3f}",
                        "-i", "anullsrc=r=44100:cl=stereo"]
            if kind == "s":       # slow motion (half speed) with a 15% punch-in zoom
                vf = (f"setpts=2.0*PTS,scale={int(w * 1.15) // 2 * 2}:{int(h * 1.15) // 2 * 2},"
                      f"crop={w}:{h},fps=30,format=yuv420p")
                af = "atempo=0.5"
            else:
                vf, af = f"scale={w}:{h},fps=30,format=yuv420p", "anull"
            amap = ["-map", "0:a:0", "-af", af + ",aresample=44100,aformat=channel_layouts=stereo"] if has_a else ["-map", "1:a:0"]
            run(["ffmpeg", "-y", "-v", "error", *ins, "-map", "0:v:0", "-vf", vf, *amap, *enc,
                 "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-video_track_timescale", "90000",
                 "-shortest", str(p)])
            parts.append(p)
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        joined = tmp / "joined.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
        length = probe_duration(joined)
        ins = ["-i", str(joined)]
        fc = []
        if music:
            ins += ["-stream_loop", "-1", "-i", str(music)]
            fo = max(0.0, length - 2.5)
            fc.append(f"[0:a]volume={game_vol}[g];[1:a]volume=0.75,afade=t=in:d=1.5,afade=t=out:st={fo:.2f}:d=2.5[m];"
                      f"[g][m]amix=inputs=2:duration=first:normalize=0[a]")
        vmap = "0:v:0"
        if logo and Path(logo).exists():
            ins += ["-i", str(logo)]
            li = 2 if music else 1
            fc.append(f"[{li}:v]scale={max(60, int(w * logo_size))}:-1[lg];[0:v][lg]overlay=W-w-W*0.025:H*0.03[v]")
            vmap = "[v]"
        cmd = ["ffmpeg", "-y", "-v", "error", *ins]
        if fc:
            cmd += ["-filter_complex", ";".join(fc)]
        cmd += ["-map", vmap, "-map", "[a]" if music else "0:a:0"]
        cmd += enc if vmap == "[v]" else ["-c:v", "copy"]
        cmd += ["-c:a", "aac", "-b:a", "192k", "-t", f"{length:.3f}", "-movflags", "+faststart", str(out)]
        run(cmd)
        return length
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


def trim_silence(path):
    """Cut the silence a TTS clip starts and ends with (some voices add a second or
    more): no long quiet moments between recap beats, and the voice starts on cue."""
    path = Path(path)
    tmp = path.with_name(path.stem + ".trim" + path.suffix)
    sil = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    try:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-af",
             f"{sil},areverse,{sil},areverse", str(tmp)])
        if tmp.exists() and tmp.stat().st_size > 1000:
            tmp.replace(path)
    except Exception:  # noqa: BLE001 - the untrimmed clip is fine too
        tmp.unlink(missing_ok=True)


def build_recap_video(src, clips, out_mp4, tmp, progress=None):
    """clips: [(source_start, duration)] in order. Each is cut (re-encoded,
    no audio, same size/fps so they join cleanly) and the pieces joined.
    Returns the joined video's duration."""
    from concurrent.futures import ThreadPoolExecutor
    tmp = Path(tmp)
    tmp.mkdir(exist_ok=True)
    total_src = probe_duration(src)
    hw = _has_encoder("h264_videotoolbox")
    enc = (["-c:v", "h264_videotoolbox", "-q:v", "65", "-allow_sw", "1"] if hw
           else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
    fps = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=avg_frame_rate", "-of", "csv=p=0", str(src)]).stdout.strip() or "30"
    try:
        num, _, den = fps.partition("/")
        rate = float(num) / float(den or 1)
        if not 5 <= rate <= 120:
            raise ValueError
    except ValueError:
        fps, rate = "30", 30.0
    # exact frame counts on a running total: every clip rounded up by a fraction of a
    # frame added up over hundreds of clips (seconds of drift: the picture fell behind
    # the narration)
    want = [c[2] if len(c) > 2 else c[1] for c in clips]
    frames, acc, prev = [], 0.0, 0
    for w in want:
        acc += w
        f = round(acc * rate)
        frames.append(max(1, f - prev))
        prev += frames[-1]
    done = [0]

    def cut(item):
        i, clip = item
        start, dur = clip[0], clip[1]
        slow = (clip[2] / dur) if len(clip) > 2 and dur > 0 else 1.0     # played slower to fill its beat
        start = max(0.0, min(start, total_src - dur - 0.05))
        out = tmp / f"clip{i:05d}.mp4"
        pts = f"setpts={slow:.4f}*PTS," if slow > 1.001 else ""
        # -t before -i limits what is READ (a slowed clip then really plays longer —
        # as an output option it cut the slowed clip back to its original length);
        # -frames:v gives the exact length, tpad covers a clip at the film's very end
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur + 0.25:.3f}", "-i", str(src),
             "-an", "-vf", f"{pts}scale=trunc(iw/2)*2:trunc(ih/2)*2,fps={fps},tpad=stop_mode=clone:stop_duration=2,"
             f"format=yuv420p", "-frames:v", str(frames[i]),
             *enc, "-video_track_timescale", "90000", str(out)])
        done[0] += 1
        if progress:
            progress(done[0], len(clips))
        return out

    with ThreadPoolExecutor(4) as ex:
        parts = list(ex.map(cut, enumerate(clips)))
    lst = tmp / "clips.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(out_mp4)])
    import shutil as _sh
    _sh.rmtree(tmp, ignore_errors=True)
    return probe_duration(out_mp4)
