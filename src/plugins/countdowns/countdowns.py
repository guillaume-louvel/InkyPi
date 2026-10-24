"""Plusieurs comptes à rebours (échéances, anniversaires…) affichés en cartes."""
import logging
from dataclasses import dataclass
from datetime import date, datetime

import pytz

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

MAX_EVENEMENTS = 50
MAX_CARTES = 8
CARTES_PAR_DEFAUT = 6

UNE_FOIS, CHAQUE_ANNEE, ANNIVERSAIRE = "once", "yearly", "birthday"

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

# Colonnes × lignes selon le nombre de cartes
GRILLES = {1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2), 7: (4, 2), 8: (4, 2)}


@dataclass
class Evenement:
    nom: str
    date: date
    repetition: str = UNE_FOIS

    def prochaine(self, aujourdhui: date) -> date | None:
        """Prochaine occurrence à partir d'aujourd'hui (inclus), None si elle est passée."""
        if self.repetition == UNE_FOIS:
            return self.date if self.date >= aujourdhui else None
        for annee in (aujourdhui.year, aujourdhui.year + 1):
            occurrence = meme_jour(self.date, annee)
            if occurrence >= aujourdhui:
                return occurrence
        return None


def meme_jour(d: date, annee: int) -> date:
    """Le même jour une autre année ; le 29 février devient le 28 les années non bissextiles."""
    try:
        return d.replace(year=annee)
    except ValueError:
        return date(annee, 2, 28)


def lire_evenements(settings: dict) -> list[Evenement]:
    """Événements saisis dans le formulaire (listes parallèles de noms, dates et répétitions)."""
    noms = settings.get("eventNames[]") or []
    dates = settings.get("eventDates[]") or []
    repetitions = settings.get("eventRepeats[]") or []
    evenements = []
    for i, (nom, texte) in enumerate(zip(noms, dates)):
        nom = (nom or "").strip()
        if not nom or not texte:
            continue
        try:
            jour = date.fromisoformat(texte)
        except ValueError:
            logger.warning(f"Date invalide ignorée | événement: {nom} | date: {texte}")
            continue
        repetition = repetitions[i] if i < len(repetitions) else UNE_FOIS
        evenements.append(Evenement(nom, jour, repetition if repetition in (CHAQUE_ANNEE, ANNIVERSAIRE) else UNE_FOIS))
    return evenements[:MAX_EVENEMENTS]


def cartes(evenements: list[Evenement], aujourdhui: date, nombre: int) -> list[dict]:
    """Les `nombre` prochains événements, du plus proche au plus lointain."""
    a_venir = sorted(((e.prochaine(aujourdhui), e) for e in evenements if e.prochaine(aujourdhui)),
                     key=lambda paire: (paire[0], paire[1].nom.lower()))
    res = []
    for jour, e in a_venir[:nombre]:
        jours = (jour - aujourdhui).days
        detail = f"{'1er' if jour.day == 1 else jour.day} {MOIS[jour.month - 1]}"
        if e.repetition == UNE_FOIS and jour.year != aujourdhui.year:
            detail += f" {jour.year}"
        if e.repetition == ANNIVERSAIRE and jour.year > e.date.year:
            detail += f" · {jour.year - e.date.year} ans"
        res.append({
            "nom": e.nom,
            "nombre": "Aujourd'hui" if jours == 0 else str(jours),
            "unite": "" if jours == 0 else ("jour" if jours == 1 else "jours"),
            "detail": detail,
        })
    return res


def tailles(dimensions, nb_cartes: int, avec_titre: bool) -> dict:
    """Tailles de police (px) des cartes, d'après la place disponible pour chacune."""
    largeur, hauteur = dimensions
    colonnes, lignes = GRILLES.get(nb_cartes, (1, 1))
    if hauteur > largeur:  # écran en portrait : plus de lignes que de colonnes
        colonnes, lignes = lignes, colonnes
    marge = 0.06 * min(largeur, hauteur)  # marges et espaces entre cartes, approximativement
    titre = 0.1 * hauteur if avec_titre else 0
    carte_l = (largeur - marge * (colonnes + 1)) / colonnes
    carte_h = (hauteur - titre - marge * (lignes + 1)) / lignes
    return {
        "colonnes": colonnes,
        "lignes": lignes,
        "chiffres": carte_h * 0.42,       # plafond ; réduit pour les nombres longs (voir taille_nombre)
        "largeur_carte": carte_l,
        "nom": min(carte_h * 0.1, carte_l * 0.085),
        "titre": hauteur * 0.065,
    }


def taille_nombre(texte: str, t: dict) -> int:
    """Le nombre le plus grand possible qui tient dans la largeur de la carte (≈ 0,58 em par caractère)."""
    return int(min(t["chiffres"], t["largeur_carte"] * 0.86 / (0.58 * max(len(texte), 1))))


class Countdowns(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["max_cards"] = MAX_CARTES
        template_params["max_events"] = MAX_EVENEMENTS
        template_params["default_cards"] = CARTES_PAR_DEFAUT
        return template_params

    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        tz = pytz.timezone(device_config.get_config("timezone", default="Europe/Paris"))
        aujourdhui = datetime.now(tz).date()

        evenements = lire_evenements(settings)
        if not evenements:
            raise RuntimeError("Ajoute au moins un événement avec un nom et une date.")
        try:
            nombre = min(max(int(settings.get("maxCards") or CARTES_PAR_DEFAUT), 1), MAX_CARTES)
        except ValueError:
            nombre = CARTES_PAR_DEFAUT

        a_afficher = cartes(evenements, aujourdhui, nombre)
        titre = (settings.get("title") or "").strip()
        t = tailles(dimensions, max(len(a_afficher), 1), bool(titre))
        # Même taille pour tous les nombres (celle qui fait tenir le plus long), à part pour « Aujourd'hui »
        nombres = [c["nombre"] for c in a_afficher if c["unite"]]
        taille_commune = min((taille_nombre(n, t) for n in nombres), default=0)
        for carte in a_afficher:
            carte["taille"] = taille_commune if carte["unite"] else taille_nombre(carte["nombre"], t)

        image = self.render_image(dimensions, "countdowns.html", "countdowns.css", {
            "titre": titre,
            "cartes": a_afficher,
            "t": t,
            "plugin_settings": settings,
        })
        if image is None:
            raise RuntimeError("Le rendu de l'image a échoué.")
        return image
