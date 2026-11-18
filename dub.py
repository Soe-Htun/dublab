"""Dubbing pipeline: extract audio -> transcribe -> translate -> TTS -> mux."""
import os
import re
import subprocess
from pathlib import Path

SPEAK_SPEED = float(os.environ.get("DUBLAB_SPEAK_SPEED") or 1.15)   # every voice line is sped up this much (1.0 = TTS's own pace, felt slow)
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


def _vote_language(wav, name, spots=(0.15, 0.3, 0.45, 0.6, 0.75, 0.9), clip=30):
    """Whisper guesses the language from the first 30 s only — a Burmese ad or
    channel intro before an Indian film made it 'my'. Listen at several points
    through the video instead and add up the votes."""
    import tempfile
    from faster_whisper import decode_audio
    model = _get_model(name)
    total = probe_duration(wav)
    if total < clip * 4:
        return None                      # short clip: whisper's own guess is fine
    votes = {}
    with tempfile.TemporaryDirectory() as tmp:
        for f in spots:
            piece = Path(tmp) / "spot.wav"
            try:
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{total * f:.1f}", "-t", str(clip),
                     "-i", str(wav), "-ac", "1", "-ar", "16000", str(piece)])
                lang, prob, _ = model.detect_language(decode_audio(str(piece)), vad_filter=True)
            except Exception:  # noqa: BLE001 - e.g. a silent spot
                continue
            votes[lang] = votes.get(lang, 0.0) + prob
    if not votes:
        return None
    best = max(votes, key=votes.get)
    print(f"[whisper] language votes: { {k: round(v, 2) for k, v in votes.items()} } → {best}")
    return best


def _transcribe(wav, lang_hint, progress, name):
    global _mlx_broken
    if not lang_hint:
        try:
            lang_hint = _vote_language(wav, name)
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
        temperature=0.0, condition_on_previous_text=False,
        vad_parameters=dict(min_silence_duration_ms=400))
    segs = []
    for s in segments:  # lazy generator: decoding happens while we iterate
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


def synthesize(text, voice, out_path):
    """TTS one utterance at SPEAK_SPEED (the TTS's own pace is a bit slow)."""
    _synthesize_raw(text, voice, out_path)
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
        if not utterances[i].get("audio"):
            synthesize(utterances[i]["text"], voice, tmpdir / f"u{i}.mp3")
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
_MY_BREAK = re.compile(r"(?<!\u1039)(?=[\u1000-\u102A\u103F\u104C-\u104F](?![\u103A\u1039]))")


def _split_wide(draw, word, font, max_w):
    """Break one over-wide word at Burmese syllable starts (or characters)."""
    parts = [p for p in _MY_BREAK.split(word) if p] or list(word)
    if len(parts) == 1:
        parts = list(word)
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
    words wider than a line are broken at syllable starts."""
    words = []
    for word in text.split():
        if _tlen(draw, word, font) > max_w:
            words += _split_wide(draw, word, font, max_w)
        else:
            words.append(word)
    lines, cur = [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if cur and _tlen(draw, trial, font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


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
    vy = max(420, int(880 - hv / 2)) // 2 * 2
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


def render_frames(title, windows, out_dir, layout, logo=None, extra=30.0):
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
    files = []
    for i in range(1, n + 1):
        img = base.copy().convert("RGB")
        title_cy = (top_y + vy - 30) // 2
        if title:                            # "Jaat - 1", "Jaat - 2"… like the covers
            _gold_title(img, title, int(W * 0.9), W // 2, title_cy, 2, 104,
                        f" - {i}" if n > 1 else "")
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


def mux(src_video, dub_wav, out_mp4, bg_wav=None, bg_volume=0.8, logo=None,
        captions=None, blur=None, keyframes=None, lead=0.0, tail=0.0, intro_img=None, hide=None,
        frame=None):
    """Combine picture + dub track.

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
        m = max(8, int(vw * 0.02))                 # margin from the edges
        x = f"{m}" if "left" in logo.get("position", "") else f"W-w-{m}"
        y = f"H-h-{m}" if "bottom" in logo.get("position", "") else f"{m}"
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
    draw = bool(logo or captions or blur or intro_img or hide or frame)

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
                    # strong blur (radius grows with the band, chroma too) so the old letters
                    # can't be read through it
                    # (done on a 1/8-size copy of the band and scaled back: the same look,
                    # far less work than a big blur at full size)
                    parts.append(f"[bb]crop=iw:ih*{bh:.4f}:0:{by},split[bc][bref];"
                                 "[bc]scale=trunc(iw/12)*2:trunc(ih/12)*2,"
                                 "boxblur=luma_radius='min(h/4,4)':luma_power=2:"
                                 "chroma_radius='min(ch/4,2)':chroma_power=2[bs];"
                                 "[bs][bref]scale2ref=flags=bicubic[bz][bdrop];[bdrop]nullsink")
                    parts.append(f"[bm][bz]overlay=0:H*{1 - b0 - bh:.4f}[s1]")
                    cur = "s1"
                if hide:          # the video's own logos: erased (delogo fills each box in from
                    sw, sh = video_size(src_video)            # its edges) — a blur left them readable
                    sw, sh = sw // 2 * 2, sh // 2 * 2
                    dl = []
                    for x0, y0, x1, y1 in hide:     # (own names: x / y are the logo's spot)
                        dx, dy = max(1, int(sw * x0)), max(1, int(sh * y0))
                        dw = min(int(sw * x1), sw - 2) - dx
                        dh = min(int(sh * y1), sh - 2) - dy
                        if dw > 4 and dh > 4:
                            dl.append(f"delogo=x={dx}:y={dy}:w={dw}:h={dh}")
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
    if _has_encoder("h264_videotoolbox"):
        try:
            run(_cmd(copy_video=False, encoder="h264_videotoolbox"))
            return
        except RuntimeError as e:
            print(f"[mux] hardware encoder failed ({e}); using libx264")
    run(_cmd(copy_video=False))


