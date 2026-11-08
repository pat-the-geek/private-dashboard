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
- "something that answers wrong" is a 4xx or 5xx on a page URL, counted apart from the
  scanners, which ask for anything: an address only robots still request is a leftover in
  an index, one a human reaches is a dead link on the site;
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
import urllib.parse
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
          "page_surveillee": s.get("page_surveillee"),
          # ⚠️ This list is exhaustive: a key the configuration carries but that is not
          # repeated here never reaches audience(). "imports" was silently dropped that way.
          "imports": s.get("imports")} for s in CONFIG.get("sites", [])]
_APPSTORE = CONFIG.get("appstore") or {}
APPS = {a["id"]: a["nom"] for a in _APPSTORE.get("apps", [])}
SKU = {a["sku"]: a["id"] for a in _APPSTORE.get("apps", []) if a.get("sku")}
JOURS = int(CONFIG.get("jours") or 30)   # depth shown; after CONFIG, which carries it
# ⚠️ The App Store side is NOT bound by the same window as the logs: the sales reports are
# kept on disk and go back to the first day on sale, where an nginx log is rotated. Cutting
# their series to thirty days threw away three months of history already in hand.
JOURS_APPSTORE = int(CONFIG.get("jours_appstore") or JOURS)
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
# Le référent est le champ cité juste avant l'agent : il dit D'OÙ une adresse a été
# demandée, c'est-à-dire où se trouve le lien mort quand la réponse est un 404.
REFERENT = re.compile(r'"([^"]*)"\s+"[^"]*"\s+"[^"]*"\s*$')


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


def canon(chemin: str) -> str:
    """The key under which two addresses serving the SAME page are counted together.

    ⚠️ Seen on 30/10/2026: "/coulisses" and "/coulisses.html" sat as two lines in the
    most-read pages, each with its own figure, for one page read nine times. Depending on
    the server, one spelling or the other may 404 — so we group under this key but display
    the spelling actually seen most often, never the key itself.
    """
    c = chemin.split("?")[0]
    if c.endswith("/index.html"):
        c = c[: -len("index.html")]
    elif c.endswith(".html"):
        c = c[:-5]
    return c[:-1] if len(c) > 1 and c.endswith("/") else c


