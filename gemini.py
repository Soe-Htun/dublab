"""Gemini REST client for dubbing-script translation (no SDK needed)."""
import json
import re
import time
import ssl
import urllib.error
import urllib.request

def _ssl_context():
    """python.org's macOS Python ships without CA certificates
    (CERTIFICATE_VERIFY_FAILED); use certifi's bundle when available."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


MODEL = "gemini-3.5-flash-lite"
MAX_RETRIES = 4
CHUNK = 25
PARALLEL = 4     # chunks translated at the same time


PROMPT_EN2MM = """You are a professional dubbing script translator for short videos.
Translate each spoken line into natural SPOKEN Burmese (စကားပြောဘာသာ) — the way real people talk, never stiff written Burmese (စာရေးဘာသာ).

Rules:
- Translate meaning, tone and emotion — NEVER word-for-word. A natural Burmese line that captures the feeling beats a literal one.
- Keep it concise: each line will be SPOKEN in about the same time as the original, so avoid long-winded phrasing.
- TIMING IS CRITICAL: Burmese speech runs longer than English, so compress hard — use the shortest natural phrasing, drop filler words and repetition, keep only the core meaning and tone. A slightly shorter line is always better than a longer one.
- Match the speaker: casual stays casual, jokes stay funny in Burmese, adapt idioms to Burmese equivalents.
- Names and places stay as-is (transliterated if needed).
- Output format: exactly one line per input as "N|||translation" (N = the number I give you). Nothing else — no explanations."""


PROMPT_MM2EN = """You are a professional dubbing script translator for short videos.
Translate each spoken Burmese line into natural SPOKEN English — the way real people talk in videos, never stiff or literal.

Rules:
- Translate meaning, tone and emotion — NEVER word-for-word. A natural English line that captures the feeling beats a literal one.
- Keep it concise: each line will be SPOKEN in about the same time as the original, so avoid long-winded phrasing.
- TIMING IS CRITICAL: compress hard — use the shortest natural phrasing, drop filler words and repetition, keep only the core meaning and tone. A slightly shorter line is always better than a longer one.
- Match the speaker: casual stays casual, jokes stay funny in English, adapt Burmese idioms to English equivalents.
- Names and places stay as-is.
- Output format: exactly one line per input as "N|||translation" (N = the number I give you). Nothing else — no explanations."""


PROMPT_ZH2MM = """You are a professional dubbing script translator for Chinese short videos and movie recaps.
Translate each spoken Chinese (Mandarin) line into natural SPOKEN Burmese (စကားပြောဘာသာ) — the way Myanmar recap narrators really talk, never stiff written Burmese (စာရေးဘာသာ).

Rules:
- The Chinese text comes from automatic speech recognition, so it may contain wrong characters or homophones — infer the intended meaning from context.
- Translate meaning, tone and emotion — NEVER word-for-word. A natural Burmese line that captures the feeling beats a literal one.
- TIMING IS CRITICAL: Burmese speech runs much longer than Chinese, so compress hard — use the shortest natural phrasing, drop filler words and repetition, keep only the core meaning and tone. A slightly shorter line is always better than a longer one.
- Keep the storytelling voice: narration stays engaging and dramatic, dialogue matches the speaker, adapt Chinese idioms to Burmese equivalents.
- Names and places: transliterate into Burmese script consistently (same name → same spelling every time).
- Output format: exactly one line per input as "N|||translation" (N = the number I give you). Nothing else — no explanations."""

PROMPT_ANY2MM = """You are a professional dubbing script translator for short videos and movie recaps.
Translate each spoken {lang} line into natural SPOKEN Burmese (စကားပြောဘာသာ) — the way Myanmar narrators really talk, never stiff written Burmese (စာရေးဘာသာ).

