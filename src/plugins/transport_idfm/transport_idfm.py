import logging
from datetime import datetime, timedelta

import pytz

from plugins.base_plugin.base_plugin import BasePlugin
from plugins.transport_idfm import meteo
from plugins.transport_idfm.idfm_api import (Etat, Passage, PrimClient, classer, code_ligne, filtrer,
                                             liste, norm)

logger = logging.getLogger(__name__)

ENV_KEY = "PRIM_API_KEY"
JOURS = ["Lun.", "Mar.", "Mer.", "Jeu.", "Ven.", "Sam.", "Dim."]

DEFAUTS = {
    "title": "Départ travail",
    "busStop": "",
    "busLines": "106, 108, 110",
    "busDestination": "Joinville",
    "busCount": "4",
    "rerStop": "Joinville-le-Pont",
    "rerLineRef": "STIF:Line::C01742:",
    "rerLabel": "A",
    "rerExclude": "Boissy, Sucy, Varenne, Saint-Maur, Champigny",
    "rerCount": "3",
    "destinationStop": "Nanterre Préfecture",
    "atStop": "08:30",
    "busToTrain": "10",
    "bikeFrom": "08:15",
    "bikeTo": "09:00",
}

MARGE_BUS = timedelta(minutes=3)       # bus montrés un peu avant l'heure visée à l'arrêt
DOUBLON = timedelta(minutes=3)          # même ligne à moins de 3 min : même bus


def hhmm(d):
    return d.strftime("%Hh%M") if d else "--h--"


