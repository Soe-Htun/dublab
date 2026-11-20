"""Gemini REST client for dubbing-script translation (no SDK needed)."""
import json
import os
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
PARALLEL = 6     # chunks translated at the same time (spread over the keys)


NAME_RULE = """- CHARACTER NAMES: do NOT transliterate foreign character names. Give every character a familiar Burmese name,
  and never refer to a main character only as "ဒီကောင်လေး", "ဒီကောင်မလေး", "သူ", etc. when a name fits. Preferred names:
  young man / boy: ဂွေးတို, ကျော်ကြီး, စိုးကြီး, အောင်လေး
  young woman / girl: ဂွက်ထော်, မြမြ, ဖြူဖြူထွေး, လှလှ — and when there are more girls (e.g. one man
    and many girls): နှင်းနှင်း, ခိုင်လေး, ပိုပို, နှင်းမြ, ချယ်ရီ, သီသီ, ဇင်ဇင်, ချစ်ချစ်, ဝါဝါ, ယုယု, စုစု, အိအိ, နွေနွေ
  older man: ကျော်ရဲ, ကျော်မျိုး, တင်မောင်
  older woman: ဒေါ်မြ, ဒေါ်လှ, ဂျမ်းစိန်
  little boy (child): လပ်ကီး, ဘနားနား
  baby / little girl: ဂျက်မ, ဂူးဂူး
  CHINESE films only (Chinese-language films, Chinese dramas and Chinese AI movies): two Chinese-sounding names
  may be used as well — လင်ဖုန်း (a young man) and ရှောင်မိန် (a young woman). Never use these two for any other
  film (Korean, Indian, Thai, English…): there use only the Burmese names above.
  Use these first, in this order, for the most important characters; for more characters pick other SHORT, common
  Burmese names (one or two syllables, often doubled like နှင်းနှင်း) that are easy to tell apart.
  Each character keeps the SAME name every time; never give two characters the same name.
  FAMILY of a main character (the hero OR the heroine, e.g. "ဂွက်ထော်ရဲ့ အမေ"): a parent or relative with a small
  part gets NO name of their own — call them by the
  relation to that character ("ဂွေးတိုရဲ့ အဖေ", "ဂွေးတိုရဲ့ အမေ", "ဂွေးတိုရဲ့ ညီလေး"), or "သူ့အဖေ", "သူ့အမေ" when that
  main character is already in the sentence (e.g. "ဂွေးတိုလည်း သူ့အဖေကို ပြောလိုက်ပါတယ်"). But a family member
  who is an important character too (e.g. two brothers who are both in the story a lot) DOES get a name: say the
  relation with the name the first time ("ဂွေးတိုရဲ့ အကို ကျော်ကြီး"), then just the name.
- Places may be transliterated into Burmese script."""


PROMPT_EN2MM = """You are a professional dubbing script translator for short videos.
Translate each spoken line into natural SPOKEN Burmese (စကားပြောဘာသာ) — the way real people talk, never stiff written Burmese (စာရေးဘာသာ).

Rules:
- Translate meaning, tone and emotion — NEVER word-for-word. A natural Burmese line that captures the feeling beats a literal one.
- Keep it concise: each line will be SPOKEN in about the same time as the original, so avoid long-winded phrasing.
- TIMING IS CRITICAL: Burmese speech runs longer than English, so compress hard — use the shortest natural phrasing, drop filler words and repetition, keep only the core meaning and tone. A slightly shorter line is always better than a longer one.
- Match the speaker: casual stays casual, jokes stay funny in Burmese, adapt idioms to Burmese equivalents.
""" + NAME_RULE + """
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
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
""" + NAME_RULE + """
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
- Output format: exactly one line per input as "N|||translation" (N = the number I give you). Nothing else — no explanations."""

PROMPT_ANY2MM = """You are a professional dubbing script translator for short videos and movie recaps.
Translate each spoken {lang} line into natural SPOKEN Burmese (စကားပြောဘာသာ) — the way Myanmar narrators really talk, never stiff written Burmese (စာရေးဘာသာ).

Rules:
- The text comes from automatic speech recognition and may contain mistakes — infer the intended meaning from context.
- Translate meaning, tone and emotion — NEVER word-for-word.
- TIMING IS CRITICAL: Burmese speech runs longer, so compress hard — shortest natural phrasing, drop filler and repetition. A slightly shorter line is always better than a longer one.
- Match the speaker: narration stays engaging, dialogue matches the speaker, adapt idioms to Burmese equivalents.
""" + NAME_RULE + """
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
- Output format: exactly one line per input as "N|||translation" (N = the number I give you). Nothing else — no explanations."""

PROMPTS = {"en2mm": PROMPT_EN2MM, "mm2en": PROMPT_MM2EN, "zh2mm": PROMPT_ZH2MM}


_spent = {}      # key -> day (YYYY-MM-DD, Pacific) its daily quota ran out


def _quota_day():
    # Gemini's free daily quota resets at midnight US Pacific time
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 8 * 3600))


def _keys(first):
    """Keys to try, in order: GEMINI_API_KEYS (comma-separated, one per Google
    Cloud project — the free quota is per project) and the key passed in."""
    keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
    if first and first not in keys:
        keys.append(first)
    return keys or [first]


_turn = __import__("itertools").count()


def _call(prompt, api_key, max_tokens=8192, images=None):
    """images: optional [(label, jpeg bytes)] sent after the prompt, each
    preceded by its label so the model can refer to it. Calls take turns over
    GEMINI_API_KEYS (each key has its own per-minute limit, so parallel calls
    don't all queue behind one); a key whose daily quota is used up is skipped."""
    today = _quota_day()
    last = None
    keys = _keys(api_key)
    k0 = next(_turn) % len(keys)
    for key in keys[k0:] + keys[:k0]:
        if _spent.get(key) == today:
            continue
        try:
            return _call_one(prompt, key, max_tokens, images)
        except RateLimited as e:
            last = e
            if not e.daily:
                raise
            _spent[key] = today
            print(f"[gemini] key …{key[-4:]} used up for today — trying the next key")
    raise last or RateLimited(True)


