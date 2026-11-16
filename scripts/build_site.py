"""Build the static Terms/Privacy site for Netlify (or any static host).

Renders templates/legal.html (the same pages the app serves at /terms and
/privacy) into site_dist/, inlining static/style.css, and copies everything in
site_static/ (e.g. TikTok's tiktok*.txt URL-verification files).

Settings come from environment variables so nothing personal lives in the repo:
  CONTACT_EMAIL   e-mail shown as the contact address (optional)
  APP_NAME        name shown on the pages (default: DubLab)

Run locally:  CONTACT_EMAIL=me@example.com APP_NAME="My App" python scripts/build_site.py
"""
import os
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site_dist"


def main():
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("legal.html")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    email = os.environ.get("CONTACT_EMAIL", "").strip()
    name = os.environ.get("APP_NAME", "").strip() or "DubLab"

    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    for page in ("terms", "privacy"):
        html = tpl.render(page=page, email=email, app_name=name)
        html = html.replace('<link rel="stylesheet" href="/static/style.css">', f"<style>{css}</style>")
        html = html.replace('href="/terms"', 'href="/terms/"').replace('href="/privacy"', 'href="/privacy/"')
        html = html.replace(f'<a href="/">← {name}</a> · ', "")   # no app home on the static site
        (OUT / page).mkdir()
        (OUT / page / "index.html").write_text(html, encoding="utf-8")
    (OUT / "index.html").write_text(
        f'<!doctype html><html lang="en"><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{name}</title><style>{css}</style><main><section class="card">'
        f'<h2>{name}</h2><p><a href="/terms/">Terms of Service</a> · '
        f'<a href="/privacy/">Privacy Policy</a></p></section></main></html>', encoding="utf-8")
    for f in (ROOT / "site_static").glob("*"):
        if f.is_file() and f.name != ".gitkeep":
            shutil.copy2(f, OUT / f.name)
    print(f"built {OUT.relative_to(ROOT)}/ (contact: {'set' if email else 'none'}, name: {name})")


if __name__ == "__main__":
    main()
