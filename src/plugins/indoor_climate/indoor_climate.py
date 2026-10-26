"""Climat intérieur : température et humidité du capteur BME280, conseils, courbes sur 7 jours et moment pour aérer."""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytz
import requests

from plugins.base_plugin.base_plugin import BasePlugin
from utils import climate

logger = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
DEFAUTS = {"latitude": "48.82", "longitude": "2.47"}  # Joinville-le-Pont

SEMAINE = timedelta(days=7)
TROU_MAX = timedelta(minutes=25)   # au-delà, la courbe est interrompue (Pi éteint)
CHAUD = 24                         # °C : au-dessus, aérer sert à rafraîchir
ECART_FRAIS = 2                    # °C plus frais dehors pour que ça vaille la peine
PLUIE_MM = 0.2                     # par heure : on n'ouvre pas sous la pluie
FROID_DEHORS = 12                  # °C : en dessous, quelques minutes suffisent
HUMIDITE_CONFORT = (40, 60)

GRAPHE_L, GRAPHE_H = 700, 150      # repère du SVG des courbes


def nombre(x, decimales=1):
    """Format français : 19,5"""
    return f"{x:.{decimales}f}".replace(".", ",").replace("-", "−")


def heure(d, tz):
    return f"{d.astimezone(tz).hour}h"


# ------------------------------------------------------------------ confort
def confort(temperature, humidite):
    """Ce qui ne va pas, ou « Confortable »."""
    problemes = []
    if temperature < 16:
        problemes.append("Froid")
    elif temperature < 18:
        problemes.append("Frais")
    elif temperature > 27:
        problemes.append("Très chaud")
    elif temperature > CHAUD:
        problemes.append("Chaud")
    if humidite < 30:
        problemes.append("Air très sec")
    elif humidite < HUMIDITE_CONFORT[0]:
        problemes.append("Air sec")
    elif humidite > 70:
        problemes.append("Très humide : risque de moisissures")
    elif humidite > HUMIDITE_CONFORT[1]:
        problemes.append("Air humide")
    return " · ".join(problemes) or "Confortable"


# ------------------------------------------------------------------ aérer
@dataclass
class Meteo:
    """Conditions extérieures à une heure donnée."""
    time: datetime
    temperature: float
    humidite: float
    pluie: float  # mm sur l'heure


def previsions(latitude, longitude):
    """Maintenant, puis heure par heure sur 24 h (heures en UTC)."""
    r = requests.get(OPEN_METEO, timeout=20, params={
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,precipitation",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation",
        "forecast_days": 2,
    })
    r.raise_for_status()
    data = r.json()
    maintenant = datetime.now(timezone.utc)
    actuel = data.get("current") or {}
    res = [Meteo(maintenant, actuel["temperature_2m"], actuel["relative_humidity_2m"], actuel.get("precipitation") or 0)]
    h = data.get("hourly") or {}
    for t, temp, hum, pluie in zip(h.get("time", []), h.get("temperature_2m", []),
                                   h.get("relative_humidity_2m", []), h.get("precipitation", [])):
        quand = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        if maintenant < quand <= maintenant + timedelta(hours=24) and None not in (temp, hum):
            res.append(Meteo(quand, temp, hum, pluie or 0))
    return res


def moment_pour_aerer(interieur, meteo, tz):
    """Quand ouvrir les fenêtres : maintenant, vers une heure donnée, ou pas aujourd'hui.

    Pièce chaude : quand il fait nettement plus frais dehors. Sinon : quand l'air du dehors contient
    moins d'eau que celui de la pièce (aérer renouvelle l'air sans l'humidifier). Jamais sous la pluie.
    """
    chaud = interieur.temperature > CHAUD
    vapeur = climate.vapour_pressure(interieur.temperature, interieur.humidity)

    def favorable(m):
        if m.pluie >= PLUIE_MM:
            return False
        if chaud:
            return m.temperature <= interieur.temperature - ECART_FRAIS
        return climate.vapour_pressure(m.temperature, m.humidite) < vapeur

    actuel, suite = meteo[0], meteo[1:]
    dehors = f"{nombre(actuel.temperature, 0)}° dehors"
    if favorable(actuel):
        if chaud:
            return f"Bon moment pour aérer : plus frais dehors ({nombre(actuel.temperature, 0)}°)"
        if actuel.temperature < FROID_DEHORS:
            return f"Bon moment pour aérer : 5 à 10 min suffisent ({dehors})"
        return f"Bon moment pour aérer ({dehors})"

    prochain = next((m for m in suite if favorable(m)), None)
    if actuel.pluie >= PLUIE_MM:
        pourquoi = "Il pleut"
    elif chaud:
        pourquoi = f"Plus chaud dehors ({nombre(actuel.temperature, 0)}°)"
    else:
        pourquoi = "Air plus humide dehors"
    if prochain:
        return f"{pourquoi} : aérez plutôt vers {heure(prochain.time, tz)} ({nombre(prochain.temperature, 0)}°)"
    return f"{pourquoi} : gardez les fenêtres fermées aujourd'hui"