# when the main model is overloaded (HTTP 503 "high demand" — it came and went for
# minutes on 2026-11-19 and made an 18-min video's translation take 4+ min), the
# retry goes to the next of these instead of waiting for the same one
FALLBACK_MODELS = [m.strip() for m in (os.environ.get("DUBLAB_GEMINI_FALLBACKS") or
                   "gemini-flash-lite-latest,gemini-3.1-flash-lite,gemini-3.5-flash").split(",") if m.strip()]


def _call_one(prompt, api_key, max_tokens=8192, images=None):
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    parts = [{"text": prompt}]
    for item in images or []:          # (label, bytes) JPEGs, or (label, bytes, mime) e.g. audio
        import base64
        label, data = item[0], item[1]
        mime = item[2] if len(item) > 2 else "image/jpeg"
        parts += [{"text": label},
                  {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}]
    # films have fights, crime and romance: don't let the default safety filter
    # refuse to summarise a whole section of a movie
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
            "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in (
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]}
    limited = 0
    attempt = 0
    busy = 0                       # 5xx answers so far: each one moves on to the next model
    while True:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{models[busy % len(models)]}"
               f":generateContent?key={api_key}")
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180, context=_SSL) as r:
                data = json.loads(r.read().decode())
            if not data.get("candidates"):         # the request itself was refused
                reason = (data.get("promptFeedback") or {}).get("blockReason") or "no answer"
                raise Blocked(f"Gemini refused this part ({reason})")
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            if text.strip():
                return text
            raise RuntimeError("empty response")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                attempt += 1
                try:
                    detail = re.sub(r"\s+", " ", e.read().decode(errors="replace"))[:300]
                except Exception:  # noqa: BLE001
                    detail = ""
                print(f"[gemini] HTTP {e.code}: {detail}")
                if "location is not supported" in detail:   # the VPN / network is in a country
                    raise RuntimeError(                    # Gemini doesn't serve: retrying won't help
                        "Gemini က ဒီနိုင်ငံကနေ သုံးခွင့်မပေးဘူး (User location is not supported) — "
                        "VPN သုံးနေရင် နိုင်ငံ ပြောင်းပါ (ဥပမာ Singapore / Japan / US) ဒါမှမဟုတ် VPN ပိတ်ပြီး "
                        "▶️ ဆက်လုပ်မယ် ကို နှိပ်ပါ") from None
                # 5xx = overloaded; a 400 has also come and gone for minutes (2026-11-19)
                # with the very same request working later: keep trying ~4 minutes
                limit = MAX_RETRIES + 6 if e.code >= 500 or e.code == 400 else MAX_RETRIES
                if attempt >= limit:
                    raise RuntimeError(f"Gemini HTTP {e.code}: {detail or e.reason}") from None
                if e.code >= 500:
                    busy += 1
                    if busy % len(models):     # another model to try: go now, don't wait
                        time.sleep(1)
                        continue
                time.sleep(min(30, 2 ** attempt * 2))
                continue
            # rate limited: wait as long as Gemini asks (per-minute quota), a few times
            limited += 1
            wait, daily = _retry_info(e)
            if daily or limited > RATE_RETRIES:
                raise RateLimited(daily) from None
            time.sleep(min(90, max(wait, 15 * limited)))
        except Blocked:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            # no internet for a moment (Wi-Fi drop, DNS): keep trying for ~3 minutes
            attempt += 1
            if attempt >= MAX_RETRIES + 6:
                raise
            time.sleep(min(30, 2 ** attempt * 2))
        except Exception:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(2 ** attempt * 2)


RATE_RETRIES = 5


class Blocked(RuntimeError):
    """Gemini's safety filter refused the request (asking again won't help)."""


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


def translate_lines(lines, direction, api_key, src_lang_name=None, channel=None, prog=None):
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
    starts = list(range(0, len(lines), CHUNK))
    done = [0]
    def _counted(start):
        r = _chunk(start)
        done[0] += 1
        if prog:
            prog(0.9 * done[0] / len(starts))       # the last 10%: the spelling check
        return r
    with ThreadPoolExecutor(PARALLEL) as ex:
        parts = list(ex.map(_counted, starts))
    out = [t for part in parts for t in part]
    return out if direction == "mm2en" else fix_script(out, api_key)


INTRO_PROMPT = """You write the spoken opening line of a Burmese movie-recap video.
Below is the Burmese narration/dialogue of the video (it may be cut short).

Write ONE short hook in natural SPOKEN Burmese (စကားပြောဘာသာ) that a Myanmar recap narrator
would say before the story starts, in the style of "ဒီနေ့ ဇာတ်လမ်းက ရေခဲပင်လယ်ထဲမှာ…".

Rules:
- 1–2 sentences, at most about 30 words (it is spoken in under ~10 seconds).
- Start with "ဒီနေ့ ဇာတ်လမ်းက" (or a very close variant).
- Set up the situation and make people want to keep watching; never reveal the ending or twists.
- No greetings, no channel names, no "like/follow/subscribe", no emojis, no quotes.
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
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
    return fix_script([hook[:300]], api_key)[0]


TITLE_PROMPT = """You name Burmese movie-recap videos.
Below is the Burmese narration/dialogue of a video (it may be cut short).

Write ONE catchy Burmese title for the video cover, like "မှော်မျက်လုံး ရသွားသူ" or
"ရေခဲပင်လယ်က အသက်လုပွဲ" — short, dramatic, makes people click.

