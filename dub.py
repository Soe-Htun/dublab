"""Dubbing pipeline: extract audio -> transcribe -> translate -> TTS -> mux."""
import os
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
    try:
        return EDGE_PRIMARY_FLAG.exists()
    except OSError:
        return False


def configure_tts(keys):
    """Configure Gemini TTS key rotation. Call once at startup."""
    _tts_configure(keys)


_model = None


def transcribe(wav, lang_hint=None):
    """Return ([{start, end, text}], language).

    lang_hint: 'en', 'my', 'zh'… or None to let whisper detect the
    language; the returned language is the hint or the detected code."""
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(os.environ.get("DUBLAB_WHISPER_MODEL") or "base",
                              device="cpu", compute_type="int8")
    segments, info = _model.transcribe(
        str(wav), language=lang_hint, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400))
    segs = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
            for s in segments if s.text and s.text.strip()]
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


def _synth_edge(text, voice, out_path):
    import asyncio
    import edge_tts
    import edge_tts.communicate as _comm
    import edge_tts.constants as _C

    # 2026-10: Microsoft's classic speech.platform.bing.com endpoint answers
    # 403 in many regions. Use the newer api.msedgeservices.com endpoint for
    # the same voices (token as Ocp-Apim-Subscription-Key, not
    # TrustedClientToken). Patched at call time so library updates can't
    # silently revert it.
    _new_wss = (
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        f"?Ocp-Apim-Subscription-Key={_C.TRUSTED_CLIENT_TOKEN}"
    )
    _comm.WSS_URL = _new_wss
    _C.WSS_URL = _new_wss

    async def _go():
        await edge_tts.Communicate(text, voice).save(str(out_path))
    asyncio.run(_go())


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


def build_dub_track(utterances, voice, tmpdir, total_dur, progress=None):
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
    placed = []
    cursor = 0.0  # earliest start for the next utterance
    # GAP is 0 on purpose: any fixed gap accumulates over dozens of
    # utterances (0.15s x 37 = ~4-5s drift by the end). With 0, the total
    # drift is only the sum of genuine overruns past MAX_ATEMPO (~0.04s at
    # 1.65 on the Oct 25 test video; was ~1.7s at 1.5, ~0.03s at 1.8).
    # The next utterance starts the instant the previous ends.
    GAP = 0.0
    for i, u in enumerate(utterances):
        mp3 = tmpdir / f"u{i}.mp3"
        synthesize(u["text"], voice, mp3)
        tts_dur = probe_duration(mp3)
        slot = max(u["end"] - u["start"], 0.5)
        ratio = tts_dur / slot
        atempo = 1.0 if ratio <= 1.0 else min(ratio, MAX_ATEMPO)
        fitted = tts_dur / atempo
        start = max(u["start"], cursor)
        start_ms = int(round(start * 1000))
        wav_i = tmpdir / f"p{i}.wav"
        af = []
        if atempo > 1.001:
            af.append(f"atempo={atempo:.3f}")
        af.append(f"adelay={start_ms}|{start_ms}")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3),
             "-af", ",".join(af), "-ar", "44100", "-ac", "2", str(wav_i)])
        placed.append(wav_i)
        cursor = start + fitted + GAP
        if progress:
            progress(i + 1, len(utterances))
    dub = tmpdir / "dub.wav"
    inputs = []
    for w in placed:
        inputs += ["-i", str(w)]
    run(["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex",
         f"amix=inputs={len(placed)}:dropout_transition=0:normalize=0,"
         f"apad=whole_dur={total_dur:.2f}",
         "-ar", "44100", "-ac", "2", str(dub)])
    return dub


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
    for pat in ("u*.mp3", "p*.wav"):
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


def mux(src_video, dub_wav, out_mp4, bg_wav=None, bg_volume=0.8):
    """Combine picture + dub track.

    bg_wav: music/effects with the original voices removed (separate_music).
    It goes under the dub at bg_volume and dips a little whenever the dub
    voice speaks (sidechain compressor) so the new voice stays clear.
    Without bg_wav the original audio is dropped entirely — the original
    voices are never mixed in.
    """
    # Burned-in credit watermark, bottom-right; scale guard keeps even dims
    # for yuv420p.
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
    if WATERMARK_TEXT and _drawtext_ok():
        font = f"fontfile={_WM_FONT}:" if os.path.exists(_WM_FONT) else ""
        text = WATERMARK_TEXT.replace("\\", "").replace("'", "").replace(":", "\\:")
        vf += (f"drawtext={font}text='{text}':"
               "fontsize=h/40:fontcolor=white@0.72:"
               "shadowcolor=black@0.55:shadowx=1:shadowy=1:"
               "x=w-text_w-h/40:y=h-text_h-h/56,")
    vf += "format=yuv420p"
    bg_in = []
    if bg_wav and bg_volume > 0:
        bg_in = ["-i", str(bg_wav)]
        fc = (f"[0:v]{vf}[v];"
              "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,asplit=2[dub][sc];"
              f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={bg_volume:.2f}[bg];"
              "[bg][sc]sidechaincompress=threshold=0.02:ratio=4:attack=20:release=400[duck];"
              "[duck][dub]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[a]")
        maps = ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    else:
        maps = ["-vf", vf, "-map", "0:v:0", "-map", "1:a:0"]
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src_video), "-i", str(dub_wav),
         *bg_in, *maps,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "128k",
         # No -shortest: the finish-the-sentence rule lets the tail of the
         # last utterance run a little past the video end; -shortest would
         # cut it off again.
         "-movflags", "+faststart", str(out_mp4)])


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