def split_points(cues, total, minutes, min_tail=60.0, window=90.0):
    """Cut times about every `minutes`, moved into the nearest pause between
    captions (within `window` s) so no sentence is cut in half. A last part
    shorter than `min_tail` s is merged into the one before."""
    step = minutes * 60.0
    if not minutes or total <= step + min_tail:
        return []
    gaps = [(b, a2) for (_, b, _), (a2, _, _) in zip(cues, cues[1:]) if a2 - b > 0.25]
    cuts, target = [], step
    while total - target >= min_tail:
        near = [g for g in gaps if abs((g[0] + g[1]) / 2 - target) <= window
                and (g[0] + g[1]) / 2 > (cuts[-1] if cuts else 0) + step / 2]
        t = min(((g[0] + g[1]) / 2 for g in near), key=lambda m: abs(m - target)) if near else target
        cuts.append(round(t, 3))
        target = t + step
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


def render_cover(video, t, title, out_jpg, portrait=False, logo=None, blur=None):
    """Frame at t, cropped to 1280x720 (or 1080x1920), darkened at the
    bottom, with the Burmese title in big outlined yellow/white letters."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    W, H = (1080, 1920) if portrait else (1280, 720)
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


def _grab(video, t, w, h, hide=None, blur=None):
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
    g = np.asarray(img.convert("L"), dtype=np.float32)
    rows, cols = np.where(g.mean(1) > 18)[0], np.where(g.mean(0) > 18)[0]
    if len(rows) > g.shape[0] * 0.4 and len(cols) > g.shape[1] * 0.4:
        img = img.crop((cols[0], rows[0], cols[-1] + 1, rows[-1] + 1))
    return ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.4))


def _layout(n, W, H, portrait):
    """Panel boxes (x, y, w, h) for n = 2..5 photos. 4 = an even 2x2 grid;
    2 = side by side (landscape) or one above the other (portrait)."""
    g = 8
    if n == 2:
        g = 6
        if portrait:
            h2 = (H - g) // 2
            return [(0, 0, W, h2), (0, h2 + g, W, H - h2 - g)]
        w2 = (W - g) // 2
        return [(0, 0, w2, H), (w2 + g, 0, W - w2 - g, H)]
    if n == 4 and not portrait:
        g = 6
        w2, h2 = (W - g) // 2, (H - g) // 2
        return [(0, 0, w2, h2), (w2 + g, 0, W - w2 - g, h2),
                (0, h2 + g, w2, H - h2 - g), (w2 + g, h2 + g, W - w2 - g, H - h2 - g)]
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


def render_collage(video, times, title, out_jpg, portrait=False, logo=None, blur=None, suffix="", hide=None):
    """3–5 frames in panels with a big gold Burmese title across the bottom."""
    from PIL import Image, ImageEnhance, ImageFilter
    W, H = (1080, 1920) if portrait else (1280, 720)
    n = 2 if len(times) == 2 else max(3, min(5, len(times)))
    times = (list(times) * 5)[:n]
    canvas = Image.new("RGB", (W, H), (236, 178, 60) if n in (2, 4) else (12, 8, 6))   # 2 / 2x2: thin gold lines
    for (x, y, w, h), t in zip(_layout(n, W, H, portrait), times):
        fr = _grab(video, t, w, h, hide, blur)     # old captions cut off, old logos blurred
        fr = ImageEnhance.Color(ImageEnhance.Contrast(fr).enhance(1.15)).enhance(1.2)
        canvas.paste(fr, (x, y))
    img = canvas.convert("RGBA")
    # warm dark band behind the title, across the middle
    band_h = int(H * (0.26 if portrait else 0.4))
    band_y = H // 2 - band_h // 2
    grad = Image.new("L", (1, band_h))
    for yy in range(band_h):
        f = yy / band_h
        grad.putpixel((0, yy), int(190 * (1 - abs(f - 0.5) * 2) ** 0.8))
    shade = Image.new("RGBA", (W, band_h), (25, 4, 0, 255))
    shade.putalpha(grad.resize((W, band_h)))
    img.alpha_composite(shade, (0, band_y))
    img = img.convert("RGB")
    if portrait:
        _gold_title(img, title, int(W * 0.9), W // 2, H // 2, 3, int(W * 0.17), suffix)   # big: phones show it small
    else:
        _gold_title(img, title, int(W * 0.86), W // 2, H // 2, 2, int(H * 0.13), suffix)
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
    hero, others = got[0], got[1]
    layout = got[2] if len(got) > 2 else 4
    return [times[i] for i in hero], [times[i] for i in others], layout


def render_art_cover(art, title, out_jpg, portrait=False, logo=None, suffix=""):
    """An AI-drawn picture (bytes) as the cover: dark band at the bottom with the
    big gold Burmese title in the lower third, like popular recap thumbnails."""
    from PIL import Image, ImageEnhance, ImageOps
    import io
    W, H = (1080, 1920) if portrait else (1280, 720)
    img = ImageOps.fit(Image.open(io.BytesIO(art)).convert("RGB"), (W, H), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.05).convert("RGBA")
    band_h = int(H * (0.38 if portrait else 0.48))
    grad = Image.new("L", (1, band_h))
    for yy in range(band_h):
        grad.putpixel((0, yy), int(215 * (yy / band_h) ** 1.4))
    shade = Image.new("RGBA", (W, band_h), (10, 4, 0, 255))
    shade.putalpha(grad.resize((W, band_h)))
    bottom = H - (int(H * 0.16) if portrait else 0)            # TikTok buttons cover the very bottom
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


def make_covers(video, title, out_dir, start, end, portrait=False, logo=None, blur=None, parts=1,
                chooser=None, log=None, art=None, hide=None):
    """The film's cover: 4 photos in a 2x2 grid with the title in the middle.
    With `chooser` (Gemini) the first photo is the main character and the rest
    show the main scenes / other characters; otherwise the sharpest frames.
    parts > 1: one cover per part, same photos, titled "title - 1", "title - 2"…"""
    out_dir = Path(out_dir)
    if art:                       # an AI-drawn picture: same picture for every part
        names = ["cover1.jpg"] if parts <= 1 else [f"cover{i}.jpg" for i in range(1, parts + 1)]
        return [render_art_cover(art, title, out_dir / n, portrait, logo,
                                 "" if parts <= 1 else f" - {i}") for i, n in enumerate(names, 1)]
    pick = None
    if chooser:
        try:
            hero, others, layout = _cover_times_by_ai(video, start, end, chooser)
            if portrait:
                layout = 2        # TikTok cover: two big photos, one above the other
            if log:
                log(f"cover photos picked by AI: {len(hero)} of the main character, {len(others)} others"
                    f" · {layout}-photo cover")
            if hero or others:
                pick = _arrange(hero, others, layout)
        except Exception as e:  # noqa: BLE001 - fall back to the frame scores
            if log:
                log(f"AI cover pick failed ({str(e)[:120]}) — using the sharpest frames")
    if not pick:
        pick = pick_cover_times(video, start, end, 2 if portrait else 4, samples=36, tmp=out_dir)
    if parts <= 1:
        return [render_collage(video, pick, title, out_dir / "cover1.jpg", portrait, logo, blur, hide=hide)]
    return [render_collage(video, pick, title, out_dir / f"cover{i}.jpg", portrait, logo, blur,
                           suffix=f" - {i}", hide=hide) for i in range(1, parts + 1)]


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
    done = [0]

    def cut(item):
        i, (start, dur) = item
        start = max(0.0, min(start, total_src - dur - 0.05))
        out = tmp / f"clip{i:05d}.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
             "-an", "-vf", f"scale=trunc(iw/2)*2:trunc(ih/2)*2,fps={fps},format=yuv420p",
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