Rules:
- Burmese only, 2–6 words, at most about 25 characters.
- About the main hook of the story; no spoilers of the ending.
- No channel names, no hashtags, no emojis, no quotes, no punctuation at the end.
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
- Output only the title."""


MOVIE_NAME_PROMPT = """Below is the name a video was shared under (a Telegram caption, file name or YouTube title).
If it names a real movie or series, give that movie's name the way people search for it — in its usual spelling
(Latin letters for foreign films; a Burmese film keeps its Burmese title in Myanmar script), with the uploader's
tricks undone: letters split by dashes, dots or spaces joined back
("R-R-R" → "RRR", "K-i-n-g O-f B-e-g-g-a-r-s" → "King of Beggars", "Na..... ay..... ak" → "Nayak"), and without
years, quality tags, language/subtitle notes (e.g. "မြန်မာစာတန်းထိုး", "Hindi", "HD", "1080p", "Full Movie",
"မြန်မာဇာတ်ကား", "ရုပ်ရှင်"),
channel names, hashtags and emojis.
If it does not clearly name a movie (e.g. "video_123", a random sentence, a channel's description), answer NONE.
Answer with the name only (or NONE)."""


def movie_name(shared_as, api_key):
    """The movie's real name from what it was shared as, or "" when unknown."""
    shared_as = re.sub(r"\s+", " ", shared_as or "").strip()
    if not shared_as:
        return ""
    raw = _call(MOVIE_NAME_PROMPT + "\n\nShared as: " + shared_as[:300], api_key, max_tokens=64)
    name = raw.strip().splitlines()[0].strip().strip('"“”\'`*').strip() if raw.strip() else ""
    if not name or name.upper().startswith("NONE") or len(name) > 60:
        return ""
    return name


def write_title(lines, api_key, max_chars=6000):
    text, used = [], 0
    for ln in lines:
        if used + len(ln) > max_chars:
            break
        text.append(ln)
        used += len(ln) + 1
    raw = _call(TITLE_PROMPT + "\n\nVideo text:\n" + "\n".join(text), api_key)
    title = re.sub(r"\s+", " ", raw.strip().splitlines()[0]).strip().strip('"“”။')
    return fix_script([title[:60]], api_key)[0]



POSTS_PROMPT = """You write the upload texts for a Burmese movie-recap channel called "{channel}".
The video is a Burmese-narrated recap of a film. Cover title (Burmese): {title}
Original video title (may name the film): {name}
Beginning of the Burmese narration:
{story}

Write, in natural Burmese (Myanmar script; film names may stay in their original form):
- "tiktok": a TikTok / Facebook Reels caption: 1–2 short catchy lines that make people want to watch
  (a hook or question, no spoilers of the ending) that plainly says it is a film's story (start with
  "🎬 ရုပ်ရှင်ဇာတ်လမ်း:" or similar — never word it like real news), then the hashtags. Under 250 characters.
- "youtube_title": a YouTube title under 90 characters: the cover title, plus the film's real name in
  brackets ONLY if that name appears in the original video title above — never invent or guess an
  English name. No clickbait lies, no ALL CAPS.
- "youtube_desc": a YouTube description: 3–4 lines summing up the story's setup (no ending spoilers),
  a line saying it is a Burmese recap / review with our own narration, then the hashtags.
- "facebook": a Facebook Page post: 2–3 lines, friendly, a question to invite comments, then the hashtags.
HASHTAGS: only 3–4 short ENGLISH ones about the genre / story (e.g. #drama #fantasy #revenge #korean).
The channel's own Burmese hashtags are added after yours — do not write any Burmese hashtag, and never
#subtitle-type tags (the video is narrated, not subtitled).
- "ai_film": true if the title, name or story suggests the film itself is AI-generated (AI movie /
  AI drama / AI short film), else false.
No channel names other than "{channel}". No like/subscribe begging lines.
Reply with ONLY a JSON object with exactly these keys."""


def write_posts(title, name, story, api_key, channel="Marco Recap"):
    """Upload texts for TikTok / YouTube / Facebook in one call:
    {tiktok, youtube_title, youtube_desc, facebook, ai_film}."""
    raw = _call(POSTS_PROMPT.format(channel=channel, title=title or "-", name=name or "-",
                                    story=(story or "")[:3500]), api_key, max_tokens=2048)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0))) if m else {}
    out = {k: (data.get(k) if k == "ai_film" else str(data.get(k) or "").strip())
           for k in ("tiktok", "youtube_title", "youtube_desc", "facebook", "ai_film")}
    keys = ("tiktok", "youtube_title", "youtube_desc", "facebook")
    lines = [(k, ln) for k in keys for ln in out[k].split("\n")]
    fixed = fix_script([ln for _, ln in lines], api_key)          # stray Khmer/Thai letters etc.
    for k in keys:
        out[k] = "\n".join(f for (kk, _), f in zip(lines, fixed) if kk == k).strip()
    tag = "#" + re.sub(r"\W", "", channel)
    own = {"tiktok": f"{tag} #movierecap #ရုပ်ရှင် #မြန်မာ #fyp",
           "youtube_desc": f"{tag} #movierecap #ရုပ်ရှင် #မြန်မာ",
           "facebook": f"{tag} #movierecap #ရုပ်ရှင်"}
    for k, tags in own.items():
        if out[k]:
            out[k] += " " + tags
    return out


# ---- recap version: a shorter Burmese narration over chosen scenes ----
TTS_CPS = 15.0 * float(os.environ.get("DUBLAB_SPEAK_SPEED") or 1.15)   # Burmese TTS ~15 chars/s at normal pace, sped up in dub.synthesize

BEAT_CHARS = 130        # one beat ≈ 2–3 sentences ≈ 8 s spoken; a beat count is easier for the model to hit than a character count
RECAP_SECTION_MIN = 4   # minutes of narration per parallel request: the model fills short targets far better than long ones
RECAP_PARALLEL = 6

