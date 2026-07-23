#!/usr/bin/env python3
"""
Génère un flux RSS des biens à vendre de renoiretvaloris.com.

Fonctionnement :
1. Découvre les URLs d'annonces (sitemap.xml, puis fallback : pages /vente).
2. Compare avec l'état local (state.json) pour repérer les nouveautés.
3. Pour chaque annonce, scrape la page de détail (prix, surface, localisation,
   photo, description...).
4. Écrit feed.xml (RSS 2.0, avec image en enclosure + description enrichie).

Usage : python renoir_valoris_rss.py
À lancer en cron, ex : 0 * * * * (toutes les heures).

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
STATE_FILE = Path(__file__).parent / "state.json"
FEED_FILE = Path(__file__).parent / "feed.xml"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

# Regex d'une URL d'annonce vente : /vente/slug,VM1234 (VM=vente maison/appart, réf Netty)
AD_URL_RE = re.compile(r'href="(/vente/[^"]+,V[A-Z]?\d+)"')


def http_get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r


def discover_from_sitemap():
    """Les sites Netty publient généralement un sitemap avec toutes les annonces."""
    urls = set()
    try:
        r = http_get(f"{BASE}/sitemap.xml")
        locs = re.findall(r"<loc>(.*?)</loc>", r.text)
        # sitemap index -> suivre les sous-sitemaps
        subs = [l for l in locs if l.endswith(".xml")]
        pages = [l for l in locs if not l.endswith(".xml")]
        for sub in subs:
            try:
                pages += re.findall(r"<loc>(.*?)</loc>", http_get(sub).text)
            except Exception:
                pass
        for u in pages:
            if re.search(r"/vente/.+,V[A-Z]?\d+$", u):
                urls.add(u)
    except Exception as e:
        print(f"[sitemap] échec : {e}", file=sys.stderr)
    return urls


def discover_from_listing_pages(max_pages=15):
    """Fallback : extraire les liens d'annonces du HTML brut des pages /vente."""
    urls = set()
    for page in range(1, max_pages + 1):
        url = f"{BASE}/vente" if page == 1 else f"{BASE}/vente/{page}"
        try:
            html = http_get(url).text
        except Exception:
            break
        found = {BASE + m for m in AD_URL_RE.findall(html)}
        if not found and page > 1:
            break
        urls |= found
        time.sleep(0.5)
    return urls


def scrape_detail(url):
    """Extrait les infos d'une page d'annonce (HTML statique côté Netty)."""
    soup = BeautifulSoup(http_get(url).text, "html.parser")
    d = {"url": url}

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return tag["content"].strip() if tag and tag.get("content") else None

    d["title"] = meta("og:title") or (soup.title.string.strip() if soup.title else url)
    d["image"] = meta("og:image")
    d["meta_desc"] = meta("og:description") or ""

    text = soup.get_text("\n", strip=True)

    # Prix : premier montant en € proche du titre (format "550 000 €")
    m = re.search(r"\b(\d{1,3}(?:[  .]\d{3})+|\d{4,})\s*€", text)
    d["price"] = m.group(0).replace("\u202f", " ") if m else None

    # Champs de la section "En détails" (libellé collé à la valeur dans le texte)
    def field(label, pattern):
        m = re.search(label + r"\s*" + pattern, text)
        return m.group(1).strip() if m else None

    d["surface"] = field(r"Surface", r"(\d+(?:[.,]\d+)?)\s*m²")
    d["terrain"] = field(r"Terrain", r"(\d+(?:[.,]\d+)?)\s*m²")
    d["rooms"] = field(r"Pièces", r"(\d+)")
    d["bedrooms"] = field(r"Chambres", r"(\d+)")
    d["location"] = field(r"Localisation", r"([^\n]+)")
    d["reference"] = field(r"Référence", r"([A-Z]{1,3}\d+)")
    d["dpe"] = field(r"Classe énergie", r"([A-G])")

    # Description longue : bloc après "Descriptif du bien"
    m = re.search(r"Descriptif du bien\s*\n(.*?)(?:\n\+33|\nEnvoyer un mail|\nEn détails)",
                  text, re.S)
    d["description"] = m.group(1).strip() if m else d["meta_desc"]

    return d


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    urls = discover_from_sitemap()
    if not urls:
        print("[info] sitemap vide, fallback sur les pages /vente")
        urls = discover_from_listing_pages()
    if not urls:
        sys.exit("Aucune annonce trouvée — le site a peut-être changé de structure.")
    print(f"[info] {len(urls)} annonces découvertes")

    now = datetime.now(timezone.utc).isoformat()

    # Nouvelles annonces -> scraper le détail ; annonces connues -> réutiliser le cache
    for url in sorted(urls):
        if url in state:
            continue
        try:
            data = scrape_detail(url)
            data["first_seen"] = now
            state[url] = data
            print(f"[nouveau] {data['title']}")
            time.sleep(1)  # rester poli avec le serveur
        except Exception as e:
            print(f"[erreur] {url} : {e}", file=sys.stderr)

    # Marquer les annonces disparues (vendues/retirées) sans les supprimer du state
    for url in state:
        state[url]["active"] = url in urls

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    # --- Génération du flux RSS ---
    fg = FeedGenerator()
    fg.id(f"{BASE}/vente")
    fg.title("Renoir & Valoris — Biens à vendre")
    fg.link(href=f"{BASE}/vente", rel="alternate")
    fg.description("Nouvelles annonces immobilières Renoir & Valoris (Nancy)")
    fg.language("fr")

    active = [d for d in state.values() if d.get("active")]
    active.sort(key=lambda d: d["first_seen"], reverse=True)

    for d in active[:50]:
        fe = fg.add_entry()
        fe.id(d["url"])
        fe.link(href=d["url"])

        bits = [b for b in (d.get("price"),
                            f"{d['surface']} m²" if d.get("surface") else None,
                            d.get("location")) if b]
        fe.title(f"{' — '.join(bits)}" if bits else d["title"])

        html_desc = ""
        if d.get("image"):
            html_desc += f'<img src="{d["image"]}" width="400"/><br/>'
        html_desc += f"<b>{d['title']}</b><br/>"
        details = []
        if d.get("price"): details.append(f"Prix : {d['price']}")
        if d.get("surface"): details.append(f"Surface : {d['surface']} m²")
        if d.get("terrain"): details.append(f"Terrain : {d['terrain']} m²")
        if d.get("rooms"): details.append(f"Pièces : {d['rooms']}")
        if d.get("bedrooms"): details.append(f"Chambres : {d['bedrooms']}")
        if d.get("location"): details.append(f"Localisation : {d['location']}")
        if d.get("dpe"): details.append(f"DPE : {d['dpe']}")
        if d.get("reference"): details.append(f"Réf. {d['reference']}")
        html_desc += " • ".join(details)
        if d.get("description"):
            html_desc += f"<br/><br/>{d['description'][:1500]}"
        fe.description(html_desc)

        if d.get("image"):
            fe.enclosure(d["image"], 0, "image/jpeg")
        fe.pubDate(datetime.fromisoformat(d["first_seen"]))

    fg.rss_file(str(FEED_FILE), pretty=True)
    print(f"[ok] {FEED_FILE} généré — {len(active)} annonces actives")


if __name__ == "__main__":
    main()
