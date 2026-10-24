# Plugin InkyPi « Transports IDFM »

Prochains bus, état et prochains trains d'une ligne RER, heure d'arrivée estimée —
données temps réel d'Île-de-France Mobilités (PRIM).

## Clé API

1. Compte gratuit sur https://prim.iledefrance-mobilites.fr
2. Abonne-toi à : *Prochains Passages – requête unitaire* et
   *Calculateur Île-de-France Mobilités – Accès générique (Navitia)*
   (l'info trafic vient aussi de Navitia).
   Pour vérifier la clé et les arrêts : `scripts/idfm_check.py "Nom de l'arrêt"`.
3. Dans le fichier `.env` à la racine d'InkyPi : `PRIM_API_KEY=ta_cle`
4. `sudo systemctl restart inkypi.service`

## Réglages

- **Arrêts** : un nom (« Les Platanes ») ou un identifiant
  (`STIF:StopArea:SP:12345:`). Un nom est résolu une fois puis mémorisé ; le nom
  trouvé pour l'arrêt de bus s'affiche en bas à gauche de l'écran — vérifie-le.
- **Je suis à l'arrêt vers** (08:30 par défaut) : les bus, le train et l'heure
  d'arrivée sont calculés pour cette heure-là, même consultés à 7h30. Au-delà de
  l'horizon du temps réel, les horaires viennent du calculateur et sont marqués
  « ≈ ». Une fois l'heure passée, tout est calculé pour maintenant.
- **Bus → quai du RER** (10 min par défaut) : seuls les trains qu'on peut attraper
  après le premier bus pris à l'arrêt sont affichés.
- **Info trafic** : en rouge seulement si la perturbation touche le trajet (ligne
  entière, gare de montée ou de descente, tronçon parcouru) dans les 90 minutes
  qui suivent l'heure à l'arrêt. Le reste s'affiche en petit sous « Trafic normal ».
- **Météo** (Open-Meteo, sans clé) : autour de l'arrêt de bus ; température et
  ressenti actuels, puis pluie, vent et rafales sur le créneau du trajet à vélo
  (8h15–9h par défaut, réglable ; le créneau n'est pas affiché). Pluie, rafales
  ≥ 50 km/h, verglas, neige et orage passent en rouge.
- **Heure de mise à jour** : toujours affichée en bas à droite.
- **Mode démo** : données fictives, pratique pour régler la mise en page.

## Programmation

1. Crée une playlist « Matin » de 07:30 à 08:30, du lundi au vendredi,
   rafraîchie toutes les 5 minutes (champs « Days » et « Refresh every »).
2. Ajoute-lui ce plugin avec un rafraîchissement **toutes les 5 minutes** : c'est
   ce réglage-là qui décide si les données sont rechargées.

La playlist prend la main à 7h30 pile et la rend à 8h30 (une playlist plus courte
est prioritaire sur la playlist par défaut). InkyPi ne redessine l'écran que si
l'image a changé ; avec l'heure de mise à jour affichée, il la redessine à chaque
rafraîchissement (~30 s). Laisse le compte à rebours désactivé.

Pour vérifier la clé, les arrêts et la météo sur le Pi :
`sudo /usr/local/inkypi/venv_inkypi/bin/python scripts/idfm_check.py "Les Platanes"`

## Notes techniques

Constats faits sur les vraies API PRIM (octobre 2026) :

- **Identifiants d'arrêt** : `stop_area:IDFM:<n>` (Navitia) devient `STIF:StopArea:SP:<n>:`
  pour `stop-monitoring` (vérifié pour Joinville-le-Pont et Les Platanes).
- **Lignes** : les passages temps réel n'ont pas de `PublishedLineName`, seulement
  `LineRef` (106 = `C01135`, 108 = `C01137`, 110 = `C01139`). Le plugin récupère la
  table nom → code via `v2/navitia/stop_areas/<id>/lines` et la garde en cache.
- **Horizon du temps réel** : `stop-monitoring` couvre environ une heure. Au-delà, les
  départs viennent de `v2/navitia/stop_areas/<id>/departures` (marqués « ≈ » s'ils sont
  théoriques), sans doublon avec le temps réel.
- **Info trafic** : `general-message` renvoie surtout des travaux à venir ; on utilise
  `v2/navitia/lines/<ligne>/line_reports`. Les pannes d'ascenseur y sont rattachées à
  une gare (et classées à tort `SIGNIFICANT_DELAYS`), une gare non desservie est un
  tronçon d'une seule gare, et les périodes d'application sont parfois grossières. D'où
  le tri par rapport au trajet (`classer()`), qui s'appuie sur les gares de l'itinéraire.
- **Itinéraire** : le statut global de `journeys` n'est pas exploitable (une panne
  d'ascenseur suffit à le dégrader) ; seules les heures et la liste des gares servent.

Tests : `tests/test_transport_idfm.py`, construits à partir de ces réponses réelles.