RECAP_PROMPT = """You are a Myanmar movie-recap narrator (like popular Burmese recap channels).
Below is the transcript of {what}: numbered lines "N|mm:ss|text" in {lang}, from speech recognition
(may contain mistakes — infer the meaning).

Write a RECAP of it in natural, engaging SPOKEN Burmese (စကားပြောဘာသာ), told in your own words as a
narrator ("ဂွေးတိုက…", "အဲ့ဒီမှာ…"), not a translation of the dialogue.

Rules:
- LENGTH IS A HARD REQUIREMENT: write AT LEAST {beats} beats, each about {beat_chars} Burmese characters
  (2–3 sentences) — about {chars} characters in total (≈ {minutes} minutes spoken). Narrations that come out
  short are rejected, so tell the story scene by scene in detail: what each character does, says and feels,
  every conflict, twist and the ending of this section. Never summarise several scenes in one line.
- Beats are in story order. Each beat tells what happens during a short range of transcript lines, so the
  matching scene can be shown while it is narrated; cover the whole section from start to end.
- Each beat's line range must be in order and not go backwards; ranges should be short (the scene shown).
- SKIP ADVERTISEMENTS: re-uploaded films often have ads cut into them (betting/casino sites such as 1XBET,
  apps, shops, "download free movies at www…", channel promos, trailers of other films). Their lines do not
  belong to the story (different topic, selling something, website names, prices, "download", "join our
  channel"). Never narrate them and never point a beat's line range at them or at the lines right next to them,
  so the ad is not shown.
- SKIP SONGS AND DANCES: song-and-dance numbers, music videos and montages (common in Indian films), and slow
  moody moments where music plays over a character just feeling or walking, are not story. Transcript lines that are song lyrics (rhyming, repeated chorus lines, poetic words unrelated to the
  dialogue around them) belong to such a number: never narrate them and never point a beat's line range at them,
  so the dance scene is not shown. Jump straight to the next story scene.
""" + NAME_RULE + """
- {extra}
- IT IS A FICTIONAL FILM, NOT NEWS: tell it as a film's story ("ဒီဇာတ်ကားထဲမှာ…", "ရုပ်ရှင်ထဲမှာ…").
  When real people, real teams/clubs, companies, countries, diseases, wars or historical events appear, say
  they are in the film, so nobody takes it as real-world news. Never say it really happened unless the film
  itself says it is based on a true story (then say "အဖြစ်မှန်ကို အခြေခံထားတဲ့ ဇာတ်ကား").
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
- Output format: one beat per line as "FROM|TO|narration" (FROM/TO = line numbers). Nothing else."""


def _recap_chunk(lines, lo, hi, minutes, lang, extra, api_key, what):
    numbered = "\n".join(f"{i}|{int(s // 60):02d}:{int(s % 60):02d}|{t}" for i, (s, t) in enumerate(lines[lo:hi], lo))
    chars = int(minutes * 60 * TTS_CPS)
    prompt = RECAP_PROMPT.format(what=what, lang=lang or "the original language",
                                 chars=chars, minutes=round(minutes, 1),
                                 beats=max(3, round(chars / BEAT_CHARS)), beat_chars=BEAT_CHARS,
                                 extra=extra) + "\n\nTranscript:\n" + numbered
    try:
        raw = _call(prompt, api_key, max_tokens=60000)
    except Blocked as e:              # one violent/explicit section: leave it out, keep the rest
        print(f"[recap] lines {lo}-{hi} skipped: {e}")
        return []
    beats, last = [], lo
    for line in raw.strip().split("\n"):
        m = re.match(r"\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.+)", line)
        if not m:
            continue
        a, b, say = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        a = min(max(a, last, lo), hi - 1)
        b = min(max(b, a), hi - 1)
        beats.append({"from": a, "to": b, "say": say})
        last = a
    return beats


def _chars(beats):
    return sum(len(b["say"]) for b in beats)


CAST_PROMPT = """Below is the transcript of a movie ({lang}, from speech recognition — may contain mistakes).
Ignore advertisements cut into the video (betting sites, apps, channel promos) — their people are not characters.
List its characters (at most 12, most important first) and give each one a Burmese name for a
Myanmar recap narration.

""" + NAME_RULE + """

A main character's family members with small parts are written by relation instead of a name, e.g.
"ဂွေးတိုရဲ့ အဖေ = the hero's father (original name if known)"; a family member with a big part gets a name and the
relation, e.g. "ကျော်ကြီး = ဂွေးတိုရဲ့ အကို, the hero's elder brother (original name if known)".
Output one character per line as "Burmese name = who they are (role, gender, age; their original name if known)".
Nothing else."""


CAST_PROMPT_MM = """Below is the transcript of a BURMESE movie (from speech recognition — names may be misspelled).
List its characters (at most 12, most important first) with the names the film itself uses for them, spelled
correctly in Burmese (fix obvious speech-recognition mistakes). A character never called by name is written by
role or relation, e.g. "ကိုအောင်ရဲ့ အမေ". Ignore advertisements cut into the video.
Output one character per line as "name = who they are (role, gender, age)". Nothing else."""


def _is_burmese(lang):
    return (lang or "").strip().lower() in ("my", "burmese", "myanmar")


TRANSCRIBE_PROMPT = """This audio is a {length:.0f}-second piece of a BURMESE (Myanmar) movie. Write down what the
people SAY, in Burmese (Myanmar script), as spoken lines with the time each starts, from 0 at the start of this
piece. Skip music, songs and silence. One line per spoken sentence, like:
00:03|ဘယ်သွားမလို့လဲ
00:07|ဈေးကို သွားမလို့ပါ
Output only such lines."""


