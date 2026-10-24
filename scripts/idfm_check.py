#!/usr/bin/env python3
"""Read-only check of the PRIM (Île-de-France Mobilités) APIs used by the transport plugin.

Usage, from the InkyPi folder on the Pi:
    sudo /usr/local/inkypi/venv_inkypi/bin/python scripts/idfm_check.py "Bus stop name"

Reads PRIM_API_KEY from the .env file. Prints what the APIs return for the bus
stop, the lines serving it, the RER A disruptions, and what the plugin would
display from all that, now and 75 minutes ahead; never prints the key.
"""
import html
import logging
import os
import re
import sys
from collections import Counter
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://prim.iledefrance-mobilites.fr/marketplace"
RER_A = "line:IDFM:C01742"


def val(x):
    """SIRI Lite fields are either {"value": ...} or [{"value": ...}]."""
    if isinstance(x, list):
        x = x[0] if x else None
    return x.get("value") if isinstance(x, dict) else x


def get(path, params=None):
    response = requests.get(f"{BASE}/{path}", params=params, timeout=20,
                            headers={"apikey": API_KEY, "Accept": "application/json"})
    if response.status_code in (401, 403):
        raise RuntimeError(f"HTTP {response.status_code}: key refused (check the PRIM subscriptions)")
    response.raise_for_status()
    return response.json()


def text(disruption):
    message = (disruption.get("messages") or [{}])[0].get("text") or ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", message)).split())


def section(title, fn):
    print(f"\n== {title}")
    try:
        fn()
    except Exception as e:
        print("ERROR:", e)


def find_stop():
    places = get("v2/navitia/places", {"q": STOP_NAME, "type[]": "stop_area", "count": 5}).get("places") or []
    for place in places:
        print(place.get("id"), "|", place.get("name"))
    if places:
        stop["id"] = places[0]["id"]
        print("-> using", stop["id"])


def departures():
    code = stop["id"].split(":")[-1]
    data = get("stop-monitoring", {"MonitoringRef": f"STIF:StopArea:SP:{code}:"})
    visits = []
    for delivery in data.get("Siri", {}).get("ServiceDelivery", {}).get("StopMonitoringDelivery", []):
        visits += delivery.get("MonitoredStopVisit") or []
    print(f"{len(visits)} departures")
    for visit in visits[:12]:
        journey = visit.get("MonitoredVehicleJourney", {})
        call = journey.get("MonitoredCall", {})
        print(val(journey.get("LineRef")), "|", val(journey.get("PublishedLineName")), "|",
              val(call.get("DestinationDisplay")) or val(journey.get("DestinationName")), "|",
              call.get("ExpectedDepartureTime") or call.get("AimedDepartureTime"))


def lines():
    for line in get(f"v2/navitia/stop_areas/{stop['id']}/lines").get("lines", []):
        print(line.get("code"), "|", line.get("id"), "|", (line.get("commercial_mode") or {}).get("name"))


def rer_disruptions():
    disruptions = get(f"v2/navitia/lines/{RER_A}/line_reports").get("disruptions", [])
    print(f"{len(disruptions)} disruptions")
    impacted = Counter()
    line_level = []
    for d in disruptions:
        objects = d.get("impacted_objects") or []
        kinds = {str((o.get("pt_object") or {}).get("embedded_type")) + ("+section" if "impacted_section" in o else "")
                 for o in objects}
        impacted.update(kinds)
        if any(k.startswith("line") for k in kinds):
            line_level.append(d)
    print("Impacted objects:", dict(impacted))
    print(f"{len(line_level)} affect the line or a section of it:")
    for d in line_level[:15]:
        periods = d.get("application_periods") or [{}]
        print(d.get("status"), "|", periods[0].get("begin"), "->", periods[0].get("end"),
              f"({len(periods)} periods)", "|", text(d)[:70])
        for o in d.get("impacted_objects") or []:
            section_info = o.get("impacted_section")
            if section_info:
                print("    section:", (section_info.get("from") or {}).get("name"), "->",
                      (section_info.get("to") or {}).get("name"))
            if o.get("impacted_stops"):
                print("    impacted stops:", [(s.get("stop_point") or {}).get("name") for s in o["impacted_stops"]][:8])


