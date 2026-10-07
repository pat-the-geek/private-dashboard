# A private dashboard

Your sites' audience, your App Store figures, your apps' versions and your Mastodon accounts,
on one page made for a phone — **served on your own private network, never on the internet**.

Nothing is hard-coded: everything comes from `config.json`. Every section is optional, and a
missing one simply removes its part of the page.

> **In a hurry?** One file does all of it:
> ```bash
> python3 scripts/faire-installeur.py          # builds the installer
> python3 installeur-tableau-de-bord.py        # shows what it would do
> python3 installeur-tableau-de-bord.py --installer --vers ~/my-dashboard
> ```
> It writes the files, redraws the icon with your initials, produces the figures, starts the
> container, opens access to your private network and sets the morning task. Without
> `--installer` it only shows. `--sans-tache` leaves your crontab alone.
>
> ⛔ The installer is **not** committed: it is rebuilt from the sources, so there are never
> two copies of the page and the generator.

The rest of this file describes the same thing step by step, for anyone who wants to
understand it, or to install only part of it.

---

## What the page shows

One page, read from top to bottom. A section with nothing to say does not appear: a partial
configuration gives a shorter page, not a page full of holes.

### At a glance

The summary, in tiles. Each gives a figure, its seven-day trend and its distance from the
average — in green or red, so that a bad day is seen rather than read.

- **per site**: yesterday's human visitors, with the last seven days as a curve;
- **Installs**: first downloads on the last day Apple has published;
- **Last purchase**: how many days since you sold anything — past a week, the tile says so;
- **Revenue**: the total since the start, per currency;
- **Exits to the App Store**: clicks on your `/go/` links.

Below them, **alerts**: a version that is ready but never submitted, an indexing robot that
crawls the site without ever opening the page you are watching.

### Site audience

Per site: pages read, visitors over seven days, humans and robots since the start; human
visitors day by day as bars; the most-read pages; the devices. The site's name is a link —
it is usually the first thing you want to open.

### To the App Store

Clicks on your `/go/` links, **humans on one side, the total on the other**.

⚠️ Both figures are shown together on purpose. The raw total looks like interest when it is
made of robots following every redirect: read alone, it lies.

### App Store

Since the start: installs, purchases, revenue per currency, then a table per app (installs,
re-downloads, updates, purchases). Then installs day by day over the chosen depth, the last
seven days in plain figures, and the devices your apps are installed on.

### Products · Version history

The state of each App Store Connect listing: the version on sale, the one in preparation and
its status, the TestFlight beta groups and their number of testers. Then the version history,
on sale and upcoming, with their dates.

### Mastodon · Indexing

Per account: followers, posts published, latest posts. And, per site and per crawler, the
number of hits over the period — with, if you have named one, the watched page and how many
times the crawler went there.

### On the phone

The ↻ button at the top right re-reads the figures; the page also does it by itself when you
come back to it. Added to the home screen, it opens full screen, with no address bar.

---

## How the figures are counted

These rules matter more than the figures themselves: two sources that contradict each other
are worth nothing.

| | |
|---|---|
| **A visitor's address** | the **last** quoted field of the log line, never the first — behind a proxy, the first one is the proxy's |
| **A page view** | a 200 response on a page URL (`/`, `.html`, or no extension), outside `/api/`, `/data/` and `/go/`, from an address classed human |
| **A day** | midnight to midnight in the chosen time zone, not in UTC. The conversion follows daylight saving: a fixed offset would get the week the clocks change wrong |
| **Apps** | grouped by **Apple Identifier**, never by title — an app renamed along the way would be counted twice |
| **Installs** | first downloads only. Re-downloads and updates are counted separately |
| **In-app purchases** | attached to their app by the **SKU**, which the row carries in `Parent Identifier` — not by the identifier, which is the purchase's own |
| **Revenue** | summed **per currency**, never across currencies |
| **Versions** | all platforms together: Apple's reports do not tell them apart |

---

## 1. The configuration

```bash
cp tableau-de-bord/config.exemple.json tableau-de-bord/config.json
```

| Key | What it does |
|---|---|
| `titre`, `marque`, `accent` | the name shown, the two halves of the wordmark, the colour |
| `jours` | the depth of the charts (30 by default) |
| `fuseau` | **optional** — the time zone that cuts the days, in IANA form (`Europe/Paris`, `America/Montreal`). Absent, the machine's own |
| `sites[].journaux` | a file pattern, for example `~/sites/my-site/logs/access-*.log` |
| `sites[].classement` | **optional** — a script that writes a CSV `ip,type,provenance,user_agent` |
| `sites[].page_surveillee` | **optional** — a page you want to know whether indexing robots visit; the dashboard warns you when a crawler ignores it |
| `appstore.env` | a file carrying `ASC_VENTES_KEY_ID`, `ASC_VENTES_ISSUER_ID` and `ASC_VENDOR` |
| `appstore.rapports` | the directory holding Apple's daily reports (`ventes-YYYY-MM-DD.tsv`) |
| `appstore.apps[]` | `id` (Apple Identifier), `nom`, `sku` |
| `mastodon[]` | `compte` and `instance`; a local `fichier` if you already mirror one |