# Fallback classifier, when no classification script is given: a robot is recognised by
# what it says about itself. Less reliable than analysing hosting ranges — a robot disguised
# as a browser gets through — but enough for a rough cut, and we say so.
# Les robots d'indexation, nommés une fois pour deux usages : compter leurs passages, et
# distinguer QUI demande une adresse qui répond mal. Un moteur qui réclame encore une page
# retirée est un reste d'index ; une sonde WordPress, elle, n'apprend rien à personne.
INDEXEURS = ("Googlebot", "bingbot", "Applebot", "YandexBot")

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
    top = collections.Counter()              # clé canonique → pages vues
    orthographe = collections.defaultdict(collections.Counter)   # clé → adresses réelles
    # Une page relais — celle qu'une app ouvre pour remettre quelque chose — n'est pas une
    # lecture : 450 ouvertures écrasaient les huit vraies pages du classement. À part.
    chemin_relais = (site.get("imports") or {}).get("chemin")
    relais = collections.Counter()
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
                brut = chemin.split("?")[0]
                cle = canon(brut)
                orthographe[cle][brut] += 1
                if chemin_relais and cle == canon(chemin_relais):
                    relais[cle] += 1
                else:
                    top[cle] += 1
                devices[appareil(info["agent"])] += 1
                provenances[info["provenance"] or "direct"] += 1
                if j == veille:
                    d = datetime.strptime(quand.split()[0], "%d/%b/%Y:%H:%M:%S")
                    heures[d.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZONE).hour] += 1

    # ── Exits to the App Store, humans AND total ───────────────────────────────────────
    # ⚠️ The total does not measure interest: robots follow redirects with enthusiasm, and in
    # practice they make up almost all of it. Show both, never the total alone.
    departs = collections.Counter(); departs_h = collections.Counter()
    # Par jour aussi : sans série, impossible de rapprocher un départ d'une lecture ou
    # d'une installation — les trois chiffres restaient dans trois cartes séparées.
    departs_jour = collections.Counter(); departs_jour_h = collections.Counter()
    # ── What visitors take away, broken down by one query parameter ───────────────────
    # A page that hands something over — a feed to import, a file to open — says more
    # about what people want than a page view does. The configuration names the path and
    # the parameter to group by; without it, the section simply does not appear.
    conf_i = site.get("imports") or {}
    chemin_i = conf_i.get("chemin")
    param_i = conf_i.get("parametre") or "langue"
    imports_val = collections.Counter()
    imports_jour = collections.Counter()
    imports_ips = set()
    # ⭐ Second breakdown: the query string says WHAT was taken, rarely which family it
    # belongs to. A lookup table the site already publishes can tell — the configuration
    # names the file and the fields, so nothing about any one site is written here.
    #   "groupes": {"nom": …, "table": …,
    #               "depuis":   [list, key field, value field],      url → group key
    #               "libelles": [list, key field, label field, emoji field?]}
    conf_g = conf_i.get("groupes") or {}
    groupe_de, libelle_de = {}, {}
    if conf_g.get("table"):
        try:
            tab = json.loads(vers_chemin(conf_g["table"]).read_text(encoding="utf-8"))
            liste, cle, val = conf_g.get("depuis") or []
            groupe_de = {x[cle]: x[val] for x in tab.get(liste, []) if x.get(cle) and x.get(val)}
            lib = conf_g.get("libelles") or []
            if len(lib) >= 3:
                for x in tab.get(lib[0], []):
                    prefixe = (x.get(lib[3]) + " ") if len(lib) > 3 and x.get(lib[3]) else ""
                    libelle_de[x[lib[1]]] = prefixe + str(x.get(lib[2]) or x[lib[1]])
        except Exception as exc:
            print(f"  lookup table unreadable ({exc})")
    imports_grp = collections.Counter()
    # ── Indexing robots: who comes by, and do they reach the page being watched ────────
    # `page_surveillee` in the configuration: a page you doubt is being indexed. Without it,
    # we only count the hits.
    surveillee = site.get("page_surveillee")
    moteurs = collections.defaultdict(lambda: collections.Counter())
    # ── Ce qui répond mal ─────────────────────────────────────────────────────────────
    # ⛔ La boucle des lecteurs ne garde que les 200 : aucun 404, 403 ou 410 n'atteignait
    # cette page. Six adresses retirées d'un de nos sites sont restées des semaines dans
    # l'index d'un moteur, et c'est en interrogeant le moteur qu'on l'a appris — le journal,
    # lui, les voyait. On les compte donc ici, en séparant QUI demande : une adresse que
    # seuls les robots réclament est un reste d'index, celle qu'un humain atteint est un
    # lien mort.
    err_codes = collections.Counter()
    err_adr = collections.Counter()
    err_hum = collections.Counter()
    err_mot = collections.Counter()
    err_de = collections.defaultdict(collections.Counter)
    # La collecte d'abord, le compte ensuite : savoir si une adresse mentait sur son nom
    # demande d'avoir vu TOUTES ses lignes.
    err_brut: list = []
    prétentions = collections.defaultdict(set)
    # ⚠️ L'orthographe EXACTE compte ici plus qu'ailleurs : cette adresse se recopie
    # telle quelle dans l'outil de suppression d'un moteur. Pas de clé canonique affichée.
    err_orth = collections.defaultdict(collections.Counter)
    err_scanners = 0
    # ⚠️ Le premier jour RÉELLEMENT couvert par le journal. Sans lui, un journal qui ne
    # remonte pas à 30 jours fait dessiner des jours vides comme s'ils avaient été calmes :
    # l'histogramme montre un silence qui n'a pas eu lieu, et les moyennes s'effondrent.
    # Ce passage-ci voit toutes les lignes, humains et robots : c'est lui qui sait.
    premier_vu = None
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
                premier_vu = j if premier_vu is None else min(premier_vu, j)
                # ⚠️ Un nom de robot se cherche dans le SEUL champ user-agent. Sur la
                # ligne entière, un référent trafiqué ou une adresse qui porte le nom d'un
                # moteur se faisait compter comme un passage.
                ag = AGENT.search(l)
                agent_bas = (ag.group(1) if ag else "").lower()
                a = ADRESSE.search(l.rstrip())
                ip = (a.group(1) if a else "").strip()
                inf = types.get(ip)
                t = (inf or {}).get("type")
                indexeur = next((x for x in INDEXEURS if x.lower() in agent_bas), None)
                # ⛔ Un nom de moteur dans l'agent est une PRÉTENTION, pas une preuve. Une
                # seule adresse (45.138.12.32, le 30/10/2026) frappait l'un de nos sites
                # avec Firefox, Chrome, Safari ET Googlebot, et fabriquait même son
                # X-Forwarded-For : ses sondes WordPress arrivaient en tête des adresses
                # « encore réclamées par un moteur ». On garde donc, pour chaque adresse,
                # l'ensemble de ce qu'elle prétend être : un indexeur authentique ne se
                # présente jamais aussi comme un navigateur. Vérification par cohérence,
                # pas par DNS inverse — c'est moins sûr, et cela ne coûte aucune requête.
                prétentions[ip].add(indexeur or "navigateur")
                if methode == "GET" and code[0] in "45" and est_une_page(chemin):
                    if t == "SCANNER":
                        # Un scanner demande n'importe quoi : le compter par adresse
                        # noierait les vraies erreurs. Un seul nombre, pour mémoire.
                        err_scanners += 1
                    else:
                        r = REFERENT.search(l.rstrip())
                        d = (r.group(1) if r else "").strip()
                        # Un référent qui pointe l'adresse demandée elle-même est un
                        # rechargement : il occupe une ligne et n'apprend rien.
                        if d.split("?")[0].endswith(chemin.split("?")[0]):
                            d = ""
                        # ⛔ Venue du tableau de bord lui-même (servi sur le tailnet, *.ts.net) :
                        # c'est son propriétaire qui clique sur une erreur pour la vérifier. La compter
                        # faisait grossir, à chaque vérification, l'erreur qu'il vérifiait
                        # (06/11/2026 : la moitié des « erreurs vues par un humain »).
                        if urllib.parse.urlsplit(d).hostname and \
                                urllib.parse.urlsplit(d).hostname.endswith(".ts.net"):
                            continue
                        err_brut.append((code, chemin.split("?")[0], ip, indexeur,
                                         t in HUMAIN, d if d and d != "-" else None))
                if chemin_i and chemin.split("?")[0] == chemin_i and code == "200":
                    if inf and inf["type"] in HUMAIN:
                        v = urllib.parse.parse_qs(
                            urllib.parse.urlparse(chemin).query).get(param_i) or ["?"]
                        imports_val[v[0][:12]] += 1
                        if groupe_de:
                            u = urllib.parse.parse_qs(
                                urllib.parse.urlparse(chemin).query).get("url")
                            imports_grp[groupe_de.get(u[0], "?") if u else "?"] += 1
                        imports_jour[j] += 1
                        imports_ips.add(ip)
                if chemin.startswith("/go/"):
                    cible = chemin.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                    departs[cible] += 1
                    departs_jour[j] += 1
                    if t in HUMAIN:
                        departs_h[cible] += 1
                        departs_jour_h[j] += 1
                if indexeur:
                    moteurs[indexeur][j] += 1
                    if surveillee and surveillee in chemin:
                        moteurs[indexeur]["page"] += 1

    # Une adresse qui se présente sous un seul nom, et ce nom est celui d'un moteur :
    # elle est crue. Dès qu'un navigateur figure dans ses prétentions, elle ne l'est plus.
    honnetes = {a for a, f in prétentions.items() if len(f) == 1 and "navigateur" not in f}
    for code_e, brut, adresse, indexeur, humain, referent in err_brut:
        k = (code_e, canon(brut))
        err_orth[k][brut] += 1
        err_codes[code_e] += 1
        err_adr[k] += 1
        if humain:
            err_hum[k] += 1
        elif indexeur and adresse in honnetes:
            err_mot[k] += 1
        if referent:
            err_de[k][referent] += 1

    compte = collections.Counter(v["type"] for v in types.values())
    # La série commence au premier jour couvert, pas au premier jour de la fenêtre. Un jour
    # creux À L'INTÉRIEUR de la période reste à zéro — c'est une vraie journée sans lecteur,
    # et elle doit se voir.
    fenetre = [(aujourdhui - timedelta(days=JOURS - 1 - i)).isoformat() for i in range(JOURS)]
    couverts = [j for j in fenetre if premier_vu is None or j >= premier_vu]
    serie = [{"jour": j, "pages": pages.get(j, 0), "visiteurs": len(visiteurs.get(j, ()))}
             for j in couverts]
    return {
        "nom": site["nom"],
        "serie": serie,
        "depuis": couverts[0] if couverts else None,
        # Vrai quand le journal ne remonte pas aussi loin que « jours » : la page le dit
        # plutôt que de laisser croire à une période complète.
        "tronque": len(couverts) < JOURS,
        "humains": sum(compte.get(k, 0) for k in HUMAIN),
        "robots": compte.get("robot", 0),
        "scanners": compte.get("SCANNER", 0),
        "indetermines": compte.get("indetermine", 0),
        # L'orthographe affichée est celle que le journal porte le plus souvent : un lien
        # construit sur la clé canonique pourrait tomber sur un 404, selon le serveur.
        "pages_top": [{"page": orthographe[p].most_common(1)[0][0], "vues": n}
                      for p, n in top.most_common(10)],
        "pages_relais": [{"page": orthographe[p].most_common(1)[0][0], "vues": n}
                         for p, n in relais.most_common()],
        "appareils": [{"nom": k, "n": v} for k, v in devices.most_common()],
        "provenances": [{"nom": k, "n": v} for k, v in provenances.most_common(8)],
        "veille": {"jour": veille, "heures": [heures.get(h, 0) for h in range(24)]},
        "departs": [{"cible": c, "total": n, "humains": departs_h.get(c, 0)}
                    for c, n in departs.most_common()],
        "departs_serie": [{"jour": j, "total": departs_jour.get(j, 0),
                           "humains": departs_jour_h.get(j, 0)} for j in couverts],
        "erreurs": ({"total": sum(err_codes.values()),
                     "humains": sum(err_hum.values()),
                     "scanners": err_scanners,
                     "moteurs": sum(err_mot.values()),
                     "codes": [{"code": c, "n": n} for c, n in err_codes.most_common()],
                     # ⚠️ Classées par ce qu'elles APPRENNENT, jamais par leur nombre : les
                     # dix plus fréquentes étaient dix sondes WordPress, et l'adresse que
                     # Bing réclamait encore se trouvait au-delà du vingtième rang. Ne
                     # paraissent donc que celles qu'un lecteur a atteintes ou qu'un moteur
                     # d'indexation demande encore ; le reste tient dans un seul nombre.
                     "adresses": [{"chemin": err_orth[k].most_common(1)[0][0],
                                   "code": k[0], "n": err_adr[k],
                                   "humains": err_hum.get(k, 0),
                                   "moteurs": err_mot.get(k, 0),
                                   "venant_de": (err_de[k].most_common(1)[0][0]
                                                 if err_de.get(k) else None)}
                                  for k in sorted(
                                      (k for k in err_adr
                                       if err_hum.get(k) or err_mot.get(k)),
                                      key=lambda k: (-err_hum.get(k, 0), -err_mot.get(k, 0),
                                                     -err_adr[k]))[:8]],
                     "autres": sum(n for k, n in err_adr.items()
                                   if not err_hum.get(k) and not err_mot.get(k))}
                    if err_codes else None),
        "imports": ({"nom": conf_i.get("nom") or "Imports",
                     "parametre": param_i,
                     "total": sum(imports_val.values()),
                     "personnes": len(imports_ips),
                     # A malformed link puts anything in that parameter — we saw numeric
                     # ids where a language code belonged, each seen exactly once. Something
                     # seen once is not a category: it joins "other", however short the list.
                     "valeurs": ([{"cle": k, "n": n}
                                  for k, n in imports_val.most_common(8) if n > 1]
                                 + ([{"cle": "other", "n": _reste}]
                                    if (_reste := sum(n for k, n in imports_val.items()
                                                      if n == 1 or k not in dict(
                                                          imports_val.most_common(8)))) else [])),
                     "serie": [{"jour": j, "n": imports_jour.get(j, 0)}
                               for j in sorted(imports_jour)],
                     "groupes_nom": conf_g.get("nom") or "",
                     # The head, then everything else in one line — so the column adds up to
                     # the total above. Without it, eleven themes out of thirty-eight showed
                     # 197 of 396 and the reader had no way to know what the gap was.
                     "groupes": ([{"cle": libelle_de.get(k, k), "n": n}
                                  for k, n in imports_grp.most_common(12) if k != "?"]
                                 + [{"cle": l, "n": n, "reste": True} for l, n in (
                                     ("other themes", sum(n for k, n in imports_grp.most_common()[12:]
                                                          if k != "?")),
                                     ("not in the table", imports_grp.get("?", 0)))
                                    if n])}
                    if imports_val else None),
        "moteurs": [{"nom": n,
                     "serie": [c.get(j, 0) for j in couverts],
                     "total": sum(v for k, v in c.items() if k != "page"),
                     "page_surveillee": surveillee,
                     "sur_la_page": c.get("page", 0)}
                    for n, c in sorted(moteurs.items())],
    }