def journey():
    destination = get("v2/navitia/places", {"q": "Nanterre Préfecture", "type[]": "stop_area", "count": 1})["places"][0]
    data = get("v2/navitia/journeys", {"from": stop["id"], "to": destination["id"],
                                       "data_freshness": "realtime", "count": 1})
    journeys = data.get("journeys") or []
    if not journeys:
        print("no journey found")
        return
    j = journeys[0]
    print("status:", j.get("status") or "(none)", "| leave", j.get("departure_date_time"),
          "| arrive", j.get("arrival_date_time"))
    for s in j.get("sections") or []:
        if s.get("type") != "public_transport":
            continue
        info = s.get("display_informations") or {}
        stops = [(t.get("stop_point") or {}).get("name") for t in s.get("stop_date_times") or []]
        links = [l for l in info.get("links") or [] if l.get("type") == "disruption"]
        print(info.get("commercial_mode"), info.get("code"), "|", (s.get("from") or {}).get("name"), "->",
              (s.get("to") or {}).get("name"), f"| {len(links)} disruptions linked")
        print("    stops:", ", ".join(stops))
    for d in data.get("disruptions") or []:
        print("journey disruption:", d.get("status"), "|", (d.get("severity") or {}).get("effect"), "|", text(d)[:80])


def plugin_view(minutes_ahead=0):
    """What the plugin would display for someone at the bus stop `minutes_ahead` from now."""
    def view():
        sys.path.insert(0, os.path.join(ROOT, "src"))
        logging.disable(logging.CRITICAL)  # the plugin logs full tracebacks; errors are listed below instead
        from datetime import datetime, timedelta
        from plugins.transport_idfm.idfm_api import PrimClient
        from plugins.transport_idfm.transport_idfm import TransportIdfm

        tz = ZoneInfo("Europe/Paris")
        at_stop = (datetime.now(tz) + timedelta(minutes=minutes_ahead)).strftime("%H:%M")
        bike_to = (datetime.now(tz) + timedelta(minutes=minutes_ahead + 45)).strftime("%H:%M")
        settings = {"busStop": STOP_NAME, "atStop": at_stop, "bikeFrom": at_stop, "bikeTo": bike_to}
        state, names = TransportIdfm({"id": "transport_idfm"})._collecter(PrimClient(API_KEY, tz), settings)
        print(f"At the stop at {at_stop} (≈ = timetable, not live)")
        print("Bus:", [f"{p.ligne} {'≈' if p.prevu else ''}{p.heure:%H:%M} {p.destination}" for p in state.bus])
        print("RER:", [f"{'≈' if p.prevu else ''}{p.heure:%H:%M} {p.destination}" for p in state.rer])
        print("Traffic banner:", (state.perturbations[0] if state.perturbations else "normal")
              if state.trafic_connu else "unknown")
        print("Small traffic info:", state.infos_trafic or None)
        print("Arrival:", f"{state.arrivee:%H:%M} (bus at {state.depart_trajet:%H:%M})" if state.arrivee else None)
        if state.meteo:
            w = state.meteo.vue()
            print(f"Weather: {w['temperature']}° (feels {w['ressenti']}°) | bike ride {at_stop}-{bike_to}: "
                  f"{w['etat']} | {w['temp_trajet']} | {w['pluie']} | {w['vent']} | alerts: {w['alertes'] or None}")
        print("Errors:", state.erreurs or None)
    return view


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Usage: idfm_check.py "Bus stop name"')
    STOP_NAME = sys.argv[1]
    load_dotenv(os.path.join(ROOT, ".env"))
    API_KEY = os.getenv("PRIM_API_KEY")
    if not API_KEY:
        sys.exit("PRIM_API_KEY is missing from the .env file")

    stop = {}
    section("Bus stop", find_stop)
    if stop:
        section("Departures at that stop", departures)
        section("Lines at that stop", lines)
        section("Journey to Nanterre Préfecture (realtime)", journey)
    section("RER A disruptions (Navitia)", rer_disruptions)
    section("What the plugin would show now", plugin_view(0))
    section("What it would show 75 minutes ahead (like 7h30 for 8h45)", plugin_view(75))