class TransportIdfm(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["api_key"] = {
            "required": True,
            "service": "Île-de-France Mobilités (PRIM)",
            "expected_key": ENV_KEY,
        }
        template_params["defaults"] = DEFAUTS
        return template_params

    # ------------------------------------------------------------------ helpers
    def _reglage(self, settings, cle):
        v = settings.get(cle)
        return v if v not in (None, "") else DEFAUTS.get(cle, "")

    def _heure(self, settings, cle, maintenant):
        """Réglage « HH:MM » -> datetime du jour (None si invalide)."""
        try:
            heures, minutes = map(int, self._reglage(settings, cle).split(":"))
            return maintenant.replace(hour=heures, minute=minutes, second=0, microsecond=0)
        except ValueError:
            return None

    def _a_l_arret(self, settings, maintenant):
        """Heure à laquelle on sera à l'arrêt de bus, ou maintenant si elle est passée."""
        cible = self._heure(settings, "atStop", maintenant)
        return max(cible, maintenant) if cible else maintenant

    def _creneau_velo(self, settings, maintenant):
        debut = self._heure(settings, "bikeFrom", maintenant)
        fin = self._heure(settings, "bikeTo", maintenant)
        if not debut or not fin or fin <= maintenant or fin <= debut:
            return maintenant, maintenant + timedelta(minutes=45)
        return max(debut, maintenant), fin

    def _coordonnees(self, client, settings, stop_id):
        """Coordonnées de l'arrêt de bus pour la météo, mises en cache dans les réglages."""
        cache = settings.get("_meteoCoord") or {}
        if cache.get("arret") != stop_id:
            lat, lon = client.coordonnees(stop_id)
            cache = {"arret": stop_id, "lat": lat, "lon": lon}
            settings["_meteoCoord"] = cache
        return cache["lat"], cache["lon"]

    @staticmethod
    def _prochains(client, stop_id, temps_reel, lignes, contient, exclure, nombre, a_partir_de):
        """Passages à partir d'une heure : le temps réel d'abord, complété par les départs du
        calculateur quand l'heure dépasse l'horizon du temps réel (ex. bus de 8h30 vus à 7h30)."""
        res = [p for p in filtrer(temps_reel, lignes, contient, exclure, 99) if p.heure >= a_partir_de]
        if len(res) < nombre:
            try:
                prevus = filtrer(client.departs(stop_id, a_partir_de), lignes, contient, exclure, 99)
            except Exception as ex:  # noqa: BLE001 - on garde le temps réel
                logger.warning(f"Départs théoriques indisponibles : {ex}")
                prevus = []
            for p in prevus:
                deja = any(code_ligne(q.ligne_ref) == code_ligne(p.ligne_ref) and abs(q.heure - p.heure) <= DOUBLON
                           for q in res)
                if not deja:
                    res.append(p)
        return sorted(res, key=lambda p: p.heure)[:nombre]

    def _resoudre(self, client, settings, cle):
        """Accepte un identifiant (STIF:… ou stop_area:…) ou un nom d'arrêt.
        Un nom est résolu une fois via Navitia puis mis en cache dans les réglages."""
        saisie = self._reglage(settings, cle).strip()
        if not saisie:
            raise RuntimeError(f"Réglage manquant : {cle}")
        if saisie.startswith(("STIF:", "stop_area:")):
            return client.vers_navitia(saisie), saisie
        cache = settings.get(f"_{cle}Cache") or {}
        if cache.get("saisie") != saisie:
            nid, nom = client.chercher_arret(saisie)
            cache = {"saisie": saisie, "id": nid, "nom": nom}
            settings[f"_{cle}Cache"] = cache
            logger.info(f"Arrêt '{saisie}' résolu en {nom} ({nid})")
        return cache["id"], cache["nom"]

    def _lignes(self, client, settings, stop_id):
        """Noms des lignes d'un arrêt par code ({"C01135": "106"}), mis en cache dans les réglages.
        Les passages temps réel ne donnent que le code : il faut cette table pour filtrer et afficher."""
        cache = settings.get("_busLinesCache") or {}
        if cache.get("arret") != stop_id:
            try:
                par_nom = client.lignes_arret(stop_id)
            except Exception as ex:  # noqa: BLE001 - on retombe sur le filtre par nom
                logger.warning(f"Lignes de l'arrêt {stop_id} indisponibles : {ex}")
                return {}
            cache = {"arret": stop_id, "lignes": {code: nom for nom, code in par_nom.items()}}
            settings["_busLinesCache"] = cache
        return cache["lignes"]

    # ------------------------------------------------------------------ données
    def _collecter(self, client, s):
        e = Etat()
        noms = {}
        maintenant = datetime.now(client.tz)
        e.a_l_arret = self._a_l_arret(s, maintenant)
        ids = {}
        for cle, nom in (("busStop", "bus"), ("rerStop", "rer"), ("destinationStop", "dest")):
            try:
                ids[nom], noms[nom] = self._resoudre(client, s, cle)
            except Exception as ex:  # noqa: BLE001 - affiché à l'écran
                logger.exception(f"Arrêt {cle}")
                e.erreurs.append(f"Arrêt : {ex}")
        bus_id, rer_id, dest_id = ids.get("bus"), ids.get("rer"), ids.get("dest")

        # Itinéraire pour l'heure où l'on est à l'arrêt : arrivée estimée et train à prendre
        trajet = None
        if bus_id and dest_id:
            try:
                trajet = client.itineraire(bus_id, dest_id, e.a_l_arret)
                if trajet:
                    e.depart_trajet, e.arrivee = trajet.depart, trajet.arrivee
            except Exception as ex:  # noqa: BLE001
                logger.exception("Itinéraire")
                e.erreurs.append(f"Itinéraire : {ex}")

        if bus_id:
            try:
                noms_lignes = self._lignes(client, s, bus_id)
                demandees = liste(self._reglage(s, "busLines"))
                demandees_n = {norm(d) for d in demandees}
                codes = [code for code, nom in noms_lignes.items() if norm(nom) in demandees_n]
                e.bus = self._prochains(client, bus_id, client.passages(client.vers_siri(bus_id)),
                                        demandees + codes, self._reglage(s, "busDestination"), [],
                                        int(self._reglage(s, "busCount")),
                                        max(e.a_l_arret - MARGE_BUS, maintenant))
                for p in e.bus:
                    p.ligne = noms_lignes.get(code_ligne(p.ligne_ref), p.ligne)
            except Exception as ex:  # noqa: BLE001
                logger.exception("Bus")
                e.erreurs.append(f"Bus : {ex}")

        if rer_id:
            try:
                rer_ref = self._reglage(s, "rerLineRef")
                # Les trains qu'on peut attraper : premier bus pris à l'arrêt + trajet jusqu'au quai
                bus_pris = next((p.heure for p in e.bus if not p.annule and p.heure >= e.a_l_arret), e.a_l_arret)
                a_partir_de = bus_pris + timedelta(minutes=int(self._reglage(s, "busToTrain")))
                e.rer = self._prochains(client, rer_id, client.passages(client.vers_siri(rer_id), rer_ref),
                                        [code_ligne(rer_ref)], "", liste(self._reglage(s, "rerExclude")),
                                        int(self._reglage(s, "rerCount")), max(a_partir_de, maintenant))
            except Exception as ex:  # noqa: BLE001
                logger.exception("RER")
                e.erreurs.append(f"RER : {ex}")

        try:
            perturbations = client.perturbations(self._reglage(s, "rerLineRef"),
                                                 e.a_l_arret + timedelta(minutes=90))
            e.trafic_connu = True
            # Sans itinéraire, on ne connaît que la gare RER de départ et la destination
            arrets = trajet.arrets if trajet else []
            extremites = trajet.extremites if trajet else [
                noms.get("rer", self._reglage(s, "rerStop")), noms.get("dest", self._reglage(s, "destinationStop"))]
            e.perturbations, e.infos_trafic = classer(perturbations, arrets, extremites)
        except Exception as ex:  # noqa: BLE001
            logger.exception("Info trafic")
            e.erreurs.append(f"Info trafic : {ex}")

        if bus_id and s.get("hideWeather") != "true":
            try:
                lat, lon = self._coordonnees(client, s, bus_id)
                debut, fin = self._creneau_velo(s, maintenant)
                e.meteo = meteo.prevision(lat, lon, client.tz, debut, fin)
            except Exception as ex:  # noqa: BLE001
                logger.exception("Météo")
                e.erreurs.append(f"Météo : {ex}")
        return e, noms

    def _demo(self, maintenant):
        n = maintenant.replace(second=0, microsecond=0)
        m = lambda x: n + timedelta(minutes=x)  # noqa: E731
        e = Etat(
            bus=[Passage("108", "Joinville-le-Pont RER", m(3)),
                 Passage("106", "Joinville-le-Pont RER", m(7)),
                 Passage("110", "Joinville-le-Pont RER", m(9), statut="cancelled"),
                 Passage("108", "Joinville-le-Pont RER", m(15))],
            rer=[Passage("A", "Saint-Germain-en-Laye", m(14)),
                 Passage("A", "Cergy-le-Haut", m(19)),
                 Passage("A", "Poissy", m(24))],
            perturbations=["Trafic perturbé entre Vincennes et Nation en raison d'un incident "
                           "technique. Retards de 10 à 15 min à prévoir."],
            depart_trajet=m(3), arrivee=m(52),
        )
        e.bus[3].prevu = True
        e.trafic_connu = True
        e.a_l_arret = m(3)
        e.meteo = meteo.Meteo(temperature=7.4, ressenti=4.1, code=3, jour=True, debut=m(0), fin=m(45),
                              t_min=6.8, t_max=8.2, ressenti_min=3.6, pluie_mm=1.4, pluie_proba=80,
                              pluie_debut=m(20), vent=18, rafales=42, code_trajet=61,
                              alertes=[f"Pluie dès {meteo.h(m(20))}"])
        return e, {"bus": "Mon arrêt (démo)", "rer": "Joinville-le-Pont", "dest": "Nanterre-Préfecture"}

    # ------------------------------------------------------------------ rendu
    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/Paris"))
        maintenant = datetime.now(tz)

        if settings.get("demoMode") == "true":
            etat, noms = self._demo(maintenant)
        else:
            api_key = device_config.load_env_key(ENV_KEY)
            if not api_key:
                raise RuntimeError(f"Clé API manquante : ajoute {ENV_KEY}=... dans le fichier .env d'InkyPi.")
            etat, noms = self._collecter(PrimClient(api_key, tz), settings)
            if not (etat.bus or etat.rer or etat.arrivee) and etat.erreurs:
                # Rien d'exploitable : on remonte l'erreur à l'interface web
                raise RuntimeError(" | ".join(etat.erreurs))

        compte_a_rebours = settings.get("showCountdown") == "true"

        def bus_vue(p):
            mins = int((p.heure - maintenant).total_seconds() // 60) if p.heure else None
            return {
                "ligne": p.ligne, "heure": hhmm(p.heure), "annule": p.annule, "prevu": p.prevu,
                "dans": (("à l'approche" if mins <= 0 else f"{mins} min")
                         if compte_a_rebours and mins is not None else ""),
            }

        duree = None
        if etat.depart_trajet and etat.arrivee:
            duree = int((etat.arrivee - etat.depart_trajet).total_seconds() // 60)

        params = {
            "title": self._reglage(settings, "title"),
            "date": f"{JOURS[maintenant.weekday()]} {maintenant:%d/%m}",
            "maj": hhmm(maintenant),
            "bus": [bus_vue(p) for p in etat.bus],
            "bus_destination": self._reglage(settings, "busDestination"),
            "a_l_arret": (hhmm(etat.a_l_arret)
                          if etat.a_l_arret and etat.a_l_arret > maintenant + timedelta(minutes=5) else ""),
            "meteo": etat.meteo.vue() if etat.meteo else None,
            "rer_label": self._reglage(settings, "rerLabel"),
            "rer_titre": f"→ {noms.get('dest', '')}" if noms.get("dest") else noms.get("rer", ""),
            "rer": [{"heure": hhmm(p.heure), "destination": p.destination, "annule": p.annule, "prevu": p.prevu}
                    for p in etat.rer],
            "perturbation": etat.perturbations[0] if etat.perturbations else "",
            "info_trafic": etat.infos_trafic[0] if etat.infos_trafic else "",
            "trafic_connu": getattr(etat, "trafic_connu", False),
            "arrivee": hhmm(etat.arrivee) if etat.arrivee else "",
            "depart": hhmm(etat.depart_trajet) if etat.depart_trajet else "",
            "duree": duree,
            "dest_nom": noms.get("dest", self._reglage(settings, "destinationStop")),
            "arret_bus": noms.get("bus", ""),
            "erreurs": etat.erreurs,
            "plugin_settings": settings,
        }
        image = self.render_image(dimensions, "transport_idfm.html", "transport_idfm.css", params)
        if image is None:
            raise RuntimeError("Le rendu de l'image a échoué.")
        return image
