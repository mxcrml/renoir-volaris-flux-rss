#!/usr/bin/env python3
"""
Génère un flux RSS des articles de blog de renoiretvaloris.com.

1. Découvre les URLs d'articles (sitemap.xml, fallback : page /blog et
   plan du site) - identifiables par "/blog/" dans l'URL.
2. Compare avec state_blog.json pour repérer les nouveautés
   (first_seen = date de première détection = pubDate RSS).
3. Scrape chaque nouvel article : le contenu principal (<main>) est
   converti proprement en Markdown, puis nettoyé et tronqué.
4. Écrit feed_blog.xml (RSS 2.0).

Usage : python renoir_valoris_blog_rss.py
Dépendances : pip install requests beautifulsoup4 feedgen markdownify
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from markdownify import markdownify

BASE = "https://www.renoiretvaloris.com"
STATE_FILE = Path(__file__).parent / "state_blog.json"
FEED_FILE = Path(__file__).parent / "feed_blog.xml"
MAX_ITEMS = 30
CONTENT_MAX = 2500      # taille max du contenu markdown dans le feed
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

# URL d'article : /blog/slug,123 (id numérique Netty en fin d'URL)
BLOG_URL_RE = re.compile(r'href="(?:https?://[^/"]+)?(/blog/[^"?#]+,\d+)"')

# Marqueurs de fin de contenu : tout ce qui suit est coupé
STOP_PATTERNS = [
    re.compile(r"^#*\s*\+?\s*\d[\d .]{8,}\s*$", re.M),        # téléphone
    re.compile(r"^#*\s*\S+@\S+\.(fr|com)\s*$", re.M),          # email
    re.compile(r"^.{0,10}Envie de conna", re.M | re.I),        # bloc estimation
    re.compile(r"^.{0,10}Toujours plus d", re.M | re.I),       # articles suggérés
    re.compile(r"^.{0,10}Suivez notre actualité", re.M | re.I),
    re.compile(r"^.{0,10}Je recherche un bien", re.M | re.I),
    re.compile(r"^.{0,10}Vous souhaitez faire estimer", re.M | re.I),
    re.compile(r"^.{0,10}Partager l['']article", re.M | re.I),
]

# Tags à supprimer du conteneur avant conversion
JUNK_TAGS = ["nav", "header", "footer", "form", "script", "style",
             "iframe", "button", "input", "select", "textarea", "svg", "noscript"]


def http_get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r


def discover_from_sitemap():
    urls = set()
    try:
        r = http_get(f"{BASE}/sitemap.xml")
        locs = re.findall(r"<loc>(.*?)</loc>", r.text)
        subs = [l for l in locs if l.endswith(".xml")]
        pages = [l for l in locs if not l.endswith(".xml")]
        for sub in subs:
            try:
                pages += re.findall(r"<loc>(.*?)</loc>", http_get(sub).text)
            except Exception:
                pass
        for u in pages:
            if re.search(r"/blog/.+,\d+$", u):
                urls.add(u)
    except Exception as e:
        print(f"[sitemap] échec : {e}", file=sys.stderr)
    return urls


def discover_from_pages():
    """Fallback : liens /blog/ dans la page /blog et le plan du site (statiques)."""
    urls = set()
    for page_url in (f"{BASE}/blog", f"{BASE}/plan-du-site,114"):
        try:
            html = http_get(page_url).text
            urls |= {BASE + m for m in BLOG_URL_RE.findall(html)}
        except Exception as e:
            print(f"[fallback] {page_url} : {e}", file=sys.stderr)
    return urls


def html_to_markdown(container) -> str:
    """Convertit le conteneur principal en Markdown propre."""
    # supprimer les blocs non éditoriaux
    for tag in container.find_all(JUNK_TAGS):
        tag.decompose()
    # supprimer les images (souvent vides/décoratives ici ; la cover est
    # fournie séparément via og:image)
    for img in container.find_all("img"):
        img.decompose()
    # les liens deviennent du texte simple (pas de [txt](url) parasites)
    for a in container.find_all("a"):
        a.replace_with(a.get_text(" ", strip=True))

    md = markdownify(str(container), heading_style="ATX", bullets="-")

    # nettoyage
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)      # images résiduelles
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def cut_at_stop_markers(md: str) -> str:
    """Coupe le markdown au premier marqueur de fin de contenu."""
    cut = len(md)
    for pat in STOP_PATTERNS:
        m = pat.search(md)
        if m and m.start() < cut:
            cut = m.start()
    return md[:cut].strip()


def start_at_title(md: str, title: str) -> str:
    """Si des restes de menu précèdent le contenu, démarre au 1er titre #."""
    m = re.search(r"^#\s+.+$", md, re.M)
    if m:
        return md[m.start():].strip()
    # sinon, tenter de démarrer à la 1re occurrence du début du titre
    key = title[:25]
    idx = md.find(key)
    return md[idx:].strip() if idx > 0 else md


