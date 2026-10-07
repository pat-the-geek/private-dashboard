#!/usr/bin/env python3
"""Fetches the daily App Store Connect sales reports into logs/appstore/.

Saves downloading the export by hand from Sales and Trends. Needs an API key carrying the
Sales and Reports role.

⛔ The key never leaves the machine: the `.p8` is read to sign a one-hour token, and nothing
else. It is neither copied nor committed.

The environment file — `ASC_VENTES_ENV`, or `~/.appstore/ventes.env` by default — carries
`ASC_VENTES_KEY_ID`, `ASC_VENTES_ISSUER_ID` and `ASC_VENDOR`. ⚠️ The `.p8` must sit **next to
that file**, named `AuthKey_<KEY_ID>.p8`.

The JWT is signed with `cryptography` alone, without PyJWT: one dependency fewer on a
machine de production se paie rarement.

    python3 scripts/ventes-appstore.py [--jours 14]
"""
from __future__ import annotations

import argparse
import base64
import datetime
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEST = BASE / "logs" / "appstore"
CONF = Path(os.path.expanduser(
    os.environ.get("ASC_VENTES_ENV") or str(Path.home() / ".appstore" / "ventes.env")))
POINT = "https://api.appstoreconnect.apple.com/v1/salesReports"


def reglages() -> dict:
    valeurs = {}
    if CONF.exists():
        for ligne in CONF.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#") and "=" in ligne:
                c, v = ligne.split("=", 1)
                valeurs[c.strip()] = v.strip()
    for c in ("ASC_VENTES_KEY_ID", "ASC_VENTES_ISSUER_ID", "ASC_VENDOR"):
        valeurs[c] = os.environ.get(c) or valeurs.get(c, "")
        if not valeurs[c]:
            sys.exit(f"⛔ {c} absent — voir {CONF}")
    return valeurs


def _b64(octets: bytes) -> bytes:
    return base64.urlsafe_b64encode(octets).rstrip(b"=")


def jeton(kid: str, iss: str) -> str:
    """An ES256 JWT, signed by hand. JWS wants raw r||s, not OpenSSL's DER."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    # The .p8 is looked for NEXT TO the environment file: one path to configure, and the
    # key stays with what describes it.
    chemin = CONF.parent / f"AuthKey_{kid}.p8"
    if not chemin.exists():
        sys.exit(f"⛔ key not found: {chemin}")
    cle = serialization.load_pem_private_key(chemin.read_bytes(), password=None)
    entete = _b64(json.dumps({"alg": "ES256", "kid": kid, "typ": "JWT"}).encode())
    corps = _b64(json.dumps({"iss": iss, "iat": int(time.time()),
                             "exp": int(time.time()) + 900,
                             "aud": "appstoreconnect-v1"}).encode())
    message = entete + b"." + corps
    der = cle.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    brut = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return (message + b"." + _b64(brut)).decode()


def telecharger(jour: datetime.date, tok: str, vendeur: str) -> str | None:
    url = (f"{POINT}?filter[frequency]=DAILY&filter[reportSubType]=SUMMARY"
           f"&filter[reportType]=SALES&filter[vendorNumber]={vendeur}"
           f"&filter[reportDate]={jour}&filter[version]=1_1")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok,
                                               "Accept": "application/a-gzip",
                                               "User-Agent": "tableau-de-bord/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as rep:
            return gzip.decompress(rep.read()).decode("utf-8")
    except urllib.error.HTTPError as e:
        # 404: Apple has not published that day yet, or there was no sale at all.
        if e.code == 404:
            return None
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jours", type=int, default=14, help="depth in days (default 14)")
    ap.add_argument("--refaire", action="store_true", help="re-download even if the file exists")
    args = ap.parse_args()

    r = reglages()
    tok = jeton(r["ASC_VENTES_KEY_ID"], r["ASC_VENTES_ISSUER_ID"])
    DEST.mkdir(parents=True, exist_ok=True)
    hier = datetime.date.today() - datetime.timedelta(days=1)

    neufs = vides = gardes = 0
    for i in range(args.jours):
        jour = hier - datetime.timedelta(days=i)
        cible = DEST / f"ventes-{jour}.tsv"
        if cible.exists() and not args.refaire:
            gardes += 1
            continue
        texte = telecharger(jour, tok, r["ASC_VENDOR"])
        if texte is None:
            vides += 1
            continue
        cible.write_text(texte, encoding="utf-8")
        neufs += 1
    print(f"  {neufs} day(s) fetched · {gardes} already there · {vides} with no report "
          f"(not published yet, or no sale)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
