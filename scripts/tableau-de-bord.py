#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the private dashboard's data: site audience + App Store figures.

⛔ WHAT COMES OUT OF HERE IS NOT PUBLIC. The file produced lives in `tableau-de-bord/`, served
on 127.0.0.1 and exposed to the tailnet alone by `tailscale serve`. It must never land in a
public directory, nor be committed: it is rebuilt every morning.

The definitions below matter more than the figures themselves — two sources that contradict
each other are worth nothing:
- a visitor's real address is the LAST quoted field of the nginx log (X-Forwarded-For, set by
  the proxy), never the first, which is the proxy's own;
- "a page viewed by a human" is a 200 response on a page URL — ending in "/", in ".html", or
  with no extension — outside /api/, /data/ and /go/, whose address the classifier calls
  human;
- the day is LOCAL, not UTC: it follows the configuration's time zone ("fuseau"), or the
  machine's. Timestamps are converted into that zone rather than offset by a fixed amount,
  otherwise the week the clocks change is wrong;
- on the App Store side, rows are grouped by **Apple Identifier** and never by title: an app
  renamed along the way shows up under two titles and would be counted twice. Only types
  1/1F/1T/F1 are first downloads; IA1 is a purchase; revenue is summed per currency only.

    python3 scripts/tableau-de-bord.py [--sortie tableau-de-bord/donnees.json]
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RACINE = Path(__file__).resolve().parent.parent

def lire_config() -> dict:
    """The configuration, so this dashboard serves someone else without being rewritten.

    `tableau-de-bord/config.json` if it exists, otherwise `config.exemple.json`. Every section
    is optional: a missing one removes its part of the page, it breaks nothing.
    """
    for nom in ("config.json", "config.exemple.json"):
        f = RACINE / "tableau-de-bord" / nom
        if f.exists():
            c = json.loads(f.read_text(encoding="utf-8"))
            c["_fichier"] = nom
            return c
    return {}


def vers_chemin(x) -> Path:
    """⚠️ Not named `chemin`: that name is already a loop variable for the requested URL,
    and the function was being shadowed everywhere it was called."""
    return Path(os.path.expanduser(str(x)))


CONFIG = lire_config()


def _fuseau_machine() -> ZoneInfo:
    """The machine's time zone, by its NAME — not by its offset of the moment.

    ⚠️ `datetime.now().astimezone().tzinfo` would give a frozen offset: the week the clocks
    change would be wrong. So we read the /etc/localtime link, and failing that we say so
    and fall back to UTC rather than being wrong in silence.
    """
    lien = Path("/etc/localtime")
    if lien.is_symlink() and "zoneinfo/" in (cible := os.readlink(lien)):
        try:
            return ZoneInfo(cible.split("zoneinfo/")[-1])
        except Exception:
            pass
    print("  time zone undetermined, days cut in UTC — set \"fuseau\"")
    return ZoneInfo("UTC")


# The day is cut in THIS zone: midnight to midnight, daylight saving included.
ZONE = ZoneInfo(CONFIG["fuseau"]) if CONFIG.get("fuseau") else _fuseau_machine()
SITES = [{"nom": s["nom"],
          "journaux": s.get("journaux"),
          "classement": s.get("classement"),
          "page_surveillee": s.get("page_surveillee")} for s in CONFIG.get("sites", [])]
_APPSTORE = CONFIG.get("appstore") or {}
APPS = {a["id"]: a["nom"] for a in _APPSTORE.get("apps", [])}
SKU = {a["sku"]: a["id"] for a in _APPSTORE.get("apps", []) if a.get("sku")}
JOURS = int(CONFIG.get("jours") or 30)   # depth shown; after CONFIG, which carries it
# ⚠️ An in-app purchase row carries ITS OWN Apple Identifier; the app is read from
# "Parent Identifier", which holds the SKU and not the numeric identifier. Without the SKU
# table from the configuration, purchases and revenue silently drop to zero.
NEUFS = {"1", "1F", "1T", "F1"}
REPRIS = {"3", "3F", "F3"}
MAJ = {"7", "7F", "F7"}
ACHATS = {"IA1", "IA9", "IA1-M", "IAY"}

