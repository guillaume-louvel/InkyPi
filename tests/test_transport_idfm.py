"""Tests of the IDFM transport plugin, using response shapes captured from the real PRIM APIs."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from plugins.transport_idfm import meteo
from plugins.transport_idfm.idfm_api import Passage, Perturbation, PrimClient, classer, code_ligne, filtrer
from plugins.transport_idfm.transport_idfm import TransportIdfm

PARIS = ZoneInfo("Europe/Paris")


def siri_visit(line_ref, destination, when):
    return {"MonitoredVehicleJourney": {
        "LineRef": {"value": line_ref},
        "MonitoredCall": {"DestinationDisplay": [{"value": destination}],
                          "ExpectedDepartureTime": when.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")},
    }}


def navitia(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def disruption(message, impacted, begin, end, status="active"):
    return {
        "status": status,
        "severity": {"effect": "SIGNIFICANT_DELAYS"},
        "impacted_objects": [impacted],
        "application_periods": [{"begin": navitia(begin), "end": navitia(end)}],
        "messages": [{"text": f"<p>{message}</p>", "channel": {"content_type": "text/html"}}],
    }


LINE = {"pt_object": {"embedded_type": "line", "id": "line:IDFM:C01742"}}
SECTION = {"pt_object": {"embedded_type": "line", "id": "line:IDFM:C01742"},
           "impacted_section": {"from": {"id": "stop_area:IDFM:70640"}, "to": {"id": "stop_area:IDFM:71517"}}}
STATION = {"pt_object": {"embedded_type": "stop_area", "id": "stop_area:IDFM:71651"}}


class TestHelpers:

    @pytest.mark.parametrize("ref,code", [
        ("STIF:Line::C01742:", "C01742"), ("line:IDFM:C01135", "C01135"), ("", ""),
    ])
    def test_code_ligne(self, ref, code):
        assert code_ligne(ref) == code

    def test_filter_matches_line_codes_when_published_name_is_missing(self):
        now = datetime.now(PARIS)
        passages = [
            Passage("STIF:Line::C01135:", "Joinville RER", now, ligne_ref="STIF:Line::C01135:"),
            Passage("STIF:Line::C01135:", "Villiers Gare", now, ligne_ref="STIF:Line::C01135:"),
            Passage("STIF:Line::C01399:", "Joinville RER", now, ligne_ref="STIF:Line::C01399:"),  # N33
        ]
        kept = filtrer(passages, ["106", "C01135"], "Joinville", [], 5)
        assert [(p.ligne_ref, p.destination) for p in kept] == [("STIF:Line::C01135:", "Joinville RER")]


class TestDisruptions:

    def test_keeps_only_line_disruptions_active_during_the_commute(self):
        client = PrimClient("key", PARIS)
        now = datetime.now(PARIS).replace(tzinfo=None)
        data = {"disruptions": [
            disruption("Panne d'un ascenseur", STATION, now - timedelta(days=30), now + timedelta(days=300)),
            disruption("Incident technique, retards", LINE, now - timedelta(minutes=20), now + timedelta(hours=1)),
            disruption("Travaux entre Joinville et Nation", SECTION, now + timedelta(minutes=30), now + timedelta(hours=3)),
            disruption("Travaux de nuit", SECTION, now + timedelta(hours=10), now + timedelta(hours=16)),
            disruption("Travaux en novembre", LINE, now + timedelta(days=15), now + timedelta(days=20), status="future"),
            disruption("Hier", LINE, now - timedelta(days=1), now - timedelta(hours=20), status="past"),
        ]}

        kept = client.extraire_perturbations(data)
        assert [p.texte for p in kept] == ["Incident technique, retards", "Travaux entre Joinville et Nation"]
        assert kept[0].ligne_entiere and kept[0].sections == []
        assert not kept[1].ligne_entiere and len(kept[1].sections) == 1

    def test_html_message_is_cleaned(self):
        d = {"messages": [{"text": "<p>P&#233;riode : en semaine<br>d&#232;s 21h50</p>",
                           "channel": {"content_type": "text/html"}}]}
        assert PrimClient._message(d) == "Période : en semaine dès 21h50"

    def test_prefers_plain_text_message(self):
        d = {"messages": [{"text": "<b>Long HTML</b>", "channel": {"content_type": "text/html"}},
                          {"text": "Court", "channel": {"content_type": "text/plain"}}]}
        assert PrimClient._message(d) == "Court"


# The route returned by the journey planner on 24/10 (CDG Étoile skipped that day)
ROUTE = ["Les Platanes", "Joinville-le-Pont", "Joinville-le-Pont", "Nogent-sur-Marne", "Fontenay-sous-Bois",
         "Vincennes", "Nation", "Gare de Lyon", "Châtelet - Les Halles", "Auber", "La Défense", "Nanterre - Préfecture"]
ENDS = ["Les Platanes (Joinville-le-Pont)", "Joinville-le-Pont (Joinville-le-Pont)",
        "Joinville-le-Pont (Joinville-le-Pont)", "Nanterre - Préfecture (Nanterre)"]


class TestClassification:

    @pytest.mark.parametrize("perturbation,blocking", [
        # Real cases from 24/10: a station closed that the train only passes through
        (Perturbation("CDG Étoile non desservie", [("Charles de Gaulle - Étoile (Paris)",) * 2]), False),
        (Perturbation("Châtelet non desservie", [("Châtelet - Les Halles (Paris)",) * 2]), False),
        # The station where you board or get off
        (Perturbation("Joinville non desservie", [("Joinville-le-Pont (Joinville-le-Pont)",) * 2]), True),
        (Perturbation("Nanterre fermée", [("Nanterre Préfecture",) * 2]), True),
        # Stretches of track: on another branch, or on the route
        (Perturbation("Travaux Boissy", [("La Varenne - Chennevières (Saint-Maur)", "Boissy-Saint-Léger (Boissy)")]), False),
        (Perturbation("Incident Vincennes - Nation", [("Vincennes (Vincennes)", "Nation (Paris)")]), True),
        # The whole line
        (Perturbation("Incident technique", [], ligne_entiere=True), True),
    ])
    def test_against_the_route(self, perturbation, blocking):
        genantes, infos = classer([perturbation], ROUTE, ENDS)
        assert (genantes, infos) == (([perturbation.texte], []) if blocking else ([], [perturbation.texte]))

    def test_any_stretch_blocks_when_route_is_unknown(self):
        p = Perturbation("Travaux Boissy", [("La Varenne - Chennevières", "Boissy-Saint-Léger")])
        assert classer([p], [], ["Joinville-le-Pont", "Nanterre Préfecture"]) == (["Travaux Boissy"], [])


MONDAY_0730 = datetime(2026, 10, 26, 7, 30, tzinfo=PARIS)


def at(hhmm):
    hours, minutes = map(int, hhmm.split(":"))
    return MONDAY_0730.replace(hour=hours, minute=minutes)


def departure(code, line_id, direction, hhmm, realtime=False):
    return {"display_informations": {"code": code, "direction": direction},
            "route": {"line": {"id": line_id}},
            "stop_date_time": {"departure_date_time": navitia(at(hhmm)),
                               "data_freshness": "realtime" if realtime else "base_schedule"}}


# Open-Meteo answer for Monday morning: rain from 8h30, strong gusts at 8h
OPEN_METEO = {
    "current": {"temperature_2m": 6.2, "apparent_temperature": 3.1, "weather_code": 3, "is_day": 1},
    "hourly": {
        "time": [f"2026-10-26T{h:02d}:00" for h in range(7, 11)],
        "temperature_2m": [5.8, 6.4, 7.5, 8.9],
        "apparent_temperature": [2.9, 3.4, 4.6, 6.0],
        "precipitation_probability": [10, 70, 80, 40],
        "weather_code": [3, 61, 63, 3],
        "wind_speed_10m": [15, 22, 25, 18],
        "wind_gusts_10m": [30, 55, 48, 35],
    },
    "minutely_15": {
        "time": [f"2026-10-26T{h:02d}:{m:02d}" for h in (8, 9) for m in (0, 15, 30, 45)],
        "precipitation": [0.0, 0.0, 0.3, 0.5, 0.2, 0.1, 0.0, 0.0],
    },
}


class FakeClient(PrimClient):
    """Answers like PRIM on Monday at 7h30: the live departures stop before 8h30, so the
    8h30 buses and the train after them come from the journey planner's timetable."""

    def __init__(self):
        super().__init__("key", PARIS)
        self.calls = []

    def get(self, chemin, params=None):
        self.calls.append((chemin, params))
        if chemin == "v2/navitia/places":
            ids = {"Les Platanes": ("stop_area:IDFM:70606", "Les Platanes (Joinville-le-Pont)"),
                   "Joinville-le-Pont": ("stop_area:IDFM:70640", "Joinville-le-Pont (Joinville-le-Pont)"),
                   "Nanterre Préfecture": ("stop_area:IDFM:71379", "Nanterre Préfecture (Nanterre)")}
            stop_id, name = ids[params["q"]]
            return {"places": [{"id": stop_id, "name": name, "embedded_type": "stop_area"}]}
        if chemin == "v2/navitia/stop_areas/stop_area:IDFM:70606":
            return {"stop_areas": [{"coord": {"lat": "48.8205", "lon": "2.4741"}}]}
        if chemin == "v2/navitia/stop_areas/stop_area:IDFM:70606/lines":
            return {"lines": [{"code": "106", "id": "line:IDFM:C01135"}, {"code": "108", "id": "line:IDFM:C01137"},
                              {"code": "110", "id": "line:IDFM:C01139"}, {"code": "N33", "id": "line:IDFM:C01399"}]}
        if chemin == "stop-monitoring" and params["MonitoringRef"] == "STIF:StopArea:SP:70606:":
            visits = [siri_visit("STIF:Line::C01137:", "Joinville RER", at("07:40")),
                      siri_visit("STIF:Line::C01135:", "Joinville RER", at("08:20")),
                      siri_visit("STIF:Line::C01139:", "Joinville RER", at("08:28")),
                      siri_visit("STIF:Line::C01135:", "Villiers Gare", at("08:30"))]
            return {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": visits}]}}}
        if chemin == "stop-monitoring":
            visits = [siri_visit("STIF:Line::C01742:", "Saint-Germain-en-Laye", at("07:35")),
                      siri_visit("STIF:Line::C01742:", "Boissy-Saint-Léger", at("07:38"))]
            return {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": visits}]}}}
        if chemin == "v2/navitia/stop_areas/stop_area:IDFM:70606/departures":
            assert params["from_datetime"] == "20261026T082700"
            return {"departures": [
                departure("110", "line:IDFM:C01139", "Joinville-le-Pont RER", "08:29"),  # = live 8h28
                departure("106", "line:IDFM:C01135", "Villiers Gare", "08:33"),
                departure("106", "line:IDFM:C01135", "Joinville-le-Pont RER", "08:36"),
                departure("108", "line:IDFM:C01137", "Joinville-le-Pont RER", "08:44"),
            ]}
        if chemin == "v2/navitia/stop_areas/stop_area:IDFM:70640/departures":
            assert params["from_datetime"] == "20261026T084600"  # first bus caught (8h36) + 10 min to the platform
            return {"departures": [
                departure("A", "line:IDFM:C01742", "Saint-Germain-en-Laye", "08:47", realtime=True),
                departure("A", "line:IDFM:C01742", "Boissy-Saint-Léger", "08:49"),
                departure("A", "line:IDFM:C01742", "Cergy-le-Haut", "08:52"),
                departure("A", "line:IDFM:C01742", "Poissy", "08:57"),
            ]}
        if chemin == "v2/navitia/lines/line:IDFM:C01742/line_reports":
            cdg = {"pt_object": {"embedded_type": "line"},
                   "impacted_section": {"from": {"name": "Charles de Gaulle - Étoile (Paris)"},
                                        "to": {"name": "Charles de Gaulle - Étoile (Paris)"}}}
            nation = {"pt_object": {"embedded_type": "line"},
                      "impacted_section": {"from": {"name": "Vincennes (Vincennes)"}, "to": {"name": "Nation (Paris)"}}}
            naive = MONDAY_0730.replace(tzinfo=None)
            return {"disruptions": [
                disruption("Panne d'un ascenseur", STATION, naive - timedelta(days=9), naive + timedelta(days=300)),
                disruption("CDG Étoile non desservie", cdg, naive - timedelta(hours=8), naive + timedelta(hours=12)),
                # Starts after 9h: within 90 min of being at the stop (8h30), not of now (7h30)
                disruption("Incident Vincennes - Nation", nation, naive + timedelta(minutes=95), naive + timedelta(hours=4)),
            ]}
        if chemin == "v2/navitia/journeys":
            assert params["datetime"] == "20261026T083000"  # leaving when at the stop, not now

            def section(start, end, stops, leaves):
                return {"type": "public_transport", "from": {"name": start}, "to": {"name": end},
                        "departure_date_time": navitia(at(leaves)),
                        "stop_date_times": [{"stop_point": {"name": n}} for n in stops]}
            return {"journeys": [{
                "departure_date_time": navitia(at("08:36")),
                "arrival_date_time": navitia(at("09:20")),
                "sections": [section(ENDS[0], ENDS[1], ROUTE[:2], "08:36"), {"type": "transfer"},
                             section(ENDS[2], ENDS[3], ROUTE[2:], "08:47")],
            }]}
        raise AssertionError(f"unexpected call: {chemin} {params}")


@pytest.fixture
def monday_0730(monkeypatch):
    import plugins.transport_idfm.idfm_api as idfm_api
    import plugins.transport_idfm.transport_idfm as transport_idfm

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return MONDAY_0730.astimezone(tz) if tz else MONDAY_0730.replace(tzinfo=None)

    for module in (idfm_api, transport_idfm):
        monkeypatch.setattr(module, "datetime", Frozen)
    monkeypatch.setattr(meteo, "prevision",
                        lambda lat, lon, tz, debut, fin: meteo.analyser(OPEN_METEO, tz, debut, fin))


class TestCollection:

    def test_monday_morning_for_being_at_the_stop_at_0830(self, monday_0730):
        plugin = TransportIdfm({"id": "transport_idfm"})
        settings = {"busStop": "Les Platanes"}  # defaults: at the stop at 08:30, bike 08:15-09:00
        client = FakeClient()

        etat, noms = plugin._collecter(client, settings)

        assert etat.erreurs == []
        assert etat.a_l_arret == at("08:30")
        # Live 110 at 8h28, then timetable buses (the 8h29 110 is the same bus as the live 8h28)
        assert [(p.ligne, f"{p.heure:%H:%M}", p.prevu) for p in etat.bus] == [
            ("110", "08:28", False), ("106", "08:36", True), ("108", "08:44", True)]
        # Trains reachable after the first bus caught at the stop (8h36 + 10 min), westbound only
        assert [(f"{p.heure:%H:%M}", p.destination) for p in etat.rer] == [
            ("08:47", "Saint-Germain-en-Laye"), ("08:52", "Cergy-le-Haut"), ("08:57", "Poissy")]
        assert etat.depart_trajet == at("08:36") and etat.arrivee == at("09:20")
        assert etat.perturbations == ["Incident Vincennes - Nation"]
        assert etat.infos_trafic == ["CDG Étoile non desservie"]
        assert etat.meteo.alertes == ["Pluie dès 8h30", "Rafales à 55 km/h"]
        assert noms["bus"] == "Les Platanes (Joinville-le-Pont)"

        # Line table and coordinates are cached in the settings for the next refreshes
        plugin._collecter(client, settings)
        chemins = [chemin for chemin, _ in client.calls]
        assert chemins.count("v2/navitia/stop_areas/stop_area:IDFM:70606/lines") == 1
        assert chemins.count("v2/navitia/stop_areas/stop_area:IDFM:70606") == 1

    def test_weather_can_be_hidden(self, monday_0730):
        etat, _ = TransportIdfm({"id": "transport_idfm"})._collecter(
            FakeClient(), {"busStop": "Les Platanes", "hideWeather": "true"})
        assert etat.meteo is None


class TestWeather:

    def test_bike_window(self):
        m = meteo.analyser(OPEN_METEO, PARIS, at("08:15"), at("09:00"))
        vue = m.vue()
        assert (m.temperature, m.ressenti) == (6.2, 3.1)
        assert m.pluie_debut == at("08:30") and round(m.pluie_mm, 1) == 0.8
        assert (m.vent, m.rafales, m.pluie_proba) == (22, 55, 70)
        assert vue["etat"] == "Pluie faible"
        assert vue["pluie"] == "Pluie dès 8h30 (0,8 mm)"
        assert vue["icone"].endswith("plugins/weather/icons/04d.png")

    def test_dry_mild_morning_has_no_alerts(self):
        data = {"current": {"temperature_2m": 12, "apparent_temperature": 11, "weather_code": 1, "is_day": 1},
                "hourly": {"time": ["2026-10-26T08:00"], "temperature_2m": [12], "apparent_temperature": [11],
                           "precipitation_probability": [5], "weather_code": [1], "wind_speed_10m": [10],
                           "wind_gusts_10m": [20]},
                "minutely_15": {"time": ["2026-10-26T08:15"], "precipitation": [0.0]}}
        m = meteo.analyser(data, PARIS, at("08:15"), at("09:00"))
        assert m.alertes == [] and m.vue()["pluie"] == "Pas de pluie prévue"

    def test_frost(self):
        data = {"current": {"temperature_2m": -1, "apparent_temperature": -5, "weather_code": 0, "is_day": 1}}
        assert "Risque de verglas" in meteo.analyser(data, PARIS, at("08:15"), at("09:00")).alertes
