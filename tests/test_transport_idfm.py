"""Tests of the IDFM transport plugin, using response shapes captured from the real PRIM APIs."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from plugins.transport_idfm.idfm_api import Passage, PrimClient, code_ligne, filtrer
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

        assert client.extraire_perturbations(data) == ["Incident technique, retards", "Travaux entre Joinville et Nation"]

    def test_html_message_is_cleaned(self):
        d = {"messages": [{"text": "<p>P&#233;riode : en semaine<br>d&#232;s 21h50</p>",
                           "channel": {"content_type": "text/html"}}]}
        assert PrimClient._message(d) == "Période : en semaine dès 21h50"

    def test_prefers_plain_text_message(self):
        d = {"messages": [{"text": "<b>Long HTML</b>", "channel": {"content_type": "text/html"}},
                          {"text": "Court", "channel": {"content_type": "text/plain"}}]}
        assert PrimClient._message(d) == "Court"


class FakeClient(PrimClient):
    """PrimClient answering from the responses captured at Les Platanes / Joinville-le-Pont."""

    def __init__(self):
        super().__init__("key", PARIS)
        self.calls = []

    def get(self, chemin, params=None):
        self.calls.append(chemin)
        now = datetime.now(PARIS)
        if chemin == "v2/navitia/places":
            ids = {"Les Platanes": ("stop_area:IDFM:70606", "Les Platanes (Joinville-le-Pont)"),
                   "Joinville-le-Pont": ("stop_area:IDFM:70640", "Joinville-le-Pont (Joinville-le-Pont)"),
                   "Nanterre Préfecture": ("stop_area:IDFM:71379", "Nanterre Préfecture (Nanterre)")}
            stop_id, name = ids[params["q"]]
            return {"places": [{"id": stop_id, "name": name, "embedded_type": "stop_area"}]}
        if chemin == "v2/navitia/stop_areas/stop_area:IDFM:70606/lines":
            return {"lines": [{"code": "106", "id": "line:IDFM:C01135"}, {"code": "108", "id": "line:IDFM:C01137"},
                              {"code": "110", "id": "line:IDFM:C01139"}, {"code": "N33", "id": "line:IDFM:C01399"}]}
        if chemin == "stop-monitoring" and params["MonitoringRef"] == "STIF:StopArea:SP:70606:":
            visits = [siri_visit("STIF:Line::C01135:", "Villiers Gare", now + timedelta(minutes=2)),
                      siri_visit("STIF:Line::C01137:", "Joinville RER", now + timedelta(minutes=4)),
                      siri_visit("STIF:Line::C01135:", "Joinville RER", now + timedelta(minutes=5)),
                      siri_visit("STIF:Line::C01139:", "Joinville RER", now + timedelta(minutes=16))]
            return {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": visits}]}}}
        if chemin == "stop-monitoring":
            visits = [siri_visit("STIF:Line::C01742:", "Boissy-Saint-Léger", now + timedelta(minutes=3)),
                      siri_visit("STIF:Line::C01742:", "Saint-Germain-en-Laye", now + timedelta(minutes=6))]
            return {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [{"MonitoredStopVisit": visits}]}}}
        if chemin == "v2/navitia/lines/line:IDFM:C01742/line_reports":
            return {"disruptions": []}
        if chemin == "v2/navitia/journeys":
            return {"journeys": [{"departure_date_time": navitia(now + timedelta(minutes=4)),
                                  "arrival_date_time": navitia(now + timedelta(minutes=50))}]}
        raise AssertionError(f"unexpected call: {chemin} {params}")


class TestCollection:

    def test_bus_lines_are_filtered_and_named_from_line_codes(self):
        plugin = TransportIdfm({"id": "transport_idfm"})
        settings = {"busStop": "Les Platanes"}
        client = FakeClient()

        etat, noms = plugin._collecter(client, settings)

        assert etat.erreurs == []
        assert [(p.ligne, p.destination) for p in etat.bus] == [("108", "Joinville RER"), ("106", "Joinville RER"),
                                                                ("110", "Joinville RER")]
        assert [p.destination for p in etat.rer] == ["Saint-Germain-en-Laye"]
        assert etat.trafic_connu and etat.perturbations == []
        assert etat.arrivee is not None
        assert noms["bus"] == "Les Platanes (Joinville-le-Pont)"

        # The line table is cached in the settings: no second lookup on the next refresh
        plugin._collecter(client, settings)
        assert client.calls.count("v2/navitia/stop_areas/stop_area:IDFM:70606/lines") == 1
