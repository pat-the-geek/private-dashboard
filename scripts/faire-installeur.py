#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the dashboard installer: a single file that carries everything.

⚠️ The installer is a PRODUCT, never a source. It is rebuilt from the real files every time —
otherwise we would keep two copies of the page and the generator, and they would drift apart
the day one is fixed without a thought for the other.

    python3 scripts/faire-installeur.py            # → installeur-tableau-de-bord.py
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "installeur-tableau-de-bord.py"

# What travels: the text as it is, the images in base64.
TEXTES = {
    "scripts/tableau-de-bord.py": RACINE / "scripts" / "tableau-de-bord.py",
    "scripts/ventes-appstore.py": RACINE / "scripts" / "ventes-appstore.py",
    "tableau-de-bord/index.html": RACINE / "tableau-de-bord" / "index.html",
    "tableau-de-bord/style.css": RACINE / "tableau-de-bord" / "style.css",
    "tableau-de-bord/nginx.conf": RACINE / "tableau-de-bord" / "nginx.conf",
    "tableau-de-bord/icone.svg": RACINE / "tableau-de-bord" / "icone.svg",
    "tableau-de-bord/manifest.webmanifest": RACINE / "tableau-de-bord" / "manifest.webmanifest",
    "tableau-de-bord/config.exemple.json": RACINE / "tableau-de-bord" / "config.exemple.json",
    "tableau-de-bord/README.md": RACINE / "tableau-de-bord" / "README.md",
    "tableau-de-bord/LICENSE": RACINE / "tableau-de-bord" / "LICENSE",
}


# ── What must NEVER travel ────────────────────────────────────────────────────────────
# A repository using this builder is an instance: it carries a wordmark, apps, a key, a
# machine name. The installer is a product: it must know none of that. The build fails if
# any one of those patterns survives in the package — proof rather than hope.
#
# ⛔ The patterns live in `tableau-de-bord/marqueurs.txt`, which stays in YOUR repository and
# is never shipped. Published, that list would name precisely what it exists to hide.
MARQUEURS = RACINE / "tableau-de-bord" / "marqueurs.txt"


def interdits() -> tuple[str, ...]:
    if not MARQUEURS.exists():
        print(f"· no {MARQUEURS.name}: nothing is being checked for", file=sys.stderr)
        return ()
    return tuple(l.strip().lower() for l in MARQUEURS.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#"))


INTERDITS = interdits()

# The trim files are STATIC: no configuration changes them afterwards, unlike the page's
# title and wordmark, which come from donnees.json. So we build neutral ones here, without
# touching the repository's, which belong to our own instance.
ICONE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <!-- Starting icon. The installer redraws both PNGs from your configuration's "marque"
       and "accent"; this SVG stays as it is. -->
  <rect width="64" height="64" rx="14" fill="#000000"/>
  <text font-family="Arial Black, Arial, sans-serif" font-weight="900" font-size="30"
        fill="#EDEBE4" x="32" y="30" text-anchor="middle">AB</text>
  <rect fill="#E8972E" x="5" y="38" width="18" height="7" rx="2"/>
  <text font-family="Arial Black, Arial, sans-serif" font-weight="900" font-size="30"
        fill="#EDEBE4" x="48" y="58" text-anchor="middle">cd</text>
</svg>
"""
MANIFESTE = {
    "name": "Tableau de bord", "short_name": "Tableau",
    "description": "Site audience and App Store figures — private network.",
    "start_url": "/", "display": "standalone",
    "background_color": "#131310", "theme_color": "#131310", "orientation": "portrait",
    "icons": [
        {"src": "/icone-180.png", "sizes": "180x180", "type": "image/png"},
        {"src": "/icone-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}


def icones_neutres() -> dict[str, bytes]:
    """The two starting PNGs, "AB" and "cd". Drawn here, never copied from the repo."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.exit("⛔ Pillow is needed to draw the neutral icons: pip3 install pillow")
    police = next((e for e in ("/System/Library/Fonts/Supplemental/Arial Black.ttf",
                               "/System/Library/Fonts/Helvetica.ttc",
                               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
                   if Path(e).exists()), None)
    sorties = {}
    for taille in (180, 512):
        k = taille / 64
        im = Image.new("RGB", (taille, taille), (0, 0, 0))
        d = ImageDraw.Draw(im)
        f = ImageFont.truetype(police, int(30 * k)) if police else ImageFont.load_default()
        for texte, x, y in (("AB", 32, 30), ("cd", 48, 58)):
            b = d.textbbox((0, 0), texte, font=f)
            d.text(((x * k) - (b[2] - b[0]) / 2, (y * k) - b[3]), texte, font=f,
                   fill=(237, 235, 228))
        d.rounded_rectangle([5 * k, 38 * k, 23 * k, 45 * k], radius=2 * k, fill=(0xE8, 0x97, 0x2E))
        tampon = io.BytesIO()
        im.save(tampon, format="PNG", optimize=True)
        sorties[f"tableau-de-bord/icone-{taille}.png"] = tampon.getvalue()
    return sorties


def controler(fichiers: dict[str, str]) -> None:
    """Refuse to build if a personal marker has survived anywhere."""
    fautes = []
    for nom, contenu in fichiers.items():
        # The licence is the one place an author's name belongs: without a copyright holder
        # the text grants nothing, and nobody may legally use the code.
        if nom.endswith("LICENSE"):
            continue
        bas = contenu.lower()
        for mot in INTERDITS:
            if mot in bas:
                ligne = next((i for i, l in enumerate(contenu.splitlines(), 1)
                              if mot in l.lower()), 0)
                fautes.append(f"{nom}:{ligne} — {mot!r}")
    if fautes:
        print("⛔ build refused: the package is not neutral.", file=sys.stderr)
        for f in sorted(set(fautes)):
            print("   " + f, file=sys.stderr)
        sys.exit(1)


def main() -> int:
    manquants = [str(p) for p in TEXTES.values() if not p.exists()]
    if manquants:
        sys.exit("files not found: " + ", ".join(manquants))

    textes = {k: v.read_text(encoding="utf-8") for k, v in TEXTES.items()}
    # The static trim is swapped for its neutral version: the repo keeps ours.
    textes["tableau-de-bord/icone.svg"] = ICONE_SVG
    textes["tableau-de-bord/manifest.webmanifest"] = (
        json.dumps(MANIFESTE, ensure_ascii=False, indent=2) + "\n")
    paquet = {"textes": textes,
              "images": {k: base64.b64encode(v).decode()
                         for k, v in icones_neutres().items()}}
    controler(textes)

    corps = (RACINE / "scripts" / "modele-installeur.py").read_text(encoding="utf-8")
    # The package is injected as JSON: no escaping to invent, no delimiter that could
    # turn up inside the content.
    corps = corps.replace('PAQUET = {}  # REMPLACE_A_LA_FABRICATION',
                          "PAQUET = " + json.dumps(paquet, ensure_ascii=False))
    # And once more on the finished product: the installer template travels too. The licence
    # is cut out of the text first — exactly as JSON encoded it — so the copyright holder's
    # name does not look like a leak, while everything around it is still checked.
    licence = json.dumps(paquet["textes"]["tableau-de-bord/LICENSE"])[1:-1]
    controler({SORTIE.name: corps.replace(licence, "")})
    SORTIE.write_text(corps, encoding="utf-8")
    SORTIE.chmod(0o755)
    print(f"→ {SORTIE.name} · {SORTIE.stat().st_size // 1024} kB · "
          f"{len(paquet['textes'])} text files, {len(paquet['images'])} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