### The log format

The script expects the common nginx format, **with the real address as the last field**:

```
log_format … '$remote_addr - … [$time_local] "$request" $status … "$http_referer" "$http_user_agent" "$http_x_forwarded_for"';
```

⚠️ Behind a proxy (Cloudflare, a load balancer), `$remote_addr` is the proxy's. It is
`$http_x_forwarded_for`, last, that carries the visitor's address — and that is the one the
script reads. Without that field, all your visitors collapse into a single address.

### Humans or robots

With a `classement` script, the dashboard runs it and reads its verdict. The script is called
with one argument — the CSV to write — from its own project root, and must produce:

```
ip,type,provenance,user_agent
203.0.113.7,human,direct,Mozilla/5.0 …
198.51.100.9,robot,crawl,Googlebot/2.1 …
```

Only rows whose `type` is `human` count as visitors (`humain` is accepted as well, for
scripts written before this was in English).

Without such a script, a fallback classifier recognises a robot by what it says about itself
(`bot`, `crawl`, `spider`, `curl`…).

⚠️ **That fallback is coarse**: a robot disguised as a browser will pass for a reader, and so
will a rented server's address. For figures you can rely on, supply a real classifier.

### The App Store Connect key

A key with the **Sales** role is enough: it reads the sales reports, the versions and the beta
groups. It cannot read builds, nor create analytics reports.

⛔ The `.p8` never leaves the machine: it signs a one-hour token, nothing more. It is looked
for **next to** the environment file, named `AuthKey_<KEY_ID>.p8`.

`ventes-appstore.py` can also be run on its own, to download Apple's reports. It does not read
`config.json`: give it the same path through the `ASC_VENTES_ENV` variable, otherwise it will
look in `~/.appstore/ventes.env`.

---

## 2. Serving the page

An nginx that never leaves the machine:

```yaml
tableau:
  image: nginx:alpine
  ports:
    - "127.0.0.1:8090:80"        # ⛔ 127.0.0.1, never 0.0.0.0
  volumes:
    - ./tableau-de-bord:/usr/share/nginx/html:ro
    - ./tableau-de-bord/nginx.conf:/etc/nginx/conf.d/default.conf:ro
  restart: unless-stopped
```

⚠️ `nginx.conf` is mounted **as a single file**: the mount follows the inode. After editing
it, `docker compose up -d` is not enough — you need `--force-recreate --no-deps tableau`.

---

## 3. Opening it to your private network, and to it alone

With [Tailscale](https://tailscale.com):

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8090
tailscale serve status        # must say “(tailnet only)”
```

⛔ **Never `tailscale funnel`**: it would publish these figures on the internet.

Three checks beat one promise:

```bash
dig +short your-machine.your-tailnet.ts.net @1.1.1.1   # must answer NOTHING
tailscale funnel status                                 # “tailnet only”
lsof -nP -iTCP -sTCP:LISTEN | grep 8090                 # 127.0.0.1 only
```

On iPhone: open the address in Safari, then **Share → Add to Home Screen**.

⚠️ `titre`, `marque` and `accent` dress the page on every load. **The home-screen icon is an
image**: the installer redraws it with your initials if Pillow is present
(`pip3 install pillow`), otherwise it keeps the shipped “AB · cd” — no setting changes it
afterwards. `icone.svg` shows how it is built.

---

## 4. Refreshing every morning

```bash
python3 scripts/tableau-de-bord.py
```

As a scheduled task, at whatever hour suits you. ⚠️ If you go through GitHub Actions, know
that its `cron` only understands UTC and ignores daylight saving: fire at **both** possible
hours and do the work only when the local hour is the right one.

`tableau-de-bord/donnees.json` **must not be committed**: it holds audience and sales figures.

---

## What the dashboard cannot do

- **Install sources** (App Store search, browse, web referrer): they need the Analytics API,
  so a key with the **Admin** role and a report request filed in advance. They are not here.
- **Search terms** typed in the App Store: Apple only gives those to Apple Search Ads.
- **Build numbers**: out of reach of a Sales key.

---

## Licence

MIT — see `LICENSE`. Use it, change it, ship it; it comes with no warranty.