# One nginx log line: … [timestamp] "request" code … "referer" "agent" "real address"
LIGNE = re.compile(r'\[([^\]]+)\]\s+"(\w+)\s+(\S+)[^"]*"\s+(\d{3})')
ADRESSE = re.compile(r'"([^"]*)"\s*$')
AGENT = re.compile(r'"([^"]*)"\s+"[^"]*"\s*$')


def jour_local(brut: str) -> str | None:
    """“05/Oct/2026:22:14:03 +0000” → the matching date in the chosen time zone."""
    try:
        d = datetime.strptime(brut.split()[0], "%d/%b/%Y:%H:%M:%S")
    except ValueError:
        return None
    return d.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZONE).date().isoformat()


def est_une_page(chemin: str) -> bool:
    c = chemin.split("?")[0]
    if c.startswith(("/api/", "/data/", "/go/")):
        return False
    dernier = c.rsplit("/", 1)[-1]
    return c.endswith("/") or c.endswith(".html") or "." not in dernier


# Fallback classifier, when no classification script is given: a robot is recognised by
# what it says about itself. Less reliable than analysing hosting ranges — a robot disguised
# as a browser gets through — but enough for a rough cut, and we say so.
ROBOT = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|preview|monitor|curl|wget|"
                   r"python-requests|headless|scrapy|feedfetcher|validator", re.I)


def classer_simplement(journaux: str) -> dict[str, dict]:
    vus: dict[str, dict] = {}
    for f in sorted(glob.glob(os.path.expanduser(journaux))):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for l in fh:
                a = ADRESSE.search(l.rstrip())
                ip = (a.group(1) if a else "").strip()
                if not ip:
                    continue
                ag = AGENT.search(l)
                agent = ag.group(1) if ag else ""
                t = "robot" if (not agent or ROBOT.search(agent)) else "human"
                v = vus.setdefault(ip, {"type": t, "provenance": "direct", "agent": agent})
                if t == "robot":
                    v["type"] = "robot"          # one robot hit is enough to classify
    return vus


# A classifier writes the value it likes; « humain » is accepted beside « human » so a
# script written before this was in English keeps working.
HUMAIN = ("human", "humain")


