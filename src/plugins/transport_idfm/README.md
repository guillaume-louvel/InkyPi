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
- **Info trafic** : en rouge seulement si la perturbation touche le trajet (ligne
  entière, gare de montée ou de descente, tronçon parcouru) dans les 90 minutes
  qui suivent l'heure à l'arrêt. Le reste s'affiche en petit sous « Trafic normal ».
- **Météo** (Open-Meteo, sans clé) : autour de l'arrêt de bus ; température et
  ressenti actuels, puis pluie, vent et rafales sur le créneau vélo (8h15–9h par
  défaut). Pluie, rafales ≥ 50 km/h, verglas, neige et orage passent en rouge.
- **Mode démo** : données fictives, pratique pour régler la mise en page.

## Programmation

1. Crée une playlist « Matin » de 07:30 à 08:30, du lundi au vendredi,
   rafraîchie toutes les 5 minutes (champs « Days » et « Refresh every »).
2. Ajoute-lui ce plugin avec un rafraîchissement **toutes les 5 minutes** : c'est
   ce réglage-là qui décide si les données sont rechargées.

La playlist prend la main à 7h30 pile et la rend à 8h30 (une playlist plus courte
est prioritaire sur la playlist par défaut). InkyPi ne redessine l'écran que si
l'image a changé ; laisse le compte à rebours et l'heure de MAJ désactivés
(l'écran met ~30 s à se rafraîchir).

Pour vérifier la clé, les arrêts et la météo sur le Pi :
`sudo /usr/local/inkypi/venv_inkypi/bin/python scripts/idfm_check.py "Les Platanes"`