Rules:
- The text comes from automatic speech recognition and may contain mistakes — infer the intended meaning from context.
- Translate meaning, tone and emotion — NEVER word-for-word.
- TIMING IS CRITICAL: Burmese speech runs longer, so compress hard — shortest natural phrasing, drop filler and repetition. A slightly shorter line is always better than a longer one.
- Match the speaker: narration stays engaging, dialogue matches the speaker, adapt idioms to Burmese equivalents.
- Names and places: transliterate into Burmese script consistently.
- Output format: exactly one line per input as "N|||translation" (N = the number I give you). Nothing else — no explanations."""

PROMPTS = {"en2mm": PROMPT_EN2MM, "mm2en": PROMPT_MM2EN, "zh2mm": PROMPT_ZH2MM}


def _call(prompt, api_key):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
           f":generateContent?key={api_key}")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.7}}
    limited = 0
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180, context=_SSL) as r:
                data = json.loads(r.read().decode())
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            if text.strip():
                return text
            raise RuntimeError("empty response")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                attempt += 1
                if attempt >= MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt * 2)
                continue
            # rate limited: wait as long as Gemini asks (per-minute quota), a few times
            limited += 1
            wait, daily = _retry_info(e)
            if daily or limited > RATE_RETRIES:
                raise RateLimited(daily) from None
            time.sleep(min(90, max(wait, 15 * limited)))
        except Exception:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(2 ** attempt * 2)


RATE_RETRIES = 5


class RateLimited(RuntimeError):
    """Gemini quota hit even after waiting (daily=True: the day's quota is used up)."""
    def __init__(self, daily=False):
        super().__init__("gemini daily quota used up" if daily else "gemini rate limit")
        self.daily = daily


def _retry_info(err):
    """(seconds to wait, is_daily_quota) from a 429 response body."""
    try:
        info = json.loads(err.read().decode()).get("error", {})
    except Exception:  # noqa: BLE001
        return 30, False
    wait, daily = 30, False
    for d in info.get("details", []):
        m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
        if m:
            wait = float(m.group(1)) + 1
        for v in d.get("violations", []):
            if "PerDay" in str(v.get("quotaId", "")):
                daily = True
    return wait, daily


CHANNEL_RULE = """
- CHANNEL NAME: this dub is published on the channel "{channel}". Whenever a line names the original creator's channel or brand (e.g. "subscribe to Filmyvia Recaps", "welcome back to X", "follow X for more"), write "{channel}" instead of that name, keeping it in Latin letters exactly as given. Never mention any other channel's name."""


def translate_lines(lines, direction, api_key, src_lang_name=None, channel=None):
    """lines: [str] -> [translated str], same order.

    direction "any2mm" translates from src_lang_name (e.g. "Japanese").
    channel: the owner's channel name, swapped in for the original creator's."""
    if direction == "any2mm":
        prompt = PROMPT_ANY2MM.format(lang=src_lang_name or "the original")
    else:
        prompt = PROMPTS[direction]
    if channel:
        head, sep, tail = prompt.partition("\n- Output format:")
        prompt = head + CHANNEL_RULE.format(channel=channel) + sep + tail
    def _chunk(start):
        chunk = lines[start:start + CHUNK]
        numbered = "\n".join(f"{i + 1}|||{t}" for i, t in enumerate(chunk))
        raw = _call(prompt + "\n\nTranslate these lines:\n" + numbered, api_key)
        results = {}
        for line in raw.strip().split("\n"):
            m = re.match(r"(\d+)\|\|\|(.+)", line.strip())
            if m:
                results[int(m.group(1))] = m.group(2).strip()
        return [results.get(i, orig) for i, orig in enumerate(chunk, 1)]

    # chunks are independent: send a few at once (a 24-min video is ~10 chunks)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(PARALLEL) as ex:
        parts = list(ex.map(_chunk, range(0, len(lines), CHUNK)))
    return [t for part in parts for t in part]


INTRO_PROMPT = """You write the spoken opening line of a Burmese movie-recap video.
Below is the Burmese narration/dialogue of the video (it may be cut short).

Write ONE short hook in natural SPOKEN Burmese (စကားပြောဘာသာ) that a Myanmar recap narrator
would say before the story starts, in the style of "ဒီနေ့ ဇာတ်လမ်းက ရေခဲပင်လယ်ထဲမှာ…".

Rules:
- 1–2 sentences, at most about 30 words (it is spoken in under ~10 seconds).
- Start with "ဒီနေ့ ဇာတ်လမ်းက" (or a very close variant).
- Set up the situation and make people want to keep watching; never reveal the ending or twists.
- No greetings, no channel names, no "like/follow/subscribe", no emojis, no quotes.
- Output only the hook text, nothing else."""


def write_intro(lines, api_key, max_chars=6000):
    """A story hook spoken before the video starts, from the translated lines."""
    text, used = [], 0
    for ln in lines:
        if used + len(ln) > max_chars:
            break
        text.append(ln)
        used += len(ln) + 1
    raw = _call(INTRO_PROMPT + "\n\nVideo text:\n" + "\n".join(text), api_key)
    hook = re.sub(r"\s+", " ", raw).strip().strip('"“”')
    return hook[:300]


TITLE_PROMPT = """You name Burmese movie-recap videos.
Below is the Burmese narration/dialogue of a video (it may be cut short).

Write ONE catchy Burmese title for the video cover, like "မှော်မျက်လုံး ရသွားသူ" or
"ရေခဲပင်လယ်က အသက်လုပွဲ" — short, dramatic, makes people click.

Rules:
- Burmese only, 2–6 words, at most about 25 characters.
- About the main hook of the story; no spoilers of the ending.
- No channel names, no hashtags, no emojis, no quotes, no punctuation at the end.
- Output only the title."""


def write_title(lines, api_key, max_chars=6000):
    text, used = [], 0
    for ln in lines:
        if used + len(ln) > max_chars:
            break
        text.append(ln)
        used += len(ln) + 1
    raw = _call(TITLE_PROMPT + "\n\nVideo text:\n" + "\n".join(text), api_key)
    title = re.sub(r"\s+", " ", raw.strip().splitlines()[0]).strip().strip('"“”။')
    return title[:60]