def transcribe_burmese(chunks, api_key, log=None):
    """Whisper can't write Burmese (it gave lines like "වවවව…"), so Gemini listens
    instead. chunks: [(start_seconds, length_seconds, mp3 bytes)].
    Returns [{start, end, text}] on the film's timeline."""
    from concurrent.futures import ThreadPoolExecutor

    def one(ch):
        t0, length, mp3 = ch
        try:
            raw = _call(TRANSCRIBE_PROMPT.format(length=length), api_key, max_tokens=8192,
                        images=[("Audio:", mp3, "audio/mp3")])
        except (RuntimeError, Blocked):
            return []          # music / credits only (nothing said), or refused: skip this piece
        out = []
        for line in raw.splitlines():
            m = re.match(r"\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*[|\-–]\s*(.+)", line)
            if not m:
                continue
            a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
            sec = a * 3600 + b * 60 + int(c) if c is not None else a * 60 + b
            text = m.group(4).strip()
            if text and sec <= length + 5:
                out.append((t0 + sec, text))
        return out

    done = [0]
    def track(ch):
        r = one(ch)
        done[0] += 1
        if log:
            log(done[0], len(chunks))
        return r
    with ThreadPoolExecutor(min(4, len(chunks)) or 1) as ex:
        found = [x for part in ex.map(track, chunks) for x in part]
    found.sort()
    segs = []
    for i, (t, text) in enumerate(found):
        nxt = found[i + 1][0] if i + 1 < len(found) else t + 4
        end = min(nxt, t + max(1.5, len(text) / 12))       # ~12 Burmese chars a second
        segs.append({"start": float(t), "end": float(max(end, t + 0.8)), "text": text})
    return segs