def truncate(md: str, limit: int) -> str:
    if len(md) <= limit:
        return md
    cut = md.rfind(" ", 0, limit)
    return md[: cut if cut > 0 else limit].rstrip() + "..."


def scrape_article(url):
    """Extrait titre, image et contenu Markdown d'un article."""
    soup = BeautifulSoup(http_get(url).text, "html.parser")
    d = {"url": url}

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return tag["content"].strip() if tag and tag.get("content") else None

    d["title"] = meta("og:title") or (soup.title.string.strip() if soup.title else url)
    d["image"] = meta("og:image")

    # conteneur principal : <main>, sinon <article>, sinon <body> nettoyé
    container = soup.find("main") or soup.find("article") or soup.body
    md = html_to_markdown(container) if container else ""
    md = start_at_title(md, d["title"])
    md = cut_at_stop_markers(md)
    md = truncate(md, CONTENT_MAX)

    if not md:
        md = meta("og:description") or d["title"]
    d["content_md"] = md
    return d


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    urls = discover_from_sitemap()
    if not urls:
        print("[info] sitemap vide, fallback sur /blog et le plan du site")
        urls = discover_from_pages()
    if not urls:
        sys.exit("Aucun article trouvé - le site a peut-être changé de structure.")
    print(f"[info] {len(urls)} articles découverts")

    now = datetime.now(timezone.utc).isoformat()

    for url in sorted(urls):
        if url in state:
            continue
        try:
            data = scrape_article(url)
            data["first_seen"] = now
            state[url] = data
            print(f"[nouveau] {data['title']}")
            time.sleep(1)  # rester poli avec le serveur
        except Exception as e:
            print(f"[erreur] {url} : {e}", file=sys.stderr)

    for url in state:
        state[url]["active"] = url in urls

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    # --- Génération du flux RSS ---
    fg = FeedGenerator()
    fg.id(f"{BASE}/blog")
    fg.title("Renoir & Valoris - Le blog")
    fg.link(href=f"{BASE}/blog", rel="alternate")
    fg.description("Actualités et conseils immobiliers Renoir & Valoris (Nancy)")
    fg.language("fr")

    active = [d for d in state.values() if d.get("active")]
    active.sort(key=lambda d: d["first_seen"], reverse=True)

    for d in reversed(active[:MAX_ITEMS]):
        fe = fg.add_entry()
        fe.id(d["url"])
        fe.link(href=d["url"])
        fe.title(d["title"])
        # compat anciennes entrées du state (champ excerpt)
        fe.description(d.get("content_md") or d.get("excerpt") or d["title"])
        if d.get("image"):
            fe.enclosure(d["image"], 0, "image/jpeg")
        fe.pubDate(datetime.fromisoformat(d["first_seen"]))

    fg.rss_file(str(FEED_FILE), pretty=True)
    print(f"[ok] {FEED_FILE} généré - {len(active)} articles actifs")


if __name__ == "__main__":
    main()
