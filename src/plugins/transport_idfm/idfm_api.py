"""Accès aux API PRIM d'Île-de-France Mobilités (prochains passages, info trafic, itinéraire)."""
from __future__ import annotations

import html
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

import requests

logger = logging.getLogger(__name__)

BASE = "https://prim.iledefrance-mobilites.fr/marketplace"
TIMEOUT = 20


def norm(s: str) -> str:
    """Minuscules sans accents, pour comparer des noms d'arrêts ou de destinations."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def val(x):
    """Les champs SIRI Lite sont tantôt {"value": ...}, tantôt [{"value": ...}]."""
    if isinstance(x, list):
        x = x[0] if x else None
    if isinstance(x, dict):
        return x.get("value")
    return x


def liste(texte: str) -> list[str]:
    return [t.strip() for t in (texte or "").replace(";", ",").split(",") if t.strip()]


def code_ligne(ref: str) -> str:
    """STIF:Line::C01742: ou line:IDFM:C01742 -> C01742"""
    morceaux = [p for p in (ref or "").split(":") if p]
    return morceaux[-1] if morceaux else ""


@dataclass
class Passage:
    ligne: str
    destination: str
    heure: datetime | None
    statut: str = ""
    ligne_ref: str = ""  # STIF:Line::C01135: — PublishedLineName est souvent absent
    prevu: bool = False  # horaire théorique, pas temps réel

    @property
    def annule(self) -> bool:
        return norm(self.statut) == "cancelled"


@dataclass
class Perturbation:
    texte: str
    sections: list[tuple[str, str]] = field(default_factory=list)  # (gare de début, gare de fin)
    ligne_entiere: bool = False


@dataclass
class Trajet:
    depart: datetime | None
    arrivee: datetime | None
    arrets: list[str] = field(default_factory=list)       # tous les arrêts traversés
    extremites: list[str] = field(default_factory=list)   # où l'on monte et descend
    montees: list[tuple[str, datetime | None]] = field(default_factory=list)  # (arrêt, heure de départ)


@dataclass
class Etat:
    bus: list[Passage] = field(default_factory=list)
    rer: list[Passage] = field(default_factory=list)
    perturbations: list[str] = field(default_factory=list)  # gênantes pour le trajet
    infos_trafic: list[str] = field(default_factory=list)   # sur la ligne, sans gêner le trajet
    trafic_connu: bool = False
    a_l_arret: datetime | None = None   # heure visée à l'arrêt de bus
    meteo: object = None
    arrivee: datetime | None = None
    depart_trajet: datetime | None = None
    erreurs: list[str] = field(default_factory=list)


class PrimClient:
    def __init__(self, api_key: str, tz: tzinfo):
        self.api_key = api_key
        self.tz = tz

    # ------------------------------------------------------------------ HTTP
    def get(self, chemin: str, params: dict | None = None) -> dict:
        r = requests.get(f"{BASE}/{chemin}", params=params, timeout=TIMEOUT,
                         headers={"apikey": self.api_key, "Accept": "application/json"})
        if r.status_code in (401, 403):
            raise RuntimeError("clé API PRIM refusée")
        if r.status_code == 429:
            raise RuntimeError("quota PRIM atteint")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ arrêts
    def chercher_arret(self, texte: str) -> tuple[str, str]:
        """Renvoie (id Navitia, nom) du premier arrêt correspondant à `texte`."""
        data = self.get("v2/navitia/places", {"q": texte, "type[]": "stop_area", "count": 5})
        lieux = [p for p in data.get("places") or [] if p.get("embedded_type") in (None, "stop_area")]
        if not lieux:
            raise RuntimeError(f"arrêt introuvable : {texte}")
        return lieux[0]["id"], lieux[0].get("name", texte)

    def coordonnees(self, stop_area_id: str) -> tuple[float, float]:
        data = self.get(f"v2/navitia/stop_areas/{stop_area_id}")
        coord = (data.get("stop_areas") or [{}])[0].get("coord") or {}
        return float(coord["lat"]), float(coord["lon"])

    @staticmethod
    def vers_siri(navitia_id: str) -> str:
        """stop_area:IDFM:71517 -> STIF:StopArea:SP:71517:"""
        if navitia_id.startswith("STIF:"):
            return navitia_id
        return f"STIF:StopArea:SP:{navitia_id.split(':')[-1]}:"

    @staticmethod
    def vers_navitia(ref: str) -> str:
        """STIF:StopArea:SP:71517: -> stop_area:IDFM:71517"""
        if ref.startswith("stop_area:"):
            return ref
        code = [p for p in ref.split(":") if p][-1]
        return f"stop_area:IDFM:{code}"

    # ------------------------------------------------------------------ passages
    def passages(self, monitoring_ref: str, line_ref: str | None = None) -> list[Passage]:
        params = {"MonitoringRef": monitoring_ref}
        if line_ref:
            params["LineRef"] = line_ref
        return self.extraire_passages(self.get("stop-monitoring", params))

    def departs(self, stop_area_id: str, a_partir_de: datetime, nombre: int = 40) -> list[Passage]:
        """Départs du calculateur Navitia à partir d'une heure donnée : temps réel quand il
        est connu, sinon horaire théorique. Sert au-delà de l'horizon de stop-monitoring."""
        data = self.get(f"v2/navitia/stop_areas/{stop_area_id}/departures", {
            "from_datetime": a_partir_de.strftime("%Y%m%dT%H%M%S"),
            "data_freshness": "realtime", "count": nombre,
        })
        res = []
        for d in data.get("departures") or []:
            info = d.get("display_informations") or {}
            moment = d.get("stop_date_time") or {}
            ligne_id = ((d.get("route") or {}).get("line") or {}).get("id", "")
            res.append(Passage(
                ligne=str(info.get("code") or code_ligne(ligne_id)),
                destination=str(info.get("direction") or "?"),
                heure=self._date_navitia(moment.get("departure_date_time")),
                ligne_ref=ligne_id,
                prevu=moment.get("data_freshness") != "realtime",
            ))
        return sorted((p for p in res if p.heure), key=lambda p: p.heure)

    def heure(self, s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(self.tz)
        except ValueError:
            return None

    def extraire_passages(self, data: dict) -> list[Passage]:
        res = []
        livraisons = data.get("Siri", {}).get("ServiceDelivery", {}).get("StopMonitoringDelivery", [])
        for liv in livraisons:
            for visite in liv.get("MonitoredStopVisit") or []:
                vj = visite.get("MonitoredVehicleJourney", {})
                call = vj.get("MonitoredCall", {})
                h = (call.get("ExpectedDepartureTime") or call.get("ExpectedArrivalTime")
                     or call.get("AimedDepartureTime") or call.get("AimedArrivalTime"))
                ligne_ref = str(val(vj.get("LineRef")) or "")
                res.append(Passage(
                    ligne=str(val(vj.get("PublishedLineName")) or ligne_ref or "?"),
                    ligne_ref=ligne_ref,
                    destination=str(val(call.get("DestinationDisplay"))
                                    or val(vj.get("DestinationName")) or "?"),
                    heure=self.heure(h),
                    statut=str(call.get("DepartureStatus") or call.get("ArrivalStatus") or ""),
                ))
        limite = datetime.now(self.tz) - timedelta(minutes=1)
        res = [p for p in res if p.heure and p.heure >= limite]
        return sorted(res, key=lambda p: p.heure)

    # ------------------------------------------------------------------ lignes
    def lignes_arret(self, stop_area_id: str) -> dict[str, str]:
        """Lignes desservant un arrêt : {"106": "C01135", ...}."""
        data = self.get(f"v2/navitia/stop_areas/{stop_area_id}/lines")
        return {str(l.get("code")): code_ligne(l.get("id", ""))
                for l in data.get("lines") or [] if l.get("code") and l.get("id")}

    # ------------------------------------------------------------------ info trafic
    def perturbations(self, line_ref: str, jusqua: datetime | None = None) -> list[Perturbation]:
        """Perturbations de la ligne qui s'appliquent entre maintenant et `jusqua` (+90 min par défaut)."""
        data = self.get(f"v2/navitia/lines/line:IDFM:{code_ligne(line_ref)}/line_reports")
        return self.extraire_perturbations(data, jusqua)

    def extraire_perturbations(self, data: dict, jusqua: datetime | None = None) -> list[Perturbation]:
        """Garde les perturbations qui touchent la ligne entière ou un tronçon (pas les
        ascenseurs, rattachés à une gare) et dont une période d'application recoupe
        [maintenant, jusqua]. Le champ severity.effect n'est pas fiable : les pannes
        d'ascenseur y sont en SIGNIFICANT_DELAYS. Une gare non desservie est un tronçon
        d'une seule gare (début = fin)."""
        debut = datetime.now(self.tz)
        fin = jusqua or debut + timedelta(minutes=90)
        out = {}
        for d in data.get("disruptions") or []:
            if d.get("status") == "past":
                continue
            sections, ligne_entiere = [], False
            for objet in d.get("impacted_objects") or []:
                section = objet.get("impacted_section")
                if section:
                    sections.append(((section.get("from") or {}).get("name", ""),
                                     (section.get("to") or {}).get("name", "")))
                elif (objet.get("pt_object") or {}).get("embedded_type") == "line":
                    ligne_entiere = True
            if not (sections or ligne_entiere):
                continue
            if not any(self._recoupe(p, debut, fin) for p in d.get("application_periods") or []):
                continue
            texte = self._message(d)
            if texte and texte not in out:
                out[texte] = Perturbation(texte, sections, ligne_entiere)
        return list(out.values())

    def _recoupe(self, periode: dict, debut: datetime, fin: datetime) -> bool:
        p_debut, p_fin = self._date_navitia(periode.get("begin")), self._date_navitia(periode.get("end"))
        if not p_debut or not p_fin:
            return False
        return p_debut < fin and p_fin > debut

    @staticmethod
    def _message(perturbation: dict) -> str:
        messages = perturbation.get("messages") or []
        # Texte brut de préférence, sinon on retire les balises HTML
        messages.sort(key=lambda m: (m.get("channel") or {}).get("content_type") != "text/plain")
        for m in messages:
            texte = html.unescape(re.sub(r"<[^>]+>", " ", m.get("text") or ""))
            texte = " ".join(texte.split())
            if texte:
                return texte
        return ""

    def _date_navitia(self, s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            naive = datetime.strptime(s, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        # pytz demande localize() : replace(tzinfo=…) donnerait un décalage LMT erroné
        if hasattr(self.tz, "localize"):
            return self.tz.localize(naive)
        return naive.replace(tzinfo=self.tz)

    # ------------------------------------------------------------------ itinéraire
    def itineraire(self, depart: str, arrivee: str, quand: datetime | None = None) -> Trajet | None:
        """Premier trajet partant de `depart` à partir de `quand` (maintenant par défaut)."""
        data = self.get("v2/navitia/journeys", {
            "from": depart, "to": arrivee,
            "datetime": (quand or datetime.now(self.tz)).strftime("%Y%m%dT%H%M%S"),
            "data_freshness": "realtime", "count": 1,
        })
        trajets = data.get("journeys") or []
        if not trajets:
            return None
        trajet = Trajet(self._date_navitia(trajets[0].get("departure_date_time")),
                        self._date_navitia(trajets[0].get("arrival_date_time")))
        for section in trajets[0].get("sections") or []:
            if section.get("type") != "public_transport":
                continue
            trajet.extremites += [(section.get("from") or {}).get("name", ""), (section.get("to") or {}).get("name", "")]
            trajet.montees.append(((section.get("from") or {}).get("name", ""),
                                   self._date_navitia(section.get("departure_date_time"))))
            trajet.arrets += [(t.get("stop_point") or {}).get("name", "") for t in section.get("stop_date_times") or []]
        return trajet


def nom_gare(nom: str) -> str:
    """« Nanterre - Préfecture (Nanterre) » et « Nanterre Préfecture » -> « nanterreprefecture »"""
    sans_ville = re.sub(r"\s*\([^)]*\)\s*$", "", nom or "")
    return re.sub(r"[^a-z0-9]", "", norm(sans_ville))


def classer(perturbations: list[Perturbation], arrets: list[str],
            extremites: list[str]) -> tuple[list[str], list[str]]:
    """Sépare les perturbations qui gênent le trajet de celles données pour info.

    Gênante : toute la ligne, une gare fermée où l'on monte ou descend, ou un tronçon de
    plusieurs gares qui touche le trajet. Une gare fermée qu'on ne fait que traverser
    n'est qu'une info. Sans trajet connu (`arrets` vide), tout tronçon de plusieurs gares
    est considéré comme gênant."""
    arrets_n = {nom_gare(a) for a in arrets}
    extremites_n = {nom_gare(e) for e in extremites}
    genantes, infos = [], []
    for p in perturbations:
        genante = p.ligne_entiere
        for debut, fin in p.sections:
            debut_n, fin_n = nom_gare(debut), nom_gare(fin)
            if debut_n == fin_n:
                genante = genante or debut_n in extremites_n
            else:
                genante = genante or not arrets_n or debut_n in arrets_n or fin_n in arrets_n
        (genantes if genante else infos).append(p.texte)
    return genantes, infos


def filtrer(passages: list[Passage], lignes: list[str], contient: str,
            exclure: list[str], nombre: int) -> list[Passage]:
    """`lignes` accepte des noms (« 106 ») ou des codes (« C01135 »)."""
    lignes_n = {norm(l) for l in lignes}
    contient_n = norm(contient)
    exclure_n = [norm(x) for x in exclure]
    out = [p for p in passages
           if (not lignes_n or norm(p.ligne) in lignes_n or norm(code_ligne(p.ligne_ref)) in lignes_n)
           and (not contient_n or contient_n in norm(p.destination))
           and not any(x in norm(p.destination) for x in exclure_n)]
    return out[:nombre]
