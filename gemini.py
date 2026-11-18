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
PARALLEL = 4     # chunks translated at the same time


NAME_RULE = """- CHARACTER NAMES: do NOT transliterate foreign character names. Give every character a familiar Burmese name,
  and never refer to a main character only as "ဒီကောင်လေး", "ဒီကောင်မလေး", "သူ", etc. when a name fits. Preferred names:
  young man / boy: ဂွေးတို, ကျော်ကြီး, လင်ဖူန်း, စိုးကြီး,အောင်လေး
  young woman / girl: ရှောင်မေ, မြမြ, ဂွက်ထော်, ဖြူဖြူထွေး, လှလှ
  older man: ကျော်ရဲ, ကျော်မျိုး, တင်မောင်
  older woman: ဒေါ်မြ, ဒေါ်လှ, ဂျမ်းစိန်
  little boy (child): လပ်ကီး, ဘနားနား
  baby / little girl: ဂျက်မ, ဂူးဂူး
  Use these first, in this order, for the most important characters; for more characters pick other short, common Burmese names.
  Each character keeps the SAME name every time; never give two characters the same name.
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


def _call(prompt, api_key, max_tokens=8192, images=None):
    """images: optional [(label, jpeg bytes)] sent after the prompt, each
    preceded by its label so the model can refer to it."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
           f":generateContent?key={api_key}")
    parts = [{"text": prompt}]
    for label, data in images or []:
        import base64
        parts += [{"text": label},
                  {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode()}}]
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7}}
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
If it names a real movie or series, give that movie's name the way people search for it — in its usual Latin
spelling, with the uploader's tricks undone: letters split by dashes, dots or spaces joined back
("R-R-R" → "RRR", "K-i-n-g O-f B-e-g-g-a-r-s" → "King of Beggars", "Na..... ay..... ak" → "Nayak"), and without
years, quality tags, language/subtitle notes (e.g. "မြန်မာစာတန်းထိုး", "Hindi", "HD", "1080p", "Full Movie"),
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
- Write Burmese in Myanmar script only — never Thai, Chinese or any other script inside Burmese words.
- Output format: one beat per line as "FROM|TO|narration" (FROM/TO = line numbers). Nothing else."""


def _recap_chunk(lines, lo, hi, minutes, lang, extra, api_key, what):
    numbered = "\n".join(f"{i}|{int(s // 60):02d}:{int(s % 60):02d}|{t}" for i, (s, t) in enumerate(lines[lo:hi], lo))
    chars = int(minutes * 60 * TTS_CPS)
    prompt = RECAP_PROMPT.format(what=what, lang=lang or "the original language",
                                 chars=chars, minutes=round(minutes, 1),
                                 beats=max(3, round(chars / BEAT_CHARS)), beat_chars=BEAT_CHARS,
                                 extra=extra) + "\n\nTranscript:\n" + numbered
    raw = _call(prompt, api_key, max_tokens=60000)
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

Output one character per line as "Burmese name = who they are (role, gender, age; their original name if known)".
Nothing else."""


