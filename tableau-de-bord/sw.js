/* Le tableau de bord hors ligne.
 *
 * ⭐ Pourquoi : la page est posée sur l'écran d'accueil d'un téléphone, et elle n'est servie
 * que sur le réseau privé. Le 27/10/2026, Tailscale coupé sur l'iPhone, elle n'affichait plus
 * que « The figures did not arrive » — alors que les chiffres du matin étaient parfaitement
 * lisibles, et suffisants. Ce fichier garde la dernière copie et la rend quand le réseau
 * manque ; la page, elle, dit l'âge de ce qu'elle montre (voir le bandeau de fraîcheur).
 *
 * ⚠️ LA PAGE ET LA FEUILLE DE STYLE CHANGENT ENSEMBLE. Un socle servi depuis le cache en
 * priorité rendrait une page ancienne avec des classes nouvelles, ou l'inverse : les SVG
 * retomberaient à 300 × 150 pixels. Donc le HTML passe TOUJOURS par le réseau d'abord, et le
 * cache ne sert que de filet. La feuille porte une empreinte ?v=N : son adresse change avec
 * elle, le cache ne peut pas mentir.
 */
var CACHE = 'tdb-1';
var SOCLE = ['/', '/index.html', '/style.css?v=3', '/icone.svg', '/icone-180.png',
             '/icone-512.png', '/manifest.webmanifest'];

self.addEventListener('install', function (ev) {
  // Chaque fichier à part : un seul manquant ferait échouer tout addAll, et l'installation
  // avec lui — un socle incomplet vaut mieux que pas de socle.
  ev.waitUntil(caches.open(CACHE).then(function (c) {
    return Promise.all(SOCLE.map(function (u) {
      return c.add(new Request(u, { cache: 'reload' })).catch(function () {});
    }));
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (ev) {
  ev.waitUntil(caches.keys().then(function (noms) {
    return Promise.all(noms.filter(function (x) { return x !== CACHE; })
                            .map(function (x) { return caches.delete(x); }));
  }).then(function () { return self.clients.claim(); }));
});

function reseauDAbord(ev, garder) {
  return fetch(ev.request).then(function (r) {
    if (garder && r && r.ok) {
      var copie = r.clone();
      caches.open(CACHE).then(function (c) { c.put(ev.request, copie); });
    }
    return r;
  }).catch(function (err) {
    return caches.match(ev.request, { ignoreSearch: false }).then(function (c) {
      if (c) return c;
      // Rien en cache : on laisse l'erreur remonter, la page a son propre message.
      throw err;
    });
  });
}

self.addEventListener('fetch', function (ev) {
  var req = ev.request;
  if (req.method !== 'GET') return;                       // /recalculer : jamais touché
  var u = new URL(req.url);
  if (u.origin !== self.location.origin) return;
  if (u.pathname === '/etat-recalcul') return;            // un état n'a pas de passé
  // Les chiffres : le réseau fait foi, le cache est le filet.
  if (u.pathname === '/donnees.json') {
    ev.respondWith(reseauDAbord(ev, true));
    return;
  }
  // Le socle : réseau d'abord lui aussi pour le document, afin que page et feuille de style
  // n'arrivent jamais de deux époques différentes.
  if (req.mode === 'navigate' || u.pathname === '/' || u.pathname === '/index.html') {
    ev.respondWith(reseauDAbord(ev, true));
    return;
  }
  // Le reste — feuille, icônes, manifeste — porte une adresse qui change avec son contenu.
  ev.respondWith(caches.match(req).then(function (c) {
    return c || reseauDAbord(ev, true);
  }));
});
