#!/usr/bin/env python3
"""Read-only check of the PRIM (Île-de-France Mobilités) APIs used by the transport plugin.

Usage, from the InkyPi folder on the Pi:
    sudo /usr/local/inkypi/venv_inkypi/bin/python scripts/idfm_check.py "Bus stop name"

Reads PRIM_API_KEY from the .env file. Prints what the APIs return for the bus
stop, the lines serving it, the RER A disruptions, and what the plugin would
display from all that; never prints the key.
"""
import logging
import os
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
        message = ((d.get("messages") or [{}])[0].get("text") or "").replace("\n", " ")
        print(d.get("status"), "|", (d.get("severity") or {}).get("effect"), "|",
              periods[0].get("begin"), "->", periods[0].get("end"), f"({len(periods)} periods)", "|",
              message[:70])


def plugin_view():
    sys.path.insert(0, os.path.join(ROOT, "src"))
    logging.disable(logging.CRITICAL)  # the plugin logs full tracebacks; errors are listed below instead
    from plugins.transport_idfm.idfm_api import PrimClient
    from plugins.transport_idfm.transport_idfm import TransportIdfm

    client = PrimClient(API_KEY, ZoneInfo("Europe/Paris"))
    state, names = TransportIdfm({"id": "transport_idfm"})._collecter(client, {"busStop": STOP_NAME})
    print("Bus:", [f"{p.ligne} {p.heure:%H:%M} {p.destination}" for p in state.bus])
    print("RER:", [f"{p.heure:%H:%M} {p.destination}" for p in state.rer])
    print("Traffic banner:", (state.perturbations[0] if state.perturbations else "normal")
          if state.trafic_connu else "unknown")
    print("Arrival:", f"{state.arrivee:%H:%M}" if state.arrivee else None)
    print("Errors:", state.erreurs or None)


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
    section("RER A disruptions (Navitia)", rer_disruptions)
    section("What the plugin would show now", plugin_view)
