"""Climat intérieur et extérieur : capteur BME280, météo Open-Meteo, confort, moment pour aérer et, au choix, courbes sur 7 jours."""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytz
import requests

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.transport_idfm.meteo import ETATS
from utils import climate

logger = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
DEFAUTS = {"latitude": "48.82", "longitude": "2.47"}  # Joinville-le-Pont

SEMAINE = timedelta(days=7)
TROU_MAX = timedelta(seconds=climate.INTERVAL_SECONDS * 1.5)  # au-delà, la courbe est interrompue (Pi éteint)
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


@dataclass
class Exterieur:
    actuel: Meteo
    ressenti: float
    etat: str
    t_min: float | None = None          # aujourd'hui (journée locale)
    t_max: float | None = None
    prevues: list = field(default_factory=list)      # les 24 h à venir, heure par heure
    historique: list = field(default_factory=list)   # les 7 derniers jours (disposition avec courbes)

    def pour_aerer(self):
        return [self.actuel] + self.prevues


def meteo_exterieure(latitude, longitude, tz, semaine=False):
    """Une seule requête Open-Meteo : maintenant, les 24 h à venir et, si demandé, les 7 derniers jours."""
    params = {
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation",
        "forecast_days": 2,
    }
    if semaine:
        params["past_days"] = 7
    r = requests.get(OPEN_METEO, timeout=20, params=params)
    r.raise_for_status()
    return analyser_meteo(r.json(), tz, datetime.now(timezone.utc))


def analyser_meteo(data, tz, maintenant):
    """Réponse Open-Meteo (heures en UTC) -> Exterieur."""
    actuel = data.get("current") or {}
    ext = Exterieur(
        actuel=Meteo(maintenant, actuel["temperature_2m"], actuel["relative_humidity_2m"], actuel.get("precipitation") or 0),
        ressenti=actuel.get("apparent_temperature", actuel["temperature_2m"]),
        etat=ETATS.get(int(actuel.get("weather_code") or 0), ""),
    )
    minuit = tz.localize(datetime.combine(maintenant.astimezone(tz).date(), datetime.min.time()))
    lendemain = tz.localize(datetime.combine(minuit.date() + timedelta(days=1), datetime.min.time()))
    aujourdhui = []
    h = data.get("hourly") or {}
    for t, temp, hum, pluie in zip(h.get("time", []), h.get("temperature_2m", []),
                                   h.get("relative_humidity_2m", []), h.get("precipitation", [])):
        if None in (temp, hum):
            continue
        meteo = Meteo(datetime.fromisoformat(t).replace(tzinfo=timezone.utc), temp, hum, pluie or 0)
        if minuit <= meteo.time < lendemain:
            aujourdhui.append(temp)
        if meteo.time <= maintenant:
            if maintenant - meteo.time <= SEMAINE:
                ext.historique.append(meteo)
        elif meteo.time <= maintenant + timedelta(hours=24):
            ext.prevues.append(meteo)
    if aujourdhui:
        ext.t_min, ext.t_max = min(aujourdhui), max(aujourdhui)
    return ext


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
def graphe(courbes, maintenant, tz, bas_min, haut_min, unite="", zone=None):
    """Courbes des 7 derniers jours pour le SVG, sur une même échelle.

    `courbes` : {"int": [(heure, valeur)], "ext": [...]}. Chaque courbe est découpée en segments,
    interrompus aux trous (Pi éteint). L'échelle couvre au moins [bas_min, haut_min] pour ne pas
    exagérer de petites variations ; `zone` : valeurs tracées en pointillés (plage confortable)."""
    debut = maintenant - SEMAINE
    courbes = {nom: [(t, v) for t, v in points if debut <= t <= maintenant] for nom, points in courbes.items()}
    if len(courbes.get("int", [])) < 2:
        return None
    valeurs = [v for points in courbes.values() for _, v in points]
    bas, haut = min(min(valeurs), bas_min), max(max(valeurs), haut_min)

    def x(t):
        return round((t - debut).total_seconds() / SEMAINE.total_seconds() * GRAPHE_L, 1)

    def y(v):
        return round(GRAPHE_H - (v - bas) / (haut - bas) * GRAPHE_H, 1)

    traces = {}
    for nom, points in courbes.items():
        segments, courant, precedent = [], [], None
        for t, v in points:
            if precedent and t - precedent > TROU_MAX and courant:
                segments.append(courant)
                courant = []
            courant.append(f"{x(t)},{y(v)}")
            precedent = t
        segments.append(courant)
        traces[nom] = [" ".join(seg) for seg in segments if len(seg) > 1]

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
        "traces": traces,
        "minuits": minuits,
        "jours": jours,
        "zone": [{"y": y(v), "texte": f"{v}{unite}"} for v in zone] if zone else [],
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
        if not historique or maintenant - historique[-1].time > timedelta(seconds=climate.MAX_AGE_SECONDS):
            raise RuntimeError("Aucune mesure récente du capteur BME280. Est-il bien branché ? "
                               "(sudo i2cdetect -y 1 doit afficher 76 ou 77)")
        actuel = historique[-1]

        courbes = settings.get("layout") == "graphs"
        exterieur = None
        try:
            exterieur = meteo_exterieure(settings.get("latitude") or DEFAUTS["latitude"],
                                         settings.get("longitude") or DEFAUTS["longitude"], tz, semaine=courbes)
        except Exception as ex:  # noqa: BLE001 - l'intérieur reste affiché
            logger.warning(f"Météo extérieure indisponible : {ex}")

        mesure = actuel.time.astimezone(tz)
        params = {
            "courbes": courbes,
            "interieur": {
                "temperature": nombre(actuel.temperature),
                "humidite": round(actuel.humidity),
                "confort": confort(actuel.temperature, actuel.humidity),
                "mesure": f"mesuré à {mesure.hour}h{mesure:%M}".replace("h00", "h"),
            },
            "exterieur": {
                "temperature": nombre(exterieur.actuel.temperature),
                "humidite": round(exterieur.actuel.humidite),
                "etat": exterieur.etat,
                "ressenti": nombre(exterieur.ressenti, 0),
                "min_max": (f"min {nombre(exterieur.t_min, 0)}° / max {nombre(exterieur.t_max, 0)}° aujourd'hui"
                            if exterieur.t_min is not None else ""),
            } if exterieur else None,
            "aerer": moment_pour_aerer(actuel, exterieur.pour_aerer(), tz) if exterieur else "",
            "graphe_l": GRAPHE_L, "graphe_h": GRAPHE_H,
            "plugin_settings": settings,
        }
        if courbes:
            dehors = exterieur.historique if exterieur else []
            params["graphe_temp"] = graphe({"int": [(r.time, r.temperature) for r in historique],
                                            "ext": [(m.time, m.temperature) for m in dehors]},
                                           maintenant, tz, 18, 22, "°")
            params["graphe_hum"] = graphe({"int": [(r.time, r.humidity) for r in historique],
                                           "ext": [(m.time, m.humidite) for m in dehors]},
                                          maintenant, tz, 30, 70, " %", zone=HUMIDITE_CONFORT)
        image = self.render_image(dimensions, "indoor_climate.html", "indoor_climate.css", params)
        if image is None:
            raise RuntimeError("Le rendu de l'image a échoué.")
        return image
