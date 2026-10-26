# Plugin InkyPi « Climat intérieur »

Température et humidité de la pièce (capteur BME280 branché sur le Pi), face à celles du
dehors (Open-Meteo, sans clé), avec le confort de la pièce et le moment pour aérer.

## Dispositions (réglage « Disposition »)

- **Grand format** (par défaut) : intérieur et extérieur côte à côte en grand, lisibles de
  loin ; conseil d'aération en bas.
- **Avec les courbes de la semaine** : les mêmes informations en cartes, plus les courbes
  de température et d'humidité sur 7 jours (trait plein : intérieur, pointillés : extérieur).
  L'historique extérieur vient d'Open-Meteo : rien de plus n'est stocké sur le Pi.

- **Mesures** : relevées toutes les heures par InkyPi, qui garde une semaine
  d'historique (≈ 170 lignes, `src/config/climate.sqlite3`). L'heure de la dernière
  mesure est affichée en haut de l'écran. L'enregistrement démarre
  tout seul si un BME280 est détecté (adresse 0x76 ou 0x77 : `sudo i2cdetect -y 1`).
- **Confort** : 18–24 °C et 40–60 % d'humidité sont considérés comme confortables ;
  en dehors, un message l'indique (air sec, humide, risque de moisissures, frais, chaud).
- **Extérieur** : température, humidité, ciel, ressenti et min/max du jour au logement.
- **Fenêtres** : à partir de la météo Open-Meteo du logement, dit s'il est
  bon d'aérer maintenant, ou à partir de quelle heure. Pièce chaude (> 24 °C) : quand il
  fait au moins 2 °C plus frais dehors. Sinon : quand l'air du dehors contient moins
  d'eau que celui de la pièce. Jamais sous la pluie.
- **Courbes** (disposition avec courbes) : les pointillés horizontaux marquent la plage
  d'humidité confortable. Une courbe intérieure interrompue signale que le Pi était éteint.

## Correction de température

Collé au Pi, le capteur mesure souvent 1 à 3 °C de trop. Compare avec un thermomètre,
puis règle **Temperature sensor correction** dans les paramètres d'InkyPi (ex. `-1,5`).
L'humidité est recalculée en conséquence, et la correction s'applique aussi à
l'historique déjà enregistré.

Une mesure par heure : rafraîchir le plugin toutes les heures suffit.
