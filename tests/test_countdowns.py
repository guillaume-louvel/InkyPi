from datetime import date

import pytest

from plugins.countdowns.countdowns import (ANNIVERSAIRE, CHAQUE_ANNEE, UNE_FOIS, Evenement, cartes, lire_evenements,
                                           tailles)

TODAY = date(2026, 10, 24)


class TestNextOccurrence:

    @pytest.mark.parametrize("event,expected", [
        (Evenement("Rendu", date(2026, 11, 14)), date(2026, 11, 14)),
        (Evenement("Rendu passé", date(2026, 10, 23)), None),           # one-off events disappear afterwards
        (Evenement("Aujourd'hui", date(2026, 10, 24)), date(2026, 10, 24)),
        (Evenement("Noël", date(2000, 12, 25), CHAQUE_ANNEE), date(2026, 12, 25)),
        (Evenement("Léa", date(2018, 3, 15), ANNIVERSAIRE), date(2027, 3, 15)),   # already passed this year
        (Evenement("Bissextile", date(2008, 2, 29), ANNIVERSAIRE), date(2027, 2, 28)),
    ])
    def test_prochaine(self, event, expected):
        assert event.prochaine(TODAY) == expected

    def test_leap_year_birthday_on_the_29th(self):
        assert Evenement("Bissextile", date(2008, 2, 29), ANNIVERSAIRE).prochaine(date(2028, 1, 1)) == date(2028, 2, 29)


class TestCards:

    def test_sorted_limited_and_labelled(self):
        events = [
            Evenement("Noël", date(2000, 12, 25), CHAQUE_ANNEE),
            Evenement("Léa", date(2018, 10, 24), ANNIVERSAIRE),
            Evenement("Rentrée", date(2027, 9, 1)),
            Evenement("Tom", date(2016, 10, 25), ANNIVERSAIRE),
            Evenement("Passé", date(2026, 1, 1)),
        ]
        result = cartes(events, TODAY, 3)
        assert [(c["nom"], c["nombre"], c["unite"], c["detail"]) for c in result] == [
            ("Léa", "Aujourd'hui", "", "24 octobre · 8 ans"),
            ("Tom", "1", "jour", "25 octobre · 10 ans"),
            ("Noël", "62", "jours", "25 décembre"),   # no age for a plain yearly event
        ]
        assert cartes(events, TODAY, 8)[-1]["detail"] == "1er septembre 2027"   # year shown when not this year

    def test_reads_the_form_lists(self):
        settings = {
            "eventNames[]": ["Léa", "", "Mauvaise date", "Rendu"],
            "eventDates[]": ["2018-10-24", "2026-12-01", "pas une date", "2026-11-14"],
            "eventRepeats[]": ["birthday", "once", "once", "weird"],
        }
        assert lire_evenements(settings) == [
            Evenement("Léa", date(2018, 10, 24), ANNIVERSAIRE),
            Evenement("Rendu", date(2026, 11, 14), UNE_FOIS),
        ]


class TestLayout:

    @pytest.mark.parametrize("cards,landscape,portrait", [
        (1, (1, 1), (1, 1)), (3, (3, 1), (1, 3)), (6, (3, 2), (2, 3)), (8, (4, 2), (2, 4)),
    ])
    def test_grid_follows_orientation(self, cards, landscape, portrait):
        t = tailles((800, 480), cards, False)
        assert (t["colonnes"], t["lignes"]) == landscape
        t = tailles((480, 800), cards, False)
        assert (t["colonnes"], t["lignes"]) == portrait