# ------------------------------------------------------------------ courbes
def graphe(historique, valeur, maintenant, tz, bas_min, haut_min, decimales=0, unite="", zone=None):
    """Courbe des 7 derniers jours pour le SVG : segments (interrompus aux trous), jours et extrêmes.

    L'échelle couvre au moins [bas_min, haut_min] pour ne pas exagérer de petites variations.
    `zone` : (bas, haut) tracés en pointillés, par exemple la plage d'humidité confortable."""
    debut = maintenant - SEMAINE
    points = [(r.time, valeur(r)) for r in historique if r.time >= debut]
    if len(points) < 2:
        return None
    valeurs = [v for _, v in points]
    bas, haut = min(min(valeurs), bas_min), max(max(valeurs), haut_min)

    def x(t):
        return round((t - debut).total_seconds() / SEMAINE.total_seconds() * GRAPHE_L, 1)

    def y(v):
        return round(GRAPHE_H - (v - bas) / (haut - bas) * GRAPHE_H, 1)

    segments, courant, precedent = [], [], None
    for t, v in points:
        if precedent and t - precedent > TROU_MAX and courant:
            segments.append(courant)
            courant = []
        courant.append(f"{x(t)},{y(v)}")
        precedent = t
    segments.append(courant)

    # Un trait à chaque minuit, le nom du jour au milieu de sa journée
    # (localize() pour que minuit reste minuit les jours de changement d'heure)
    minuits, jours = [], []
    aujourdhui = maintenant.astimezone(tz).date()
    for recul in range(8):
        date = aujourdhui - timedelta(days=recul)
        minuit = tz.localize(datetime.combine(date, datetime.min.time()))
        midi = tz.localize(datetime.combine(date, datetime.min.time()) + timedelta(hours=12))
        if minuit > debut:
            minuits.append(x(minuit))
        if debut < midi < maintenant:
            jours.append({"x": x(midi), "texte": JOURS[date.weekday()]})

    return {
        "segments": [" ".join(s) for s in segments if len(s) > 1],
        "minuits": minuits,
        "jours": jours,
        "zone": [{"y": y(v), "texte": f"{v}{unite}"} for v in zone] if zone else [],
        "min": nombre(min(valeurs), decimales) + unite,
        "max": nombre(max(valeurs), decimales) + unite,
    }


# ------------------------------------------------------------------ plugin
class IndoorClimate(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["defaults"] = DEFAUTS
        return template_params

    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/Paris"))
        maintenant = datetime.now(timezone.utc)
        correction = float(device_config.get_config("climate_temp_offset", default=0) or 0)

        historique = climate.history(SEMAINE.total_seconds(), correction)
        if not historique or maintenant - historique[-1].time > timedelta(minutes=30):
            raise RuntimeError("Aucune mesure récente du capteur BME280. Est-il bien branché ? "
                               "(sudo i2cdetect -y 1 doit afficher 76 ou 77)")
        actuel = historique[-1]

        try:
            aerer = moment_pour_aerer(actuel, previsions(settings.get("latitude") or DEFAUTS["latitude"],
                                                         settings.get("longitude") or DEFAUTS["longitude"]), tz)
        except Exception as ex:  # noqa: BLE001 - l'intérieur reste affiché
            logger.warning(f"Météo extérieure indisponible : {ex}")
            aerer = ""

        local = maintenant.astimezone(tz)
        params = {
            "date": f"{JOURS[local.weekday()].capitalize()} {local:%d/%m} · {local.hour}h{local:%M}",
            "temperature": nombre(actuel.temperature),
            "humidite": round(actuel.humidity),
            "confort": confort(actuel.temperature, actuel.humidity),
            "aerer": aerer,
            "graphe_temp": graphe(historique, lambda r: r.temperature, maintenant, tz, 18, 22, 1, "°"),
            "graphe_hum": graphe(historique, lambda r: r.humidity, maintenant, tz, 30, 70, 0, " %",
                                 zone=HUMIDITE_CONFORT),
            "graphe_l": GRAPHE_L, "graphe_h": GRAPHE_H,
            "plugin_settings": settings,
        }
        image = self.render_image(dimensions, "indoor_climate.html", "indoor_climate.css", params)
        if image is None:
            raise RuntimeError("Le rendu de l'image a échoué.")
        return image