def classer(script: Path) -> dict[str, dict]:
    """Each address's type and source, from the classifier named in the configuration.

    The script is called with one argument — the CSV to write — and run from its project
    root, so a script that resolves paths relative to that root keeps working.

    ⚠️ The output goes to a temporary file: a CSV kept in the project and its archives must
    not move. Some classifiers also drop an archive in /tmp/history when the output lands
    outside their project; we remove only what THIS run created there, never a pre-existing
    file that is none of our business.
    """
    debut = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "access.csv"
        r = subprocess.run([str(script), str(dest)], cwd=script.parent.parent,
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not dest.exists():
            raise SystemExit(f"classification failed for {script}: {r.stderr[-300:]}")
        out = {}
        with dest.open(encoding="utf-8") as f:
            for ligne in csv.DictReader(f):
                out[ligne["ip"]] = {"type": ligne["type"], "provenance": ligne["provenance"],
                                    "agent": ligne.get("user_agent", "")}
        for reste in (Path(tmp) / "history", Path("/tmp/history")):
            if reste.is_dir():
                for f in reste.glob("*"):
                    if f.is_file() and f.stat().st_mtime >= debut:
                        f.unlink(missing_ok=True)
                if not any(reste.iterdir()):
                    reste.rmdir()
    return out


def appareil(agent: str) -> str:
    a = agent or ""
    if "iPhone" in a: return "iPhone"
    if "iPad" in a: return "iPad"
    if "Android" in a: return "Android"
    if "Macintosh" in a or "Mac OS X" in a: return "Mac"
    if "Windows" in a: return "Windows"
    if "Linux" in a or "X11" in a: return "Linux"
    return "autre"


def audience(site: dict) -> dict:
    journaux = site.get("journaux")
    script = site.get("classement")
    if script and vers_chemin(script).exists():
        types = classer(vers_chemin(script))
    else:
        types = classer_simplement(journaux)
    aujourdhui = datetime.now(ZONE).date()
    debut = (aujourdhui - timedelta(days=JOURS - 1)).isoformat()

    pages = collections.Counter()            # jour → pages vues par des humains
    visiteurs = collections.defaultdict(set)  # jour → adresses humaines distinctes
    top = collections.Counter()
    devices = collections.Counter()
    provenances = collections.Counter()
    heures = collections.Counter()           # local hour of the last full day
    veille = (aujourdhui - timedelta(days=1)).isoformat()

    for journal in sorted(glob.glob(os.path.expanduser(journaux))):
        with open(journal, encoding="utf-8", errors="replace") as f:
            for l in f:
                m = LIGNE.search(l)
                if not m:
                    continue
                quand, methode, chemin, code = m.groups()
                if methode != "GET" or code != "200" or not est_une_page(chemin):
                    continue
                a = ADRESSE.search(l.rstrip())
                ip = (a.group(1) if a else "").strip()
                info = types.get(ip)
                if not info or info["type"] not in HUMAIN:
                    continue
                j = jour_local(quand)
                if not j or j < debut:
                    continue
                pages[j] += 1
                visiteurs[j].add(ip)
                top[chemin.split("?")[0]] += 1
                devices[appareil(info["agent"])] += 1
                provenances[info["provenance"] or "direct"] += 1
                if j == veille:
                    d = datetime.strptime(quand.split()[0], "%d/%b/%Y:%H:%M:%S")
                    heures[d.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZONE).hour] += 1

    # ── Exits to the App Store, humans AND total ───────────────────────────────────────
    # ⚠️ The total does not measure interest: robots follow redirects with enthusiasm, and in
    # practice they make up almost all of it. Show both, never the total alone.
    departs = collections.Counter(); departs_h = collections.Counter()
    # ── Indexing robots: who comes by, and do they reach the page being watched ────────
    # `page_surveillee` in the configuration: a page you doubt is being indexed. Without it,
    # we only count the hits.
    surveillee = site.get("page_surveillee")
    moteurs = collections.defaultdict(lambda: collections.Counter())
    for journal in sorted(glob.glob(os.path.expanduser(journaux))):
        with open(journal, encoding="utf-8", errors="replace") as f:
            for l in f:
                m = LIGNE.search(l)
                if not m:
                    continue
                quand, methode, chemin, code = m.groups()
                j = jour_local(quand)
                if not j or j < debut:
                    continue
                if chemin.startswith("/go/"):
                    cible = chemin.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                    departs[cible] += 1
                    a = ADRESSE.search(l.rstrip())
                    info = types.get((a.group(1) if a else "").strip())
                    if info and info["type"] in HUMAIN:
                        departs_h[cible] += 1
                for nom, motif in (("Googlebot", "Googlebot"), ("bingbot", "bingbot"),
                                   ("Applebot", "Applebot"), ("YandexBot", "YandexBot")):
                    if motif.lower() in l.lower():
                        moteurs[nom][j] += 1
                        if surveillee and surveillee in chemin:
                            moteurs[nom]["page"] += 1
                        break

    compte = collections.Counter(v["type"] for v in types.values())
    serie = []
    for i in range(JOURS):
        j = (aujourdhui - timedelta(days=JOURS - 1 - i)).isoformat()
        serie.append({"jour": j, "pages": pages.get(j, 0), "visiteurs": len(visiteurs.get(j, ()))})
    return {
        "nom": site["nom"],
        "serie": serie,
        "humains": sum(compte.get(k, 0) for k in HUMAIN),
        "robots": compte.get("robot", 0),
        "scanners": compte.get("SCANNER", 0),
        "indetermines": compte.get("indetermine", 0),
        "pages_top": [{"page": p, "vues": n} for p, n in top.most_common(10)],
        "appareils": [{"nom": k, "n": v} for k, v in devices.most_common()],
        "provenances": [{"nom": k, "n": v} for k, v in provenances.most_common(8)],
        "veille": {"jour": veille, "heures": [heures.get(h, 0) for h in range(24)]},
        "departs": [{"cible": c, "total": n, "humains": departs_h.get(c, 0)}
                    for c, n in departs.most_common()],
        "moteurs": [{"nom": n,
                     "serie": [c.get((aujourdhui - timedelta(days=JOURS - 1 - i)).isoformat(), 0)
                               for i in range(JOURS)],
                     "total": sum(v for k, v in c.items() if k != "page"),
                     "page_surveillee": surveillee,
                     "sur_la_page": c.get("page", 0)}
                    for n, c in sorted(moteurs.items())],
    }