def write_cast(lines, api_key, lang=None, max_chars=60000):
    """Burmese names for the characters, decided once for the whole film so
    every parallel recap section calls them the same."""
    text = "\n".join(t for _, t in lines)
    if len(text) > max_chars:                     # keep beginning, middle and end
        third = max_chars // 3
        mid = len(text) // 2
        text = text[:third] + "\n…\n" + text[mid - third // 2: mid + third // 2] + "\n…\n" + text[-third:]
    raw = _call(CAST_PROMPT.format(lang=lang or "original language") + "\n\nTranscript:\n" + text,
                api_key, max_tokens=2000)
    rows = [r.strip(" -*") for r in raw.strip().splitlines() if "=" in r]
    return "\n".join(rows[:12])


def write_recap(lines, minutes, api_key, lang=None, channel=None, min_minutes=0, log=None):
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
    if cast:
        if log:
            log("characters: " + " · ".join(r.split("=")[0].strip() for r in cast.splitlines()))
        extra += ("\n- USE EXACTLY THESE CHARACTER NAMES (decided for the whole film; every section must "
                  "call each character the same):\n" + cast)

    def job(i):
        what = "a video" if k == 1 else f"part {i + 1} of {k} of a video (continue the story; do not re-introduce it)"
        return _recap_chunk(lines, bounds[i], bounds[i + 1], per, lang, extra, api_key, what)

    with ThreadPoolExecutor(min(k, RECAP_PARALLEL)) as ex:
        parts = list(ex.map(job, range(k)))
        # each section must carry its share of the minimum (+8% headroom);
        # ask again for the short ones, scaled up by how short they came out
        need = min_minutes * 60 * TTS_CPS * 1.08 / k
        for rnd in range(3):
            short = [i for i in range(k) if _chars(parts[i]) < need]
            if not min_minutes or not short:
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
                return _recap_chunk(lines, bounds[i], bounds[i + 1], per * grow, lang, extra, api_key, what)

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
_FOREIGN = re.compile("[\u0E00-\u0EFF\u1780-\u17FF\u0900-\u09FF\u0B80-\u0BFF"
                      "\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF\u0400-\u04FF\u0600-\u06FF]")

FIX_PROMPT = """These Burmese lines wrongly contain characters from other scripts (e.g. Thai, Chinese).
Rewrite each line in correct Burmese (Myanmar script) only, keeping the meaning and style.
Names may stay in Latin letters. Output exactly one line per input as "N|||line". Nothing else."""


def has_foreign(text):
    return bool(_FOREIGN.search(text or ""))


def fix_script(lines, api_key):
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
    return [re.sub(r"\s{2,}", " ", _FOREIGN.sub("", t)).strip() if has_foreign(t) else t for t in out]


# ---- cover photos: let the model find the main character ----
COVER_PROMPT = """The numbered frames below (0 to {last}) come from one movie.
First work out who the MAIN CHARACTER is (the protagonist: the person who appears most and the story is about).
Then answer with JSON only, no other text:
{{"hero": [...], "others": [...]}}
- "hero": up to 5 frame numbers where the main character's FACE is clearly visible, large and sharp (close-up or medium shot, looking good, dramatic expression is a plus), best first.
- "others": up to 8 frame numbers, best first, that together show what this movie is ABOUT: its main scenes and setting (e.g. a fishing movie: people fishing, the small boat, the sea; a war movie: soldiers, tanks, battle; a fantasy: the magic, creatures, the kingdom), plus the other important characters (villain, partner, love interest) with their faces visible. Mix scene shots and character shots.
Never pick dark, blurry, black, fading, crowd-only, landscape-only, logo, title or text frames. A frame may appear in only one list."""


def pick_cover_frames(jpegs, api_key):
    """jpegs: [bytes]. Returns (hero indices, other indices); raises on failure."""
    raw = _call(COVER_PROMPT.format(last=len(jpegs) - 1), api_key, max_tokens=1024,
                images=[(f"Frame {i}:", b) for i, b in enumerate(jpegs)])
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    ok = lambda xs: [int(x) for x in xs or [] if str(x).isdigit() and 0 <= int(x) < len(jpegs)]
    hero = list(dict.fromkeys(ok(data.get("hero"))))
    others = [i for i in dict.fromkeys(ok(data.get("others"))) if i not in hero]
    if not hero and not others:
        raise ValueError("no cover frames picked")
    return hero, others


# ---- find the burned-in captions to blur ----
CAPTION_BAND_PROMPT = """These frames come from one video. Some videos have subtitles burned into the picture
(e.g. English or Chinese text lines, usually near the bottom). Look at ALL frames together.
If there is burned-in subtitle text, give the vertical area that covers it in every frame where it appears,
on a 0-1000 scale of the frame height measured from the TOP (0 = top edge, 1000 = bottom edge).
Cover the whole text including its outline/shadow, and both lines when there are two.
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