# ── What the machine says about itself ────────────────────────────────────────────────
# A dashboard that only counts readers says nothing when the machine beneath it stops.
# On 08/10/2026 one page went three days without being rebuilt: the data was fresh, the
# rendered page was not, and nothing on this page said so. These four readings are meant
# to make that kind of silence visible — they answer "is it still running", not "how many".


def services(projet) -> list[dict]:
    """The containers of a Compose project: state, uptime, restarts.

    A restart counter that climbs is the symptom nobody watches: the service answers, so
    everything looks fine, while it dies and comes back several times an hour.
    """
    if not projet:
        return []
    ou = vers_chemin(projet)
    try:
        r = subprocess.run(["docker", "compose", "ps", "--format", "json"], cwd=ou,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"  containers: docker said no ({r.stderr.strip()[:80]})")
            return []
        # Compose writes either one JSON array, or one object per line depending on version.
        brut = r.stdout.strip()
        lignes = json.loads(brut) if brut.startswith("[") else [
            json.loads(l) for l in brut.splitlines() if l.strip()]
    except Exception as exc:
        print(f"  containers: unreadable ({exc})")
        return []

    out = []
    for d in lignes:
        nom = d.get("Name") or d.get("Service") or "?"
        redem = None
        try:
            i = subprocess.run(["docker", "inspect", "-f", "{{.RestartCount}}", nom],
                               capture_output=True, text=True, timeout=20)
            if i.returncode == 0:
                redem = int(i.stdout.strip() or 0)
        except Exception:
            pass
        out.append({"nom": d.get("Service") or nom,
                    "etat": (d.get("State") or "").lower(),
                    "sante": (d.get("Health") or "").lower() or None,
                    "depuis": d.get("Status") or "",
                    "redemarrages": redem})
    return sorted(out, key=lambda x: x["nom"])


