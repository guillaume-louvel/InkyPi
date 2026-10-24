#!/usr/bin/env python3
"""Read-only check of the PRIM (Île-de-France Mobilités) APIs used by the transport plugin.

Usage, from the InkyPi folder on the Pi:
    sudo /usr/local/inkypi/venv_inkypi/bin/python scripts/idfm_check.py "Bus stop name"

Reads PRIM_API_KEY from the .env file. Prints what the APIs return for the bus
stop, the lines serving it, and the RER A disruptions; never prints the key.
"""
import os
import sys

import requests
from dotenv import load_dotenv

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
    for d in disruptions[:12]:
        periods = d.get("application_periods") or [{}]
        message = ((d.get("messages") or [{}])[0].get("text") or "").replace("\n", " ")
        print(d.get("status"), "|", (d.get("severity") or {}).get("effect"), "|",
              periods[0].get("begin"), "->", periods[0].get("end"), f"({len(periods)} periods)", "|",
              message[:70])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('Usage: idfm_check.py "Bus stop name"')
    STOP_NAME = sys.argv[1]
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    API_KEY = os.getenv("PRIM_API_KEY")
    if not API_KEY:
        sys.exit("PRIM_API_KEY is missing from the .env file")

    stop = {}
    section("Bus stop", find_stop)
    if stop:
        section("Departures at that stop", departures)
        section("Lines at that stop", lines)
    section("RER A disruptions (Navitia)", rer_disruptions)
