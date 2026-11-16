# DubLab — English ↔ Burmese Video Dubbing

**Upload a short video, get it back dubbed in the other language.**

DubLab is a self-hosted web app that dubs short videos between **English and
Burmese (Myanmar)** — a low-resource language pair with almost no open-source
dubbing tooling. It transcribes the speech, translates it, synthesizes a new
voice track in the target language, syncs it to the picture, and returns a
finished MP4.

🌐 **Live demo:** https://dublab.kbko.qzz.io
📄 **License:** MIT

---

## Why this exists

Burmese is spoken by 50M+ people, yet creators who want to dub content
between English and Burmese have almost no free, open tooling: commercial
dubbing services rarely support Burmese voices, and generic pipelines break
on Burmese text segmentation and speech timing. DubLab is built specifically
for this pair and runs in production for Myanmar content creators.

## Features

- 🎬 **Video dubbing** — upload a video (≤ 30 min, ≤ 1 GB by default) **or paste a YouTube / TikTok link**, download the dubbed MP4
- 🎵 **Keeps the background, drops the original voices** — [Demucs](https://github.com/facebookresearch/demucs) strips the original speech so only music and sound effects stay under the new voice (dipped slightly while it speaks); runs on Apple GPU / CUDA when available; can be switched off per video
- 🌐 **Auto language detection → Burmese** — no language to pick: whisper detects the spoken language (Chinese, English, Japanese, Korean, …) and the video is dubbed into spoken Burmese, with tuned prompts for Chinese recaps
- ✏️ **Check the translation before voicing** — review and edit every Burmese line (or blank it to skip) before the voice is generated
- 📨 **Send to Telegram** — the finished dub (or a download link when it is over the 50 MB bot limit) plus the caption file is sent to your Telegram by your own bot, automatically or with one tap; owner-only (admin password)
- 📝 **Burmese captions** — the dubbed lines burned in as captions, timed to the new voice (plus an `.srt` download); optionally blur the original burned-in captions first. Needs `libraqm` for correct Burmese shaping (`brew install libraqm`)
- 🏷️ **Channel logo** — upload a logo on the admin page, pick its corner and size, and tick it per video; burned in with the Apple hardware encoder when available
- 📢 **Channel outro** — an optional closing line (e.g. “like and follow my channel”) spoken in the same voice at the end; set it on the admin page or with `DUBLAB_OUTRO`
- 🤖 **Auto voice** — picks the male or female Burmese voice from the original speaker's pitch (or choose one yourself)
- 🗣️ **Burmese + English voices** — male and female voices both directions
  (`my-MM-ThihaNeural`, `my-MM-NilarNeural`, `en-US-AndrewNeural`, `en-US-AvaNeural`)
- 🎙️ **Local transcription** — [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your own machine, no audio leaves your server for STT
- 🌏 **Translation** — Gemini API (free tier works), tuned prompts for subtitle-style lines
- ⏱️ **Sync engine that never cuts a sentence** — speech is time-fitted up to a
  capped speed-up (1.65×); overflow cascades into the next slot instead of
  truncating, so sentence tails are never lost
- 🔁 **TTS with fallbacks + key rotation** — Edge-TTS primary, Gemini TTS
  fallback with multi-key rotation and rate-limit cooldowns, gTTS last resort
- 💧 **Credit watermark** — small lower-right watermark (configurable text)
- 🛡️ **Abuse protection built in** — job queue cap, per-IP upload rate limit,
  24 h result retention, optional admin page for user reviews

## How it works

```
upload ──► extract audio (ffmpeg) ──► transcribe (faster-whisper)
       ──► translate lines (Gemini) ──► synthesize voices (Edge-TTS / Gemini TTS)
       ──► time-fit + cascade placement ──► mux + watermark (ffmpeg) ──► MP4
```

| File | Role |
|---|---|
| `app.py` | Flask web app: upload, job queue, status polling, download, reviews/admin |
| `dub.py` | Dubbing pipeline: audio extract, transcription, TTS orchestration, sync engine, mux |
| `gemini.py` | Translation via Gemini API |
| `gemini_tts.py` | Gemini TTS with key rotation, cooldown and invalid-key handling |
| `edge_probe.py` | Tiny helper to probe Edge-TTS endpoint health |

## Quick start

Requirements: Python 3.11+, `ffmpeg`, and a free
[Gemini API key](https://aistudio.google.com/apikey).

```bash
git clone https://github.com/khantbhoneko36-commits/dublab.git
cd dublab
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # put your GEMINI_API_KEY in .env
./run.sh              # loads .env, starts waitress on 127.0.0.1:8092
```

Override defaults if needed: `PORT=9000 THREADS=4 ./run.sh`.

Open http://127.0.0.1:8092, drop in a short video, pick a direction and a
voice, and download the dubbed result.

> 🔗 Link input uses [yt-dlp](https://github.com/yt-dlp/yt-dlp). Sites change often — run
> `pip install -U yt-dlp` when downloads start failing. YouTube may block
> datacenter/cloud IPs; a home connection works more reliably.

> ⚠️ Dependency pins matter: `requirements.txt` pins `av==15.1.0` and
> `huggingface_hub<1` — newer versions break faster-whisper 1.2.x.

### Public HTTPS link (optional, ngrok)

Lets you use DubLab from your phone, and makes Telegram download links work for large files. Install ngrok (`brew install ngrok`),
add your authtoken (`ngrok config add-authtoken …`), put your free static
domain in `.env` as `NGROK_DOMAIN=…`, then run `./run.sh` and, in a second
terminal, `./ngrok`.

### Terms / Privacy site on Netlify (optional)

Static Terms of Service and Privacy Policy pages that stay online. `netlify.toml` builds them as a static site from `templates/legal.html`
(the same pages the app serves at `/terms` and `/privacy`): connect the repo in
Netlify and set `CONTACT_EMAIL` and `APP_NAME` as environment variables there.
Extra static files (e.g. site-verification files) go in `site_static/`.
Build locally with `python scripts/build_site.py`.

### Docker

```bash
docker build -t dublab .
docker run -d --name dublab -p 127.0.0.1:8092:8092 --env-file .env dublab
```

### Production (systemd)

See `dublab.service.example` — run behind nginx/Caddy as a reverse proxy with
HTTPS, exactly like the live deployment.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | Translation (and TTS fallback if no TTS keys set) |
| `GEMINI_TTS_API_KEYS` | — | Comma-separated keys for Gemini TTS rotation |
| `DUBLAB_CHANNEL_NAME` | Marco Recap | Your channel name: the translator puts it in place of the original creator's channel name, and the outro is skipped when the video already ends with that shout-out (editable on the admin page) |
| `DUBLAB_OUTRO` | — | Default closing line spoken at the end of every dub (editable on the admin page); empty = none |
| `DUBLAB_MAIL_TO` | — | E-mail address that receives every new review/feedback |
| `DUBLAB_SMTP_USER` / `DUBLAB_SMTP_PASSWORD` | — | SMTP login for sending it (Gmail: address + [App Password](https://myaccount.google.com/apppasswords)); `DUBLAB_SMTP_HOST` / `DUBLAB_SMTP_PORT` default to Gmail SSL |
| `TELEGRAM_BOT_TOKEN` | — | Token of your bot from @BotFather; then send `/start` to the bot and press Connect on the admin page |
| `PUBLIC_URL` | — | Public address used in Telegram download links for files over 50 MB (defaults to `https://$NGROK_DOMAIN`) |
| `DUBLAB_ADMIN_KEY` | — | Password for the admin page (`/admin/reviews?key=…`): reviews + settings |
| `DUBLAB_WATERMARK` | — | Watermark text (default: `made with DubLab`) |
| `DUBLAB_MAX_MINUTES` | — | Default longest video, in minutes (default `30`); can be changed live on the admin page (saved to `settings.json`) |
| `DUBLAB_MAX_MB` | — | Largest upload/download, in MB (default `3000`); `run.sh` sizes waitress's upload limit from it — when starting waitress yourself, pass `--max-request-body-size` |
| `DUBLAB_BG_VOLUME` | — | Volume of the background music/effects under the dub, `0`–`1` (default `0.8`; `0` = none) |
| `DUBLAB_DEMUCS_DEVICE` | — | Force Demucs device: `mps` / `cuda` / `cpu` (default: auto) |
| `DUBLAB_WHISPER_MODEL` | — | Speech model preselected in the UI: `small` (default — accurate, best for Chinese) or `base` (~2× faster); users can switch per video. Runs on Apple GPU / CUDA when available |

## မြန်မာဘာသာ အကျဉ်းချုပ်

DubLab ဆိုတာ ဗီဒီယိုအတိုတွေကို **အင်္ဂလိပ် ↔ မြန်မာ** အပြန်အလှန်
အသံသွင်း (dub) လုပ်ပေးတဲ့ open-source web app တစ်ခုပါ။ ဗီဒီယိုတင်လိုက်ရင်
စကားပြောကို စာသားအဖြစ်ပြောင်း၊ ဘာသာပြန်၊ မြန်မာအသံ (ကျား/မ) နဲ့
အသံအသစ်ဖော်ပြီး၊ စာကြောင်းမပြတ်အောင် အချိန်ကိုက် ညှိပေးကာ
dub လုပ်ပြီးသား MP4 ပြန်ချပေးပါတယ်။ မြန်မာဘာသာအတွက် ဒီလို tooling
အရမ်းရှားတာမို့ Myanmar creator တွေ အခမဲ့ self-host လုပ်သုံးနိုင်အောင်
ထုတ်ပေးထားတာ ဖြစ်ပါတယ်။ Live သုံးကြည့်ချင်ရင်
https://dublab.kbko.qzz.io မှာ စမ်းနိုင်ပါတယ်။

## Contributing

Issues and pull requests are welcome — especially Burmese language fixes
(translation prompts, segmentation, voice timing) and new TTS/STT backends.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Maintainer

Built and maintained by **Khant Bhone Ko** ([@khantbhoneko36-commits](https://github.com/khantbhoneko36-commits)) —
sole maintainer: releases, issue triage, and the production deployment.
More projects: https://pro.kxant.org