def appstore() -> dict:
    fichiers = sorted(glob.glob(str(RACINE / (_APPSTORE.get("rapports") or "logs/appstore")
                                     / "ventes-*.tsv"))) if APPS else []
    cumul = {i: collections.Counter() for i in APPS}
    par_jour = collections.defaultdict(lambda: collections.Counter())
    pays = collections.defaultdict(collections.Counter)
    appareils = collections.Counter()
    recettes = collections.Counter()
    premiers = {}
    dernier_achat = None            # outside the window: a purchase may be older
    for f in fichiers:
        jour = os.path.basename(f)[7:17]
        with open(f, encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                ident = (r.get("Apple Identifier") or "").strip()
                parent = (r.get("Parent Identifier") or "").strip()
                pt = (r.get("Product Type Identifier") or "").strip()
                u = int(r.get("Units") or 0)
                cle = ident if ident in APPS else SKU.get(parent) or SKU.get(parent.rsplit(".", 1)[0])
                if not cle:
                    continue
                if pt in NEUFS:
                    quoi = "neufs"
                    pays[cle][(r.get("Country Code") or "").strip()] += u
                    appareils[(r.get("Device") or "").strip()] += u
                    premiers.setdefault(cle, jour)
                elif pt in REPRIS: quoi = "repris"
                elif pt in MAJ: quoi = "maj"
                elif pt in ACHATS:
                    quoi = "achats"
                    dernier_achat = max(dernier_achat or jour, jour)
                    # ⚠️ Never summed across currencies: the column varies by country.
                    recettes[(r.get("Currency of Proceeds") or "").strip()] += \
                        float(r.get("Developer Proceeds") or 0) * u
                else:
                    continue
                cumul[cle][quoi] += u
                par_jour[jour][cle + ":" + quoi] += u

    jours = sorted(par_jour)[-JOURS:]
    return {
        "dernier_achat": dernier_achat,
        "depuis": os.path.basename(fichiers[0])[7:17] if fichiers else None,
        "jusqu_a": os.path.basename(fichiers[-1])[7:17] if fichiers else None,
        "journees": len(fichiers),
        "apps": [{
            "id": i, "nom": APPS[i], "premier_jour": premiers.get(i),
            "neufs": cumul[i]["neufs"], "repris": cumul[i]["repris"],
            "maj": cumul[i]["maj"], "achats": cumul[i]["achats"],
            "pays": [{"code": k, "n": v} for k, v in pays[i].most_common(6)],
        } for i in APPS],
        "recettes": [{"monnaie": k, "montant": round(v, 2)} for k, v in recettes.most_common() if k],
        "appareils": [{"nom": k, "n": v} for k, v in appareils.most_common()],
        "serie": [{"jour": j,
                   **{APPS[i]: par_jour[j].get(i + ":neufs", 0) for i in APPS},
                   "maj": sum(par_jour[j].get(i + ":maj", 0) for i in APPS),
                   "achats": sum(par_jour[j].get(i + ":achats", 0) for i in APPS)}
                  for j in jours],
    }


_JETON: list = []  # cache : un seul JWT — et un seul message — pour les deux appels


def entetes_asc() -> dict | None:
    """The App Store Connect token, from the key named in the configuration.

    ⛔ The .p8 never leaves the machine: we read it, sign, and send a one-hour token. With no
    "appstore" section, we return None and the sections disappear.
    """
    if _JETON:
        return _JETON[0]
    env = _APPSTORE.get("env")
    if not env or not APPS:
        return None
    if not vers_chemin(env).exists():
        print(f"  App Store Connect: {env} missing, sections omitted")
        _JETON.append(None)
        return None
    try:
        import importlib.util
        # ventes-appstore.py fixes its key path AT IMPORT, from ASC_VENTES_ENV: we set it
        # before exec_module, otherwise it would fall back to its default and ignore the config.
        os.environ["ASC_VENTES_ENV"] = str(vers_chemin(env))
        sp = importlib.util.spec_from_file_location("ve", RACINE / "scripts" / "ventes-appstore.py")
        ve = importlib.util.module_from_spec(sp)
        argv, sys.argv = sys.argv, ["tableau-de-bord"]
        sp.loader.exec_module(ve)
        sys.argv = argv
        r = ve.reglages()
        _JETON.append({"Authorization":
                       f"Bearer {ve.jeton(r['ASC_VENTES_KEY_ID'], r['ASC_VENTES_ISSUER_ID'])}"})
        return _JETON[0]
    except (Exception, SystemExit) as exc:
        # ventes-appstore.py reports an incomplete key with sys.exit: SystemExit is not an
        # Exception, and without catching it the whole dashboard would fall with it.
        print(f"  App Store Connect: key unavailable ({exc})")
        _JETON.append(None)
        return None


def versions() -> list[dict]:
    """Version history: what App Store Connect declares, dated by actual sales.

    ⚠️ `createdDate` is NOT the release date: it is the day the version record was created,
    often a few days before going on sale. The date that matters is read from the sales
    reports, Version column: the first day a version appears is the day people started
    installing it. We show both, each named for what it
    qu'elles sont.
    """
    # What the sales know: first and last appearance of each version, and its volume
    vu = collections.defaultdict(lambda: {"debut": None, "fin": None, "n": 0})
    for f in sorted(glob.glob(str(RACINE / (_APPSTORE.get("rapports") or "logs/appstore")
                                 / "ventes-*.tsv"))):
        jour = os.path.basename(f)[7:17]
        with open(f, encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                ident = (r.get("Apple Identifier") or "").strip()
                if ident not in APPS:
                    continue
                v = (r.get("Version") or "").strip()
                if not v:
                    continue
                k = (ident, v)
                u = int(r.get("Units") or 0)
                vu[k]["debut"] = min(vu[k]["debut"] or jour, jour)
                vu[k]["fin"] = max(vu[k]["fin"] or jour, jour)
                vu[k]["n"] += u

    out: list[dict] = []
    try:
        import requests
        H = entetes_asc()
        if not H:
            return out                 # no key configured: the section disappears
        B = "https://api.appstoreconnect.apple.com/v1"
        for ident, nom in APPS.items():
            x = requests.get(f"{B}/apps/{ident}/appStoreVersions", headers=H, timeout=30, params={
                "limit": 50, "fields[appStoreVersions]":
                "versionString,appStoreState,platform,createdDate"})
            if x.status_code != 200:
                continue
            # ⚠️ A version is grouped by NUMBER, not by platform: the sales reports ignore
            # the platform, and showing the same installs against "1.2 iOS" and again against
            # "1.2 Mac" would count them twice to the eye.
            par_num = {}
            for d in x.json().get("data", []):
                a = d["attributes"]
                num = a["versionString"]
                e = par_num.setdefault(num, {
                    "version": num, "plateformes": [], "etats": set(),
                    "creee": (a.get("createdDate") or "")[:10],
                    "premiere_vente": vu.get((ident, num), {}).get("debut"),
                    "derniere_vente": vu.get((ident, num), {}).get("fin"),
                    "installations": vu.get((ident, num), {}).get("n", 0),
                })
                e["plateformes"].append(a["platform"].replace("_OS", "").replace("MAC", "Mac"))
                e["etats"].add(a["appStoreState"])
                e["creee"] = min(e["creee"], (a.get("createdDate") or "")[:10])
            lignes = []
            for e in par_num.values():
                e["plateformes"] = sorted(set(e["plateformes"]))
                e["etat"] = "READY_FOR_SALE" if e["etats"] == {"READY_FOR_SALE"} \
                            else " · ".join(sorted(e["etats"] - {"READY_FOR_SALE"}))
                del e["etats"]
                lignes.append(e)
            lignes.sort(key=lambda l: (l["creee"], l["version"]), reverse=True)
            out.append({"app": nom, "versions": lignes})
    except Exception as exc:
        print(f"  versions : indisponibles ({exc})")
    return out


# Mastodon accounts come from the configuration. Two ways of reading them:
# — "fichier": a JSON already mirrored by a local collector;
# — otherwise the instance's PUBLIC API, which needs no token. That path is what makes the
#   dashboard usable by someone with no tooling of their own.
FILS = [{"compte": m.get("compte"), "instance": m.get("instance"),
         "url": m.get("url") or ((m.get("instance") or "").rstrip("/") + "/" + (m.get("compte") or "")),
         "fichier": vers_chemin(m["fichier"]) if m.get("fichier") else None}
        for m in CONFIG.get("mastodon", [])]


def fil_par_api(f: dict) -> dict | None:
    """An account's latest posts, through its instance's public API.

    Two requests, no token: we resolve the account, then read its statuses. The favourite
    and boost counters are the ones the instance publishes.
    """
    import urllib.request
    ua = {"User-Agent": "tableau-de-bord (+https://github.com)"}
    inst = (f.get("instance") or "").rstrip("/")
    acct = (f.get("compte") or "").lstrip("@")
    if not inst or not acct:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{inst}/api/v1/accounts/lookup?acct={acct}", headers=ua), timeout=20) as r:
            cpt = json.load(r)
        with urllib.request.urlopen(urllib.request.Request(
                f"{inst}/api/v1/accounts/{cpt['id']}/statuses?limit=40&exclude_reblogs=true",
                headers=ua), timeout=30) as r:
            st = json.load(r)
    except Exception as exc:
        print(f"  {f.get('compte')} : API indisponible ({exc})")
        return None
    return {"profil": {"nom": cpt.get("display_name"), "acct": "@" + cpt.get("acct", acct),
                       "url": cpt.get("url"), "posts": cpt.get("statuses_count"),
                       "abonnes": cpt.get("followers_count")},
            "collecte": datetime.now(ZONE).isoformat(timespec="seconds"),
            "posts": [{"id": x["id"], "url": x["url"], "date": x["created_at"],
                       "html": x.get("content") or "", "medias": x.get("media_attachments") or [],
                       "favoris": x.get("favourites_count", 0),
                       "partages": x.get("reblogs_count", 0),
                       "reponses": x.get("replies_count", 0)} for x in st]}


def un_fil(f: dict) -> dict | None:
    if f.get("fichier") and f["fichier"].exists():
        d = json.loads(f["fichier"].read_text(encoding="utf-8"))
    else:
        d = fil_par_api(f)
    if not d:
        return None
    posts = d.get("posts") or []
    profil = d.get("profil") or {}
    recents = sorted(posts, key=lambda p: p["date"], reverse=True)[:4]

    def texte(p):
        # Un collecteur peut fournir un champ « texte » ; l'API publique, elle, n'a que le HTML.
        t = p.get("texte")
        if not t:
            t = re.sub(r"<[^>]+>", " ", p.get("html") or "")
            t = __import__("html").unescape(re.sub(r"\s+", " ", t)).strip()
        return t[:110]

    return {
        "compte": profil.get("acct") or f["compte"],
        "nom": profil.get("nom"),
        "url": profil.get("url") or f["url"],
        "maj": d.get("maj") or d.get("collecte"),
        "messages": len(posts),
        "total_publies": profil.get("posts"),
        "abonnes": profil.get("abonnes"),
        "images": sum(len(p.get("medias") or []) for p in posts),
        # ⚠️ "reponses" is sometimes a LIST (the replies mirrored by a collector), sometimes
        # a NUMBER (the counter Mastodon returns). Both say the same thing; we only have to
        # avoid calling len() on an integer.
        "reponses": sum((r if isinstance(r := p.get("reponses") or 0, int) else len(r))
                        for p in posts),
        "favoris": sum(p.get("favoris", 0) for p in posts),
        "partages": sum(p.get("partages", 0) for p in posts),
        "derniers": [{"date": p["date"], "texte": texte(p), "favoris": p.get("favoris", 0),
                      "partages": p.get("partages", 0)} for p in recents],
    }


def les_fils() -> list[dict]:
    return [x for x in (un_fil(f) for f in FILS) if x]


def fiches() -> dict | None:
    """The state of the App Store listings and the beta, through the sales key.

    ⚠️ That key reads versions and beta groups, but NOT builds (403): it is a reporting key,
    not a management one. Build numbers are therefore not here.
    """
    try:
        import requests
        H = entetes_asc()
        if not H:
            return None                # no key configured: the section disappears
        B = "https://api.appstoreconnect.apple.com/v1"
        out = {"apps": [], "beta": []}
        for ident, nom in APPS.items():
            v = requests.get(f"{B}/apps/{ident}/appStoreVersions", headers=H, timeout=30, params={
                "limit": 50, "fields[appStoreVersions]": "versionString,appStoreState,platform"})
            if v.status_code != 200:
                continue
            par = {}
            for d in v.json().get("data", []):
                a = d["attributes"]
                p = par.setdefault(a["platform"], {"en_vente": None, "en_cours": None})
                if a["appStoreState"] == "READY_FOR_SALE" and not p["en_vente"]:
                    p["en_vente"] = a["versionString"]
                elif a["appStoreState"] != "READY_FOR_SALE" and not p["en_cours"]:
                    p["en_cours"] = f"{a['versionString']} · {a['appStoreState']}"
            out["apps"].append({"nom": nom, "plateformes": [
                {"nom": k, **vv} for k, vv in sorted(par.items())]})
            g = requests.get(f"{B}/apps/{ident}/betaGroups", headers=H, timeout=30, params={"limit": 10})
            for x in (g.json().get("data", []) if g.status_code == 200 else []):
                at = x["attributes"]
                if at.get("isInternalGroup"):
                    continue
                t = requests.get(f"{B}/betaGroups/{x['id']}/betaTesters", headers=H, timeout=30,
                                 params={"limit": 1})
                n = t.json().get("meta", {}).get("paging", {}).get("total") if t.status_code == 200 else None
                out["beta"].append({"app": nom, "groupe": at.get("name"),
                                    "lien_public": bool(at.get("publicLinkEnabled")), "testeurs": n})
        return out
    except Exception as exc:
        print(f"  fiches App Store : indisponibles ({exc})")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", default=str(RACINE / "tableau-de-bord" / "donnees.json"))
    args = ap.parse_args()
    maintenant = datetime.now(ZONE)
    print(f"── dashboard · {maintenant:%Y-%m-%d %H:%M %Z}")
    data = {"genere_le": maintenant.isoformat(timespec="seconds"), "jours": JOURS, "sites": [],
            # The trim travels with the figures: the page has nothing hard-coded.
            "titre": CONFIG.get("titre") or "Dashboard",
            "marque": CONFIG.get("marque") or {"haut": "··", "bas": "··"},
            "accent": CONFIG.get("accent") or "#E8972E"}
    for s in SITES:
        if not s.get("journaux") or not glob.glob(os.path.expanduser(s["journaux"])):
            print(f"  {s['nom']}: no log found, skipped")
            continue
        a = audience(s)
        data["sites"].append(a)
        print(f"  {a['nom']:<22}{a['humains']:>5} humans · {sum(x['pages'] for x in a['serie']):>5} pages over {JOURS} d")
    data["appstore"] = appstore()
    data["fils"] = les_fils()
    data["fiches"] = fiches()
    data["versions"] = versions()
    n = sum(a["neufs"] for a in data["appstore"]["apps"])
    print(f"  App Store             {n:>5} first downloads over {data['appstore']['journees']} days")
    sortie = Path(args.sortie)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  → {sortie} ({sortie.stat().st_size // 1024} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
