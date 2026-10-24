"""Météo du trajet à vélo, via Open-Meteo (gratuit, sans clé ; modèles Météo-France pour la France)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

from utils.app_utils import resolve_path

URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 20

# Seuils au-delà desquels une info passe en rouge pour un trajet à vélo
PLUIE_MM = 0.2          # cumul sur le trajet
PLUIE_PROBA = 60        # %
RAFALES_KMH = 50
GEL_C = 1

ETATS = {
    0: "Ciel dégagé", 1: "Peu nuageux", 2: "Partiellement nuageux", 3: "Couvert",
    45: "Brouillard", 48: "Brouillard givrant",
    51: "Bruine faible", 53: "Bruine", 55: "Bruine dense", 56: "Bruine verglaçante", 57: "Bruine verglaçante",
    61: "Pluie faible", 63: "Pluie", 65: "Pluie forte", 66: "Pluie verglaçante", 67: "Pluie verglaçante",
    71: "Neige faible", 73: "Neige", 75: "Neige forte", 77: "Grésil",
    80: "Averses faibles", 81: "Averses", 82: "Averses violentes", 85: "Averses de neige", 86: "Averses de neige",
    95: "Orage", 96: "Orage et grêle", 99: "Orage et grêle",
}

# Codes WMO -> icônes du plugin météo d'InkyPi
ICONES = {0: "01d", 1: "022d", 2: "02d", 3: "04d", 45: "50d", 48: "48d",
          51: "51d", 61: "51d", 80: "51d", 53: "53d", 63: "53d", 81: "53d", 55: "09d", 65: "09d", 82: "09d",
          56: "56d", 66: "56d", 57: "57d", 67: "57d", 71: "71d", 85: "71d", 73: "73d", 75: "13d", 86: "13d",
          77: "77d", 95: "11d", 96: "11d", 99: "11d"}
ICONES_NUIT = {"01d": "01n", "022d": "022n", "02d": "02n"}


def icone(code: int, jour: bool) -> str:
    nom = ICONES.get(code, "01d")
    if not jour:
        nom = ICONES_NUIT.get(nom, nom)
    return resolve_path(f"plugins/weather/icons/{nom}.png")


def h(d: datetime) -> str:
    return d.strftime("%-Hh%M").replace("h00", "h")


@dataclass
class Meteo:
    temperature: float
    ressenti: float
    code: int
    jour: bool
    debut: datetime
    fin: datetime
    t_min: float | None = None
    t_max: float | None = None
    ressenti_min: float | None = None
    pluie_mm: float = 0.0
    pluie_proba: int | None = None
    pluie_debut: datetime | None = None
    vent: float | None = None
    rafales: float | None = None
    code_trajet: int | None = None
    alertes: list[str] = field(default_factory=list)

    def vue(self) -> dict:
        """Textes prêts à afficher."""
        code = self.code_trajet if self.code_trajet is not None else self.code
        if self.pluie_debut:
            pluie = f"Pluie dès {h(self.pluie_debut)} ({self.pluie_mm:.1f} mm)".replace(".", ",")
        elif self.pluie_proba is not None and self.pluie_proba >= 30:
            pluie = f"Risque de pluie {self.pluie_proba} %"
        else:
            pluie = "Pas de pluie prévue"
        temp = (f"{round(self.t_min)}° à {round(self.t_max)}°" if self.t_min is not None
                and round(self.t_min) != round(self.t_max) else f"{round(self.temperature)}°")
        return {
            "temperature": round(self.temperature),
            "ressenti": round(self.ressenti),
            "icone": icone(self.code, self.jour),
            "etat": ETATS.get(code, ""),
            "temp_trajet": temp + (f", ressenti {round(self.ressenti_min)}°" if self.ressenti_min is not None else ""),
            "pluie": pluie,
            "vent": (f"Vent {round(self.vent)} km/h, rafales {round(self.rafales)}"
                     if self.vent is not None and self.rafales is not None else ""),
            "alertes": self.alertes,
        }


def prevision(lat: float, lon: float, tz, debut: datetime, fin: datetime) -> Meteo:
    r = requests.get(URL, timeout=TIMEOUT, params={
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,"
                  "wind_speed_10m,wind_gusts_10m",
        "minutely_15": "precipitation",
        "timezone": tz.zone if hasattr(tz, "zone") else str(tz),
        "forecast_days": 2,
    })
    r.raise_for_status()
    return analyser(r.json(), tz, debut, fin)


def analyser(data: dict, tz, debut: datetime, fin: datetime) -> Meteo:
    """Résume la réponse Open-Meteo : maintenant, et le pire du créneau [debut, fin[."""
    def heure(s):
        naive = datetime.fromisoformat(s)
        return tz.localize(naive) if hasattr(tz, "localize") else naive.replace(tzinfo=tz)

    actuel = data.get("current") or {}
    m = Meteo(temperature=actuel.get("temperature_2m", 0), ressenti=actuel.get("apparent_temperature", 0),
              code=int(actuel.get("weather_code") or 0), jour=bool(actuel.get("is_day", 1)), debut=debut, fin=fin)

    # Heures qui recoupent le créneau
    horaire = data.get("hourly") or {}
    lignes = [i for i, t in enumerate(horaire.get("time") or [])
              if heure(t) < fin and heure(t) + timedelta(hours=1) > debut]

    def valeurs(cle):
        serie = horaire.get(cle) or []
        return [serie[i] for i in lignes if i < len(serie) and serie[i] is not None]

    if temperatures := valeurs("temperature_2m"):
        m.t_min, m.t_max = min(temperatures), max(temperatures)
    if ressentis := valeurs("apparent_temperature"):
        m.ressenti_min = min(ressentis)
    if probas := valeurs("precipitation_probability"):
        m.pluie_proba = int(max(probas))
    if vents := valeurs("wind_speed_10m"):
        m.vent = max(vents)
    if rafales := valeurs("wind_gusts_10m"):
        m.rafales = max(rafales)
    if codes := valeurs("weather_code"):
        m.code_trajet = int(max(codes))  # les codes WMO croissent à peu près avec la gravité

    # Pluie au quart d'heure
    quarts = data.get("minutely_15") or {}
    for t, mm in zip(quarts.get("time") or [], quarts.get("precipitation") or []):
        quand = heure(t)
        if mm is None or not (quand + timedelta(minutes=15) > debut and quand < fin):
            continue
        m.pluie_mm += mm
        if mm >= 0.1 and m.pluie_debut is None:
            m.pluie_debut = max(quand, debut)

    # Ce qui compte à vélo
    if m.pluie_mm >= PLUIE_MM and m.pluie_debut:
        m.alertes.append(f"Pluie dès {h(m.pluie_debut)}")
    elif m.pluie_proba is not None and m.pluie_proba >= PLUIE_PROBA:
        m.alertes.append(f"Pluie probable ({m.pluie_proba} %)")
    if m.rafales is not None and m.rafales >= RAFALES_KMH:
        m.alertes.append(f"Rafales à {round(m.rafales)} km/h")
    t_min = m.t_min if m.t_min is not None else m.temperature
    if t_min <= GEL_C:
        m.alertes.append("Risque de verglas")
    code = m.code_trajet if m.code_trajet is not None else m.code
    if code >= 95:
        m.alertes.append("Orage")
    elif code in (71, 73, 75, 77, 85, 86):
        m.alertes.append("Neige")
    return m
