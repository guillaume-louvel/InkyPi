# Passation — plugin InkyPi « Transports IDFM »

Note de passation entre une session Claude (claude.ai) et Claude Code, 24/10/2026.

## Objectif

Sur un Raspberry Pi Zero 2W équipé d'un **Inky Impression 7.3" Spectra 6**
(800×480, 6 couleurs pures, rafraîchissement complet de ~30 s, pas de
rafraîchissement partiel) et faisant tourner InkyPi, afficher le matin :

- les prochains bus **106, 108, 110** en direction de la gare RER de **Joinville-le-Pont** ;
- l'état du **RER A** et les prochains trains de Joinville-le-Pont vers **Nanterre-Préfecture** (direction ouest) ;
- l'**heure d'arrivée estimée** à Nanterre-Préfecture.

L'**arrêt de montée du bus n'est pas encore connu** : l'utilisateur le saisit dans
l'interface web (nom ou identifiant).

## État au 24/10

| Élément | État |
|---|---|
| Code du plugin | Écrit, sur la branche `transport-idfm` du fork `guillaume-louvel/InkyPi` |
| Rendu | Vérifié via le pipeline réel d'InkyPi (Chromium headless), en paysage et en portrait, en mode démo |
| Logique | Testée sur des réponses SIRI simulées (filtres, conversion d'identifiants, cache des arrêts) |
| **API réelles** | **Jamais appelées** : il n'y avait pas de clé pendant le développement |
| Clé PRIM | Obtenue par l'utilisateur, pas encore placée dans `.env` |
| Installation sur le Pi | Pas faite. L'utilisateur avait perdu son mot de passe SSH et une réinitialisation via `firstrun.sh` et `cmdline.txt` sur la carte SD a été proposée. Vérifier qu'il a récupéré l'accès. |
| PR vers le dépôt original | Une PR a été ouverte par erreur vers `fatihak/InkyPi`. On a conseillé de la fermer, et la branche a été supprimée au passage (d'où sa recréation). **Ne jamais ouvrir de PR vers `fatihak/InkyPi`.** |
| Autre chantier | Hotspot Wi-Fi sur `claude/vigilant-brahmagupta-coll8v`. Compatible : il touche à `config.py`, `image_utils.py` et `refresh_task.py` sans changer l'interface utilisée par le plugin (`render_image`, `load_env_key`, `get_resolution`). |

## Fichiers

```
src/plugins/transport_idfm/
├── transport_idfm.py      # classe TransportIdfm(BasePlugin) : réglages, collecte, rendu
├── idfm_api.py            # client PRIM (SIRI Lite + Navitia), parsing, filtres
├── settings.html          # formulaire de l'interface web (pré-remplissage via pluginSettings)
├── render/transport_idfm.html / .css   # gabarit Jinja rendu par Chromium
├── plugin-info.json, icon.png, README.md
└── HANDOVER.md            # ce fichier
```

## API PRIM utilisées

Base : `https://prim.iledefrance-mobilites.fr/marketplace`. Une seule clé, passée dans l'en-tête
`apikey`, lue depuis `.env` sous le nom `PRIM_API_KEY` (`device_config.load_env_key`).

| Besoin | Endpoint | Paramètres |
|---|---|---|
| Prochains passages | `GET /stop-monitoring` | `MonitoringRef=STIF:StopArea:SP:<code>:` et `LineRef` en option |
| Info trafic RER A | `GET /general-message` | `LineRef=STIF:Line::C01742:` |
| Recherche d'arrêt | `GET /v2/navitia/places` | `q`, `type[]=stop_area` |
| Itinéraire | `GET /v2/navitia/journeys` | `from`/`to` = `stop_area:IDFM:<code>`, `data_freshness=realtime` |

Abonnements PRIM nécessaires : *Prochains Passages – requête unitaire*, *Messages Info
Trafic* et *Calculateur IDFM – Accès générique (Navitia)*. Ne pas utiliser la requête
globale, bien trop lourde pour un Zero 2W.

## Vérifié avec la vraie API (24/10, `scripts/idfm_check.py`)

- **Identifiants** : `stop_area:IDFM:<n>` → `STIF:StopArea:SP:<n>:` fonctionne pour
  Joinville-le-Pont (70640) et l'arrêt de bus Les Platanes (70606). Le point 1 ci-dessous est réglé.
- **Chemins Navitia sans `coverage/…`** : fonctionnent (point 2 réglé).
- **`PublishedLineName` est absent** des passages (point 4) : seul `LineRef` est fourni
  (106 = `C01135`, 108 = `C01137`, 110 = `C01139`). Le plugin récupère la table
  nom → code via `stop_areas/<id>/lines` et filtre sur le code.
- **Info trafic** (point 3) : `general-message` renvoyait surtout des travaux à venir, de nuit ou
  sur d'autres branches, et le bandeau aurait été rouge en permanence. Remplacé par
  `v2/navitia/lines/line:IDFM:C01742/line_reports`, filtré sur les perturbations qui touchent la
  ligne ou un tronçon (pas les pannes d'ascenseur, rattachées à une gare) et dont une période
  d'application tombe dans les 90 prochaines minutes. `severity.effect` n'est pas fiable : les
  pannes d'ascenseur y sont en `SIGNIFICANT_DELAYS`. Hypothèse restante : que les vraies
  perturbations de trafic soient bien rattachées à la ligne ou à un tronçon.
- **Sens du RER** (point 5) : les destinations réelles sont bien « Boissy-Saint-Léger » et
  « Saint-Germain-en-Laye ».

## Points à vérifier en priorité avec la vraie API

Hypothèses de départ, conservées pour l'historique (voir ci-dessus) :

1. **Correspondance des identifiants.** `vers_siri()` transforme `stop_area:IDFM:12345` en
   `STIF:StopArea:SP:12345:` en supposant que le code numérique est le même des deux côtés
   (zone d'arrêt). C'est le point le plus fragile. Si `stop-monitoring` ne renvoie rien,
   tester avec un `STIF:StopPoint:Q:…:` ou chercher l'identifiant dans le référentiel
   `arrets-lignes` sur data.iledefrance-mobilites.fr.
2. **Chemin Navitia.** `v2/navitia/places` et `v2/navitia/journeys` sont appelés sans
   `coverage/…`. Si PRIM répond 404, essayer `v2/navitia/coverage/idfm/…`.
3. **`general-message` filtré par `LineRef`.** Vérifier que le paramètre est accepté et que
   `InfoChannelRef` vaut bien « Perturbation » pour les incidents. Sinon le bandeau restera
   toujours vert.
4. **Libellé des lignes de bus.** Le filtre compare `PublishedLineName` à « 106 », « 108 » et
   « 110 ». Vérifier le format réel, qui pourrait inclure un préfixe ou l'identifiant `C0xxxx`.
5. **Sens du RER.** Les trains vers l'est sont écartés si leur destination contient Boissy,
   Sucy, Varenne, Saint-Maur ou Champigny. Vérifier les destinations réellement affichées
   (`DestinationDisplay`).
6. **Persistance du cache d'arrêts.** `_resoudre()` enregistre la résolution nom → identifiant
   dans `settings["_<champ>Cache"]`. La doc InkyPi indique que les réglages modifiés dans
   `generate_image` sont conservés pour les instances de playlist, mais peut-être pas pour
   « Update Now ». Au pire, la recherche est refaite à chaque rafraîchissement, ce qui coûte
   un appel de plus sans rien casser.

Pour tester rapidement sans l'interface web, depuis `src/` :

```python
from zoneinfo import ZoneInfo
from plugins.transport_idfm.idfm_api import PrimClient
c = PrimClient("<clé>", ZoneInfo("Europe/Paris"))
print(c.chercher_arret("Joinville-le-Pont"))
print(c.passages("STIF:StopArea:SP:<code>:", "STIF:Line::C01742:")[:5])
print(c.perturbations("STIF:Line::C01742:"))
```

## Choix de conception

- **On redessine l'écran le moins possible.** InkyPi ne rafraîchit que si le hash de l'image
  change. Le compte à rebours « X min » et l'heure de mise à jour sont donc **désactivés par
  défaut** (cases à cocher), sinon l'écran clignoterait à chaque cycle. Les heures sont
  affichées en absolu (« 7h43 »).
- **Couleurs** limitées aux 6 couleurs pures du Spectra 6 (`#000 #fff #f00 #0f0 #00f #ff0`)
  pour éviter le tramage.
- **CSS** basé sur une unité `--u` (1dvh en paysage, 0.85dvw en portrait) pour gérer les deux
  orientations.
- **Les erreurs n'interrompent pas l'affichage** : chaque bloc est collecté séparément, et les
  erreurs partielles s'affichent en rouge en bas de l'écran. Une `RuntimeError` est levée vers
  l'interface web seulement si rien n'est exploitable.
- **Mode démo** (case à cocher) : données fictives, sans clé, pratique pour la mise en page.
- **Programmation** prévue : une playlist InkyPi de 07:30 à 08:15, rafraîchie toutes les 2 à
  3 min, prioritaire car plus courte que la playlist habituelle (comics, météo). Limite : les
  playlists n'ont pas de jours de semaine, donc l'affichage a lieu aussi le week-end.

## Pistes ensuite

- Gérer les jours de semaine, soit dans le plugin (rendu « pas de trajet aujourd'hui »), soit
  dans le modèle `Playlist` d'InkyPi.
- Utiliser l'un des 4 boutons de l'Inky pour afficher le tracker à la demande.
- Ajouter des tests unitaires dans `tests/` à partir de vraies réponses PRIM une fois
  capturées (les JSON SIRI simulés sont reconstruits dans l'historique de la conversation,
  pas versionnés).

## Installation sur le Pi (rappel)

```bash
cd ~/InkyPi
git remote add fork https://github.com/guillaume-louvel/InkyPi   # si absent
git fetch fork && git checkout -b transport-idfm fork/transport-idfm
echo "PRIM_API_KEY=<clé>" >> .env
sudo systemctl restart inkypi.service
journalctl -u inkypi.service -f     # logs
```

`/usr/local/inkypi/src` est un lien symbolique vers `~/InkyPi/src`, donc changer de branche
suffit. Si le Pi est sur la branche hotspot, il faudra fusionner les deux branches dans une
branche d'intégration, ou cherry-picker le dossier du plugin.