def write_cast(lines, api_key, lang=None, max_chars=60000):
    """Burmese names for the characters, decided once for the whole film so
    every parallel recap section calls them the same."""
    text = "\n".join(t for _, t in lines)
    if len(text) > max_chars:                     # keep beginning, middle and end
        third = max_chars // 3
        mid = len(text) // 2
        text = text[:third] + "\n…\n" + text[mid - third // 2: mid + third // 2] + "\n…\n" + text[-third:]
    prompt = CAST_PROMPT_MM if _is_burmese(lang) else CAST_PROMPT
    raw = _call(prompt.format(lang=lang or "original language") + "\n\nTranscript:\n" + text,
                api_key, max_tokens=2000)
    rows = [r.strip(" -*") for r in raw.strip().splitlines() if "=" in r]
    return "\n".join(rows[:12])


def write_recap(lines, minutes, api_key, lang=None, channel=None, min_minutes=0, log=None, prog=None, slack=5):
    """lines: [(start_seconds, text)] of the whole video. Returns beats
    [{"from": i, "to": j, "say": burmese}] in order (i/j index into lines).
    Long targets are written in parallel sections of ~4 minutes.
    min_minutes: the narration alone (no intro/outro) must reach this; the
    model tends to write short, so short sections are rewritten longer."""
    from concurrent.futures import ThreadPoolExecutor
    k = max(1, round(minutes / RECAP_SECTION_MIN))
    bounds = [round(len(lines) * i / k) for i in range(k + 1)]
    per = minutes / k
    extra = ("Leave out any channel names and channel promotion of the original video; "
             "no greetings, no like/subscribe/follow lines (those are added separately).")
    try:
        cast = write_cast(lines, api_key, lang)
    except Exception as e:  # noqa: BLE001 - the per-section name rule still applies
        cast = ""
        if log:
            log(f"character names skipped: {e}")
    if _is_burmese(lang):        # a Burmese film: its own names, not the preferred-names list
        extra += ("\n- This is a BURMESE film: call the characters by the film's OWN names (as listed below), "
                  "NOT by the preferred-names list above. Retell it in your own words as a narrator — never copy "
                  "the dialogue line by line.")
    if cast:
        if log:
            log("characters: " + " · ".join(r.split("=")[0].strip() for r in cast.splitlines()))
        extra += ("\n- USE EXACTLY THESE CHARACTER NAMES (decided for the whole film; every section must "
                  "call each character the same):\n" + cast)

    todo, done = [k], [0]                 # sections to write (rewrites add to it), sections written
    def tick():
        done[0] += 1
        if prog:
            prog(0.05 + 0.85 * done[0] / todo[0])     # 5%: the names · last 10%: the spelling check
    if prog:
        prog(0.05)

    def job(i):
        what = "a video" if k == 1 else f"part {i + 1} of {k} of a video (continue the story; do not re-introduce it)"
        r = _recap_chunk(lines, bounds[i], bounds[i + 1], per, lang, extra, api_key, what)
        tick()
        return r

    with ThreadPoolExecutor(min(k, RECAP_PARALLEL)) as ex:
        parts = list(ex.map(job, range(k)))
        # each section must carry its share of the minimum (+8% headroom);
        # ask again for the short ones, scaled up by how short they came out
        need = min_minutes * 60 * TTS_CPS * 1.08 / k
        # up to `slack` min short of the minimum is fine (saves quota; 0 for the YouTube
        # long version, which must reach its 80%); more than that is
        # rewritten, up to 3 times. Longer than asked is always fine.
        for rnd in range(3):
            got = sum(_chars(p) for p in parts) / TTS_CPS / 60
            short = [i for i in range(k) if _chars(parts[i]) < need]
            if not min_minutes or not short or got >= min_minutes - slack:
                break
            if log:
                got = sum(_chars(p) for p in parts) / TTS_CPS / 60
                log(f"recap only ~{got:.1f} min (need {min_minutes}+) — rewriting {len(short)} "
                    f"short section(s) longer (try {rnd + 1})")

            def redo(i):
                grow = min(3.0, need * 1.2 / max(1, _chars(parts[i])))
                what = ("a video" if k == 1 else f"part {i + 1} of {k} of a video (continue the story; "
                        "do not re-introduce it)") + (". Your previous version was far too short: tell "
                        "this part in much more detail, scene by scene")
                r = _recap_chunk(lines, bounds[i], bounds[i + 1], per * grow, lang, extra, api_key, what)
                tick()
                return r

            todo[0] += len(short)
            for i, new in zip(short, ex.map(redo, short)):
                if _chars(new) > _chars(parts[i]):
                    parts[i] = new
    beats = [b for part in parts for b in part]
    fixed = fix_script([b["say"] for b in beats], api_key)
    for b, t in zip(beats, fixed):
        b["say"] = t
    return beats



# ---- keep Burmese text in Myanmar script ----
# The model sometimes slips Thai (or other scripts) into Burmese words, e.g.
# "တပ်စည်းဆราบ". Such lines are sent back once to be rewritten; anything still
# foreign after that is dropped so it is never spoken or burned in.
# Anything that is not Burmese, Latin (English words, the channel name), digits or
# common punctuation counts: the model has slipped in Thai, Chinese, Punjabi, Kannada,
# Telugu, Sinhala, Hebrew, Armenian… letters, which the font shows as □.
_FOREIGN = re.compile("[^\u0000-\u024F\u1000-\u109F\uA9E0-\uA9FF\uAA60-\uAA7F"
                      "\u2000-\u206F\u20A0-\u20CF\u2100-\u214F]")

FIX_PROMPT = """These Burmese lines wrongly contain characters from other scripts (e.g. Thai, Chinese, Hindi,
Punjabi, Kannada, Telugu, Sinhala, Hebrew).
Rewrite each line in correct Burmese (Myanmar script) only, keeping the meaning and style.
Names may stay in Latin letters. Output exactly one line per input as "N|||line". Nothing else."""


def has_foreign(text):
    return bool(_FOREIGN.search(text or ""))


# letters Gemini sometimes writes in place of their Burmese look-alikes
# ("တက္កသိုလ်", "ကတော့ក​ွာ" — Khmer ក / Thai ก for Burmese က): swapped back
# directly; stripping them instead left "ွာ" with no consonant (a ◌ on screen)
_LOOKALIKE = str.maketrans({"\u1780": "\u1000", "\u0E01": "\u1000",     # ក ก → က
                            "\u1782": "\u1002", "\u1784": "\u1004",     # គ → ဂ, ង → င
                            "\u0E07": "\u1004", "\u0E2D": "\u1021"})    # ง → င, อ → အ
# a foreign letter together with the Burmese marks hanging on it: removed as one,
# so no vowel sign or medial is left without its consonant
_FOREIGN_SYL = re.compile(_FOREIGN.pattern + "[\u102B-\u103E\u1056-\u1059\u105E-\u1060\u1062-\u1064\u1067-\u106D\u1071-\u1074\u1082-\u108D\u108F\u109A-\u109D]*")
_ORPHAN = re.compile(r"(^|(?<=\s))[\u102B-\u103E]+")     # marks left at the start of a word


def fix_script(lines, api_key):
    lines = [(t or "").translate(_LOOKALIKE) for t in lines]
    bad = [i for i, t in enumerate(lines) if has_foreign(t)]
    if not bad:
        return lines
    out = list(lines)
    try:
        raw = _call(FIX_PROMPT + "\n\n" + "\n".join(f"{k + 1}|||{lines[i]}" for k, i in enumerate(bad)),
                    api_key)
        for line in raw.strip().split("\n"):
            m = re.match(r"(\d+)\|\|\|(.+)", line.strip())
            if m and 1 <= int(m.group(1)) <= len(bad):
                out[bad[int(m.group(1)) - 1]] = m.group(2).strip()
    except Exception:  # noqa: BLE001 - fall through to stripping
        pass
    return [re.sub(r"\s{2,}", " ", _ORPHAN.sub("", _FOREIGN_SYL.sub("", t.translate(_LOOKALIKE)))).strip()
            if has_foreign(t) else t for t in out]


# ---- cover photos: let the model find the main character ----
SENSITIVE_PROMPT = """The numbered frames below (0 to {last}) come from a movie, one every {step:.0f} seconds.
List the numbers of the frames that show:
- "blood": visible blood, gore, open wounds, a dead body with injuries, or a bloody/gruesome injury;
- "sex": nudity (bare breasts, genitals or buttocks) or sexual activity / a sex scene.
Ordinary fighting, guns or punches WITHOUT blood, kissing, hugging, swimwear or a bare male chest are fine — do
not list them. When unsure, leave the frame out.
Answer with JSON only: {{"blood": [numbers], "sex": [numbers]}}"""


def find_sensitive(frames, step, api_key):
    """frames: JPEGs one every `step` seconds → set of frame numbers showing blood/gore
    or nudity/sex (to blur them: TikTok limits such videos)."""
    items = [(f"Frame {i}:", f) for i, f in enumerate(frames)]
    raw = _call(SENSITIVE_PROMPT.format(last=len(frames) - 1, step=step), api_key, max_tokens=2048, images=items)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0))) if m else {}
    out = set()
    for k in ("blood", "sex"):
        for v in data.get(k) or []:
            try:
                if 0 <= int(v) < len(frames):
                    out.add(int(v))
            except (TypeError, ValueError):
                pass
    return out


