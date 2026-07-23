#!/usr/bin/env python3
"""
Génère un flux RSS des articles de blog de renoiretvaloris.com.

Même logique que le feed des annonces :
1. Découvre les URLs d'articles (sitemap.xml, fallback : page /blog et
   plan du site) — identifiables par "/blog/" dans l'URL.
2. Compare avec state_blog.json pour repérer les nouveautés
   (first_seen = date de première détection = pubDate RSS).
3. Scrape chaque nouvel article (titre, image, chapeau/description).
4. Écrit feed_blog.xml (RSS 2.0).

Usage : python renoir_valoris_blog_rss.py
Dépendances : pip install requests beautifulsoup4 feedgen
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

BASE = "https://www.renoiretvaloris.com"
STATE_FILE = Path(__file__).parent / "state_blog.json"
FEED_FILE = Path(__file__).parent / "feed_blog.xml"
MAX_ITEMS = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

# URL d'article : /blog/slug,123 (id numérique Netty en fin d'URL)
BLOG_URL_RE = re.compile(r'href="(?:https?://[^/"]+)?(/blog/[^"?#]+,\d+)"')


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


def scrape_article(url):
    """Extrait titre, image et description d'un article (HTML statique)."""
    soup = BeautifulSoup(http_get(url).text, "html.parser")
    d = {"url": url}

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return tag["content"].strip() if tag and tag.get("content") else None

    d["title"] = meta("og:title") or (soup.title.string.strip() if soup.title else url)
    d["image"] = meta("og:image")
    d["summary"] = meta("og:description") or meta("description") or ""

    # Extrait du corps de l'article : texte après le H1, hors nav/footer.
    h1 = soup.find("h1")
    excerpt = ""
    if h1:
        parts = []
        for el in h1.find_all_next(["p", "h2", "h3", "li"]):
            t = el.get_text(" ", strip=True)
            if not t:
                continue
            # stop aux blocs de fin de page
            if t.startswith(("Je recherche un bien", "Prénom", "J'accepte le traitement")):
                break
            parts.append(t)
            if sum(len(x) for x in parts) > 1200:
                break
        excerpt = "\n".join(parts)
    d["excerpt"] = excerpt or d["summary"]

    return d


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    urls = discover_from_sitemap()
    if not urls:
        print("[info] sitemap vide, fallback sur /blog et le plan du site")
        urls = discover_from_pages()
    if not urls:
        sys.exit("Aucun article trouvé — le site a peut-être changé de structure.")
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
    fg.title("Renoir & Valoris — Le blog")
    fg.link(href=f"{BASE}/blog", rel="alternate")
    fg.description("Actualités et conseils immobiliers Renoir & Valoris (Nancy)")
    fg.language("fr")

    active = [d for d in state.values() if d.get("active")]
    active.sort(key=lambda d: d["first_seen"], reverse=True)

    for d in active[:MAX_ITEMS]:
        fe = fg.add_entry()
        fe.id(d["url"])
        fe.link(href=d["url"])
        fe.title(d["title"])

        html_desc = ""
        if d.get("image"):
            html_desc += f'<img src="{d["image"]}" width="400"/><br/>'
        if d.get("summary"):
            html_desc += f"<b>{d['summary']}</b><br/><br/>"
        if d.get("excerpt"):
            html_desc += d["excerpt"][:1500]
        fe.description(html_desc or d["title"])

        if d.get("image"):
            fe.enclosure(d["image"], 0, "image/jpeg")
        fe.pubDate(datetime.fromisoformat(d["first_seen"]))

    fg.rss_file(str(FEED_FILE), pretty=True)
    print(f"[ok] {FEED_FILE} généré — {len(active)} articles actifs")


if __name__ == "__main__":
    main()