def disques(chemins: list) -> list[dict]:
    """The space left, where it matters."""
    import shutil
    out = []
    for c in chemins:
        p = vers_chemin(c if isinstance(c, str) else c.get("chemin"))
        nom = c.get("nom") if isinstance(c, dict) else str(p)
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        out.append({"nom": nom, "total": u.total, "libre": u.free,
                    "part": round(100 * (u.total - u.free) / u.total, 1) if u.total else 0})
    return out


def fraicheur(veilles: list) -> list[dict]:
    """Files that MUST stay recent, and how long they have not been.

    ⭐ This is the reading that earns its place. A task failing in silence leaves no trace
    in a log nobody opens; it shows up here, because a file stops getting younger. We
    compare an age to a threshold, nothing more — and that is enough.
    """
    out = []
    for v in veilles:
        p = vers_chemin(v["fichier"])
        # A pattern means the most recent file of a directory: "notes/*.md".
        # ⚠️ The most recent by DATE, not by name. Sorting names assumes they carry a date,
        # which not every directory does: a folder of freely titled notes had 170 days on its
        # last name and a few hours on its newest file.
        if any(c in str(p) for c in "*?["):
            candidats = [f for f in glob.glob(str(p)) if Path(f).is_file()]
            existant = max(candidats, key=lambda f: Path(f).stat().st_mtime) if candidats else None
        else:
            existant = str(p) if p.exists() else None
        seuil = float(v.get("heures") or 24)
        if not existant:
            out.append({"nom": v["nom"], "age_h": None, "seuil_h": seuil, "frais": False})
            continue
        age = (time.time() - Path(existant).stat().st_mtime) / 3600
        out.append({"nom": v["nom"], "age_h": round(age, 1), "seuil_h": seuil,
                    "frais": age <= seuil})
    return out