COVER_PROMPT = """The numbered frames below (0 to {last}) come from one movie.
Never pick a frame with blood, gore or nudity for anything below.
First work out its THREE MOST IMPORTANT CHARACTERS: the MAIN character (the protagonist, the person who appears
most and the story is about), the SECOND (e.g. the villain or partner) and the THIRD (e.g. the love interest).
Then answer with JSON only, no other text:
{{"layout": 2 or 4, "hero": [...], "second": [...], "third": [...], "scenes": [...], "action": [...],
  "tiktok": 1 or 2}}
- "layout": 4 for most movies; 2 (two big photos) when the video is simple — one or two characters, one setting —
  or there are fewer than 4 good, different frames.
- "hero": up to 5 frames of the MAIN character, "second": up to 4 of the second character, "third": up to 4 of the
  third — each with that person's WHOLE FACE clearly visible and sharp, best first: a MEDIUM shot (head, shoulders
  and chest; the face about a quarter to a third of the frame's height) with the WHOLE HEAD inside the frame and
  space above it — NEVER a close-up where the face fills the frame or the forehead/chin is cut off, never a face
  turned away, blurred or half hidden, and the face should be in the middle part of the frame (not at the left or
  right edge); a dramatic expression is a plus.
- "scenes": up to 4 striking frames of the movie's key action or setting with people in them, best first.
- "action": up to 4 frames that show what the movie is ABOUT with the main character in it, doing the movie's
  main thing (a football movie: the hero playing football on the pitch; boxing: in the ring; a heist: the robbery;
  a war movie: fighting), best first, ideally with the main character's FACE visible in it. Sharp frames only —
  no motion blur. They may repeat "hero"/"scenes" frames.
- "tiktok": 1 when one "action" frame alone shows the movie well (the main character clearly visible in the
  action), else 2.
Never pick dark, blurry, black, fading, empty or unclear frames (furniture, walls, backs of heads, crowds far away),
landscape-only, logo, title, credit or text frames, and never frames with SUBTITLE text on them (any language). Apart from "action", a frame may appear in only one list."""


def pick_cover_frames(jpegs, api_key):
    """jpegs: [bytes]. Returns (hero indices, other indices, 2 or 4 photos); raises on failure."""
    data = {}
    for attempt in range(2):          # the model's JSON is sometimes broken: ask once more
        raw = _call(COVER_PROMPT.format(last=len(jpegs) - 1), api_key, max_tokens=2048,
                    images=[(f"Frame {i}:", b) for i, b in enumerate(jpegs)])
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0))) if m else {}
            break
        except ValueError:
            if attempt:
                raise
    ok = lambda xs: [int(x) for x in xs or [] if str(x).isdigit() and 0 <= int(x) < len(jpegs)]
    hero = list(dict.fromkeys(ok(data.get("hero"))))
    second = [i for i in dict.fromkeys(ok(data.get("second"))) if i not in hero]
    third = [i for i in dict.fromkeys(ok(data.get("third"))) if i not in hero and i not in second]
    scenes = [i for i in dict.fromkeys(ok(data.get("scenes") or data.get("others")))
              if i not in hero and i not in second and i not in third]
    # best of the 2nd and 3rd characters first: a 4-photo cover takes others[0:2]
    others = second[:1] + third[:1] + second[1:] + third[1:] + scenes
    if not hero and not others:
        raise ValueError("no cover frames picked")
    layout = 2 if str(data.get("layout")).strip() == "2" else 4
    action = list(dict.fromkeys(ok(data.get("action"))))
    tiktok = 1 if str(data.get("tiktok")).strip() == "1" else 2
    focus = {}                    # frame -> (x0, y0, x1, y1) of the face, 0-1
    for k, v in (data.get("focus") or {}).items():
        try:
            if not (str(k).isdigit() and 0 <= int(k) < len(jpegs)):
                continue
            if isinstance(v, (list, tuple)) and len(v) >= 4:
                x0, y0, x1, y1 = (min(1.0, max(0.0, float(c) / 1000)) for c in v[:4])
                if x1 > x0 and y1 > y0:
                    focus[int(k)] = (x0, y0, x1, y1)
            else:                 # an older answer shape: just the x centre
                x = min(1.0, max(0.0, float(v) / 1000))
                focus[int(k)] = (x, 0.3, x, 0.6)
        except (TypeError, ValueError):
            pass
    return hero, others, layout, action, tiktok, focus


MOVIE_BOUNDS_PROMPT = """Frames S0..S{ls} come (in order) from the START of a movie file, and frames E0..E{le}
(in order) from its END. Find where the STORY itself starts and ends — everything that is not the story is cut off:
- before it: ads and promos (casino / betting / lottery (2D 3D), apps, shops, channel intros, "subscribe" screens,
  other films' trailers, uploader title cards), studio / TV-channel logos, and the opening title sequence or opening
  credits (cast and crew names over images, the film's title card);
- after it: end credits (scrolling names, cast lists, "the end", credits over images), ads, promos, trailers.
A scene with actors acting counts as the story even when a few names are shown over it near the start.
Answer with JSON only: {{"start": <number of the FIRST S frame of the story>, "end": <number of the LAST E frame
that is still the story>}}. If all S frames are the story, start is 0; if all E frames are the story, end is {le}."""


def find_movie_bounds(start_jpegs, end_jpegs, api_key):
    """(first film frame index in start_jpegs, last film frame index in end_jpegs)."""
    imgs = [(f"Frame S{i}:", b) for i, b in enumerate(start_jpegs)] + \
           [(f"Frame E{i}:", b) for i, b in enumerate(end_jpegs)]
    raw = _call(MOVIE_BOUNDS_PROMPT.format(ls=len(start_jpegs) - 1, le=len(end_jpegs) - 1), api_key,
                max_tokens=128, images=imgs)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    s = int(data.get("start", 0) or 0)
    e = int(data.get("end", len(end_jpegs) - 1) if data.get("end") is not None else len(end_jpegs) - 1)
    return max(0, min(s, len(start_jpegs) - 1)), max(0, min(e, len(end_jpegs) - 1))


# ---- find the burned-in captions to blur ----
CAPTION_BAND_PROMPT = """These frames come from one video. Some videos have subtitles burned into the picture
(e.g. English or Chinese text lines, usually near the bottom). Look at ALL frames together.
If there is burned-in subtitle text, give the vertical area that covers it in every frame where it appears,
on a 0-1000 scale of the frame height measured from the TOP (0 = top edge, 1000 = bottom edge).
Cover the whole text including its outline/shadow. Subtitles are often TWO lines in some frames and one line in
others: "top" must be the top of the HIGHEST line seen in ANY frame (the upper line of a two-line subtitle), and
"bottom" the bottom of the LOWEST line in any frame.
Ignore logos, watermarks, channel names in corners, and text that is part of the scene (signs, posters).
Answer with JSON only: {"subtitles": true, "top": <number>, "bottom": <number>} or {"subtitles": false}"""


def find_caption_band(jpegs, api_key):
    """Returns (top, bottom) as fractions of the frame height, or None when
    the video has no burned-in subtitles. Raises on API trouble."""
    raw = _call(CAPTION_BAND_PROMPT, api_key, max_tokens=256,
                images=[(f"Frame {i}:", b) for i, b in enumerate(jpegs)])
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    if not data.get("subtitles"):
        return None
    top, bot = float(data["top"]) / 1000, float(data["bottom"]) / 1000   # Gemini's native 0-1000 scale
    if not 0 <= top < bot <= 1 or bot - top > 0.4:
        raise ValueError(f"odd subtitle area {top:.2f}-{bot:.2f}")
    return top, bot


LOGO_PROMPT = """These frames come from one video. Find the LOGOS that sit at the SAME place in at least two frames
(some show only from time to time):
TV-channel bugs, studio or uploader logos, watermarks (also faint / semi-transparent ones), channel names,
website addresses and promotional text lines (e.g. "download free at www…", sponsor/betting ads) anywhere in the frame.
Do NOT include subtitles or captions, and nothing that is part of the scene (signs, posters, clothes, screens).
For each logo give its box on a 0-1000 scale (x from the LEFT edge, y from the TOP edge), covering the whole logo
with its outline. At most 4 logos.
Answer with JSON only: {"logos": [{"x0": <n>, "y0": <n>, "x1": <n>, "y1": <n>}, ...]} — or {"logos": []} when there is none."""


def find_logos(jpegs, api_key):
    """Boxes [(x0, y0, x1, y1)] as fractions of the frame of the video's own
    fixed logos/watermarks, [] when there are none. Raises on API trouble."""
    raw = _call(LOGO_PROMPT, api_key, max_tokens=512,
                images=[(f"Frame {i}:", b) for i, b in enumerate(jpegs)])
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    boxes = []
    for b in data.get("logos") or []:
        try:
            x0, y0, x1, y1 = (float(b[k]) / 1000 for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            continue
        # a logo is small: skip anything that would blur a big part of the picture
        w, h = x1 - x0, y1 - y0
        small = w <= 0.4 and h <= 0.25
        banner = w <= 0.8 and h <= 0.08          # a thin promo text line across the top/bottom
        if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1 and (small or banner):
            boxes.append((x0, y0, x1, y1))
    return boxes[:4]


# ---- AI-drawn cover art (like popular recap thumbnails) ----
IMAGE_MODEL = __import__("os").environ.get("DUBLAB_IMAGE_MODEL") or "gemini-3.1-flash-image"

COVER_SCENE_PROMPT = """You design YouTube thumbnails for a Burmese movie-recap channel.
Below is the story of a movie (narration lines). Invent ONE eye-catching thumbnail picture for it, in the
style of viral recap thumbnails: bold, dramatic, high-contrast digital illustration, cinematic lighting,
glowing effects, vivid saturated colours, expressive faces. Often a "before → after" or "hero vs. danger"
contrast, the main character big and in front, the key conflict or setting behind (e.g. a fishing story: the
hero on a small boat in a stormy sea with a huge catch; a kung-fu story: the hero mid-kick with golden energy
and the villain behind).
Write the image prompt in English, 60-110 words: who is in it (describe the main character's look, clothes,
age), what they do, the setting, mood, colours and composition. Leave the LOWER THIRD free of important
details (a title will be printed there). No text, letters, logos or watermarks in the picture.
Characters must be original illustrated people, not real actors' likenesses.
Answer with the prompt only."""


def write_cover_scene(lines, api_key, max_chars=8000):
    text, used = [], 0
    for ln in lines:
        if used + len(ln) > max_chars:
            break
        text.append(ln)
        used += len(ln) + 1
    return _call(COVER_SCENE_PROMPT + "\n\nStory:\n" + "\n".join(text), api_key, max_tokens=1024).strip()


def draw_cover(scene, refs, api_key, portrait=False):
    """Generate the cover picture. refs: a few JPEG frames from the movie,
    used only for the setting, costumes and colours. Returns JPEG/PNG bytes."""
    import base64
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_MODEL}"
           f":generateContent?key={api_key}")
    parts = [{"text": "Create this YouTube thumbnail illustration, " + ("vertical 9:16" if portrait else
              "wide 16:9") + ". The attached movie frames show the setting, costumes and colour mood only — "
              "draw original stylized characters, do not copy the real actors' faces. No text at all.\n\n"
              + scene}]
    for b in refs[:4]:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": "9:16" if portrait else "16:9"}}}
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240, context=_SSL) as r:
                data = json.loads(r.read().decode())
            for p in data["candidates"][0].get("content", {}).get("parts", []):
                inl = p.get("inline_data") or p.get("inlineData")
                if inl and inl.get("data"):
                    return base64.b64decode(inl["data"])
            last = RuntimeError("no image in the answer")
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            last = RuntimeError(f"HTTP {e.code}: {msg[:120]}")
            if e.code == 429 and "plan and billing" in msg:     # free plan: no image quota at all
                raise RuntimeError("image generation needs billing on the Gemini key "
                                   "(free plan has no image quota)") from None
            if e.code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code < 500:
                break
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"cover image failed: {last}")