def sondes(liste: list) -> list[dict]:
    """One GET on each address that must answer, and how long it takes."""
    import urllib.error
    import urllib.request
    out = []
    for s in liste:
        depart = time.time()
        code, ms = None, None
        try:
            req = urllib.request.Request(s["url"], method="GET",
                                         headers={"User-Agent": "dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=12) as rep:
                code = rep.status
                rep.read(2048)
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = None
        ms = round((time.time() - depart) * 1000)
        out.append({"nom": s["nom"], "url": s["url"], "code": code, "ms": ms,
                    "ok": code is not None and 200 <= code < 400})
    return out


# The engine runs inside a virtual machine on macOS and Windows; these are the names its
# process has had. A plain Linux host has dockerd itself, and no virtual machine.
MOTEURS = ("com.docker.sailor", "com.docker.virtualization", "dockerd")


def _depuis(secondes: float | None) -> float | None:
    return None if secondes is None else round((time.time() - secondes) / 3600, 1)


def moteur() -> dict | None:
    """How long the container engine and the machine have each been up.

    The engine can restart on its own — to install an update of its own application, for
    instance. Every container goes down and comes back, and nothing says so: the services
    answer again within seconds. It only shows up afterwards, as a memory figure that
    halved overnight or a hole in a graph, and by then the logs that would explain it are
    gone with the containers.

    Two uptimes settle it. An engine younger than the machine means the engine alone was
    restarted, and that is worth a line in the morning.
    """
    demarre = None
    for nom in MOTEURS:
        pids = subprocess.run(["pgrep", "-f", nom], capture_output=True, text=True).stdout.split()
        if not pids:
            continue
        quand = subprocess.run(["ps", "-o", "lstart=", "-p", pids[0]],
                               capture_output=True, text=True).stdout.strip()
        try:
            demarre = time.mktime(time.strptime(quand, "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            demarre = None
        break
    machine = None
    r = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True)
    m = re.search(r"sec\s*=\s*(\d+)", r.stdout)
    if m:
        machine = int(m.group(1))
    elif Path("/proc/uptime").exists():               # Linux
        machine = time.time() - float(Path("/proc/uptime").read_text().split()[0])
    if demarre is None and machine is None:
        return None
    d = {"moteur_h": _depuis(demarre), "machine_h": _depuis(machine)}
    # One hour of slack: at boot the engine starts a little after the machine, and that is
    # not a restart. Beyond a day it is history, not news.
    d["redemarre_seul"] = bool(
        d["moteur_h"] is not None and d["machine_h"] is not None
        and d["moteur_h"] < 24 and d["moteur_h"] + 1 < d["machine_h"])
    return d


def technique() -> dict | None:
    c = CONFIG.get("technique") or {}
    if not c:
        return None
    d = {"services": services(c.get("docker")),
         "disques": disques(c.get("disques") or []),
         "fraicheur": fraicheur(c.get("fraicheur") or []),
         "sondes": sondes(c.get("sondes") or []),
         "moteur": moteur()}
    return d if any(d.values()) else None


def appstore() -> dict:
    fichiers = sorted(glob.glob(str(RACINE / (_APPSTORE.get("rapports") or "logs/appstore")
                                     / "ventes-*.tsv"))) if APPS else []
    cumul = {i: collections.Counter() for i in APPS}
    par_jour = collections.defaultdict(lambda: collections.Counter())
    pays = collections.defaultdict(collections.Counter)
    appareils = collections.Counter()
    recettes = collections.Counter()
    premiers = {}
    # Le premier jour de vente de CHAQUE version, lu dans la colonne « Version » des
    # rapports : de quoi marquer les sorties sur la courbe sans rien demander à l'API.
    # C'est la question que le tableau de bord existe pour répondre — une sortie fait-elle
    # vendre ? — et elle vivait jusqu'ici dans un fichier Markdown que personne n'ouvre.
    sorties = {}
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
                # ⚠️ Toutes les lignes, pas seulement les premiers téléchargements : la
                # colonne « on sale from » de l'historique se lit sur la PREMIÈRE apparition
                # de la version, quelle qu'elle soit. En ne regardant que les installations
                # neuves, le repère tombait un jour après la date du tableau.
                v = (r.get("Version") or "").strip()
                if v:
                    k2 = (cle, v)
                    sorties[k2] = min(sorties.get(k2, jour), jour)
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

    jours = sorted(par_jour)[-JOURS_APPSTORE:]
    fenetre = set(jours)
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
        # Un repère par version mise en vente dans la fenêtre, pour lire la courbe
        # au-dessus : la bosse du 24 octobre est-elle la 1.3, ou un hasard ?
        "reperes": sorted(({"jour": j, "texte": APPS[i] + " " + v}
                           for (i, v), j in sorties.items() if j in fenetre),
                          key=lambda x: x["jour"]),
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


UA_MASTODON = {"User-Agent": "tableau-de-bord (+https://github.com)"}


def compte_par_api(f: dict) -> dict | None:
    """Resolve an account on its instance. One request, no token."""
    import urllib.request
    inst = (f.get("instance") or "").rstrip("/")
    acct = (f.get("compte") or "").lstrip("@")
    if not inst or not acct:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{inst}/api/v1/accounts/lookup?acct={acct}", headers=UA_MASTODON), timeout=20) as r:
            return json.load(r)
    except Exception as exc:
        print(f"  {f.get('compte')} : API indisponible ({exc})")
        return None


def profil_depuis(cpt: dict, acct: str) -> dict:
    """The public profile fields the dashboard shows, from a resolved account."""
    return {"nom": cpt.get("display_name"), "acct": "@" + cpt.get("acct", acct),
            "url": cpt.get("url"), "posts": cpt.get("statuses_count"),
            "abonnes": cpt.get("followers_count")}


def fil_par_api(f: dict) -> dict | None:
    """An account's latest posts, through its instance's public API.

    Two requests, no token: we resolve the account, then read its statuses. The favourite
    and boost counters are the ones the instance publishes.
    """
    import urllib.request
    cpt = compte_par_api(f)
    if not cpt:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{(f.get('instance') or '').rstrip('/')}/api/v1/accounts/{cpt['id']}"
                "/statuses?limit=40&exclude_reblogs=true", headers=UA_MASTODON), timeout=30) as r:
            st = json.load(r)
    except Exception as exc:
        print(f"  {f.get('compte')} : API indisponible ({exc})")
        return None
    return {"profil": profil_depuis(cpt, (f.get("compte") or "").lstrip("@")),
            "collecte": datetime.now(ZONE).isoformat(timespec="seconds"),
            "posts": [{"id": x["id"], "url": x["url"], "date": x["created_at"],
                       "html": x.get("content") or "", "medias": x.get("media_attachments") or [],
                       "favoris": x.get("favourites_count", 0),
                       "partages": x.get("reblogs_count", 0),
                       "reponses": x.get("replies_count", 0)} for x in st]}


def un_fil(f: dict) -> dict | None:
    if f.get("fichier") and f["fichier"].exists():
        d = json.loads(f["fichier"].read_text(encoding="utf-8"))
        # ⚠️ A local collector does not necessarily write the profile: one of ours mirrors
        # only the posts. Without this, the display name, the follower count and the total
        # published stayed empty for that account alone, while the other showed them — the
        # kind of gap that reads as a broken card. The public API fills it without a token,
        # and the posts still come from the file.
        if not d.get("profil"):
            cpt = compte_par_api(f)
            if cpt:
                d["profil"] = profil_depuis(cpt, (f.get("compte") or "").lstrip("@"))
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
    data = {"genere_le": maintenant.isoformat(timespec="seconds"), "jours": JOURS,
            "jours_appstore": JOURS_APPSTORE, "sites": [],
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
        if a.get("erreurs"):
            e = a["erreurs"]
            print(f"  {'':<22}{e['total']:>5} wrong answers · {e['humains']} to a human,"
                  f" {e['moteurs']} to an indexer, {e['autres']} to other robots"
                  f" · {' '.join(c['code'] + '×' + str(c['n']) for c in e['codes'])}")
    data["appstore"] = appstore()
    data["fils"] = les_fils()
    data["fiches"] = fiches()
    data["versions"] = versions()
    data["technique"] = technique()
    if data["technique"]:
        s = data["technique"]
        print(f"  System                {len(s['services'])} container(s) · "
              f"{sum(1 for x in s['fraicheur'] if not x['frais'])} stale · "
              f"{sum(1 for x in s['sondes'] if not x['ok'])} probe(s) down")
    n = sum(a["neufs"] for a in data["appstore"]["apps"])
    print(f"  App Store             {n:>5} first downloads over {data['appstore']['journees']} days")
    sortie = Path(args.sortie)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  → {sortie} ({sortie.stat().st_size // 1024} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
