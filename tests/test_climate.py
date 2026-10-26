from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytz

from plugins.indoor_climate.indoor_climate import Meteo, analyser_meteo, confort, graphe, moment_pour_aerer
from utils import climate
from utils.climate import ClimateRecorder, Reading, corrected

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)   # 14h in Paris
PARIS = pytz.timezone("Europe/Paris")


def reading(minutes_ago, temperature=20.0, humidity=50.0):
    return Reading(NOW - timedelta(minutes=minutes_ago), temperature, humidity)


def outside(hours_ahead, temperature, humidity=70.0, rain=0.0):
    return Meteo(NOW + timedelta(hours=hours_ahead), temperature, humidity, rain)


class TestCorrection:

    def test_warm_sensor_reads_too_dry(self):
        # A sensor 2 °C too warm: the corrected air is colder, so relatively more humid
        fixed = corrected(Reading(NOW, 22.0, 50.0), -2.0)
        assert fixed.temperature == 20.0
        assert 55 < fixed.humidity < 57   # same amount of water at a lower temperature

    def test_no_offset_changes_nothing(self):
        r = Reading(NOW, 22.0, 50.0)
        assert corrected(r, 0) is r


class TestStorage:

    def test_history_latest_and_one_week_kept(self, tmp_path):
        db = str(tmp_path / "climate.sqlite3")
        climate.store(reading(8 * 24 * 60), db)        # older than a week: removed on the next write
        climate.store(reading(20, temperature=19.0), db)
        climate.store(reading(10, temperature=21.0), db)

        assert [r.temperature for r in climate.history(30 * 24 * 3600, db_file=db, now=NOW)] == [19.0, 21.0]
        assert climate.latest(db_file=db, now=NOW).temperature == 21.0
        assert climate.latest(offset=-1, db_file=db, now=NOW).temperature == 20.0
        # Nothing recent (sensor unplugged for 2 hours): no reading rather than an old one
        assert climate.latest(db_file=db, now=NOW + timedelta(minutes=70)).temperature == 21.0
        assert climate.latest(db_file=db, now=NOW + timedelta(hours=2)) is None

    def test_missing_database(self, tmp_path):
        assert climate.history(3600, db_file=str(tmp_path / "none.sqlite3")) == []

    def test_recorder_stores_one_measurement(self, tmp_path):
        db = str(tmp_path / "climate.sqlite3")
        sensor = SimpleNamespace(temperature=21.5, humidity=47.0, update_sensor=lambda: None)
        recorded = ClimateRecorder(sensor, db).record()
        assert (recorded.temperature, recorded.humidity) == (21.5, 47.0)
        assert climate.latest(db_file=db).temperature == 21.5

    def test_no_sensor_here(self):
        # No I2C bus in the test environment: detection gives up quietly
        assert climate.open_sensor() is None


class TestComfort:

    @pytest.mark.parametrize("temperature,humidity,expected", [
        (20.5, 50, "Confortable"),
        (17.0, 50, "Frais"),
        (20.0, 33, "Air sec"),
        (26.0, 75, "Chaud · Très humide : risque de moisissures"),
    ])
    def test_confort(self, temperature, humidity, expected):
        assert confort(temperature, humidity) == expected


class TestWhenToOpenTheWindows:

    def test_autumn_now(self):
        # Cold damp air outside still holds less water than the room: air it, briefly
        room = Reading(NOW, 20.0, 63.0)
        assert moment_pour_aerer(room, [outside(0, 9, 88)], PARIS) == \
            "Bon moment pour aérer : 5 à 10 min suffisent (9° dehors)"

    def test_wait_for_the_rain_to_stop(self):
        room = Reading(NOW, 20.0, 55.0)
        forecast = [outside(0, 11, 95, rain=1.2), outside(1, 11, 95, rain=0.8), outside(2, 12, 80)]
        assert moment_pour_aerer(room, forecast, PARIS) == "Il pleut : aérez plutôt vers 16h (12°)"

    def test_summer_heat_wait_for_the_evening(self):
        room = Reading(NOW, 27.0, 45.0)
        forecast = [outside(0, 32, 30)] + [outside(h, 32 - 1.5 * h, 40) for h in range(1, 10)]
        # 32 - 1.5 h <= 25 from h = 5: 18:00 UTC, 19h in Paris
        assert moment_pour_aerer(room, forecast, PARIS) == \
            "Plus chaud dehors (32°) : aérez plutôt vers 19h (24°)"

    def test_summer_heat_cooler_outside_now(self):
        assert moment_pour_aerer(Reading(NOW, 27.0, 45.0), [outside(0, 19, 60)], PARIS) == \
            "Bon moment pour aérer : plus frais dehors (19°)"

    def test_muggy_all_day(self):
        room = Reading(NOW, 22.0, 50.0)
        forecast = [outside(h, 26, 85) for h in range(0, 24)]
        assert moment_pour_aerer(room, forecast, PARIS) == \
            "Air plus humide dehors : gardez les fenêtres fermées aujourd'hui"


class TestWeekGraph:

    def test_indoor_and_outdoor_share_one_scale(self):
        inside = [(NOW - timedelta(minutes=m), 50.0) for m in range(7 * 24 * 60, -1, -60) if not 600 <= m <= 900]
        outside = [(NOW - timedelta(minutes=m), 90.0) for m in range(7 * 24 * 60, -1, -60)]
        g = graphe({"int": inside, "ext": outside}, NOW, PARIS, 30, 70, " %", zone=(40, 60))
        assert len(g["traces"]["int"]) == 2          # broken where the Pi was off for 5 h
        assert len(g["traces"]["ext"]) == 1
        # 50 % inside sits below 90 % outside on the same axis (y grows downwards)
        y_in = float(g["traces"]["int"][0].split()[0].split(",")[1])
        y_out = float(g["traces"]["ext"][0].split()[0].split(",")[1])
        assert y_in > y_out
        assert len(g["minuits"]) == 7
        assert g["jours"][0]["texte"] == "lun."      # today, after noon
        assert [z["texte"] for z in g["zone"]] == ["40 %", "60 %"]

    def test_no_graph_without_indoor_readings(self):
        assert graphe({"int": [], "ext": [(NOW, 10.0), (NOW, 11.0)]}, NOW, PARIS, 18, 22) is None

    def test_midnight_stays_at_midnight_across_the_clock_change(self):
        # Clocks went back on Sunday 25 October: Saturday and Sunday midnights are 25 h apart
        week = [(NOW - timedelta(minutes=m), 20.0) for m in range(7 * 24 * 60, -1, -60)]
        g = graphe({"int": week}, NOW, PARIS, 18, 22)
        gaps = sorted(round(b - a, 1) for a, b in zip(sorted(g["minuits"]), sorted(g["minuits"])[1:]))
        day = 700 / 7
        assert gaps[-1] == pytest.approx(day * 25 / 24, abs=0.2)
        assert gaps[0] == pytest.approx(day, abs=0.2)


class TestOutdoorWeather:

    def response(self):
        # Hourly from Saturday 00:00 UTC to Wednesday 00:00 UTC, temperature = hour of the day
        hours = [datetime(2026, 10, 24, tzinfo=timezone.utc) + timedelta(hours=k) for k in range(96)]
        return {
            "current": {"temperature_2m": 9.4, "relative_humidity_2m": 88, "apparent_temperature": 7.2,
                        "precipitation": 0, "weather_code": 3},
            "hourly": {"time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours],
                       "temperature_2m": [float(h.hour) for h in hours],
                       "relative_humidity_2m": [80] * len(hours),
                       "precipitation": [0] * len(hours)},
        }

    def test_now_today_forecast_and_history(self):
        ext = analyser_meteo(self.response(), PARIS, NOW)
        assert (ext.actuel.temperature, ext.actuel.humidite, ext.ressenti, ext.etat) == (9.4, 88, 7.2, "Couvert")
        # Monday in Paris = Sunday 23:00 UTC to Monday 23:00 UTC: hours 23 and 0..22
        assert (ext.t_min, ext.t_max) == (0.0, 23.0)
        assert len(ext.prevues) == 24 and ext.prevues[0].time == NOW + timedelta(hours=1)
        assert ext.historique[-1].time == NOW and ext.historique[0].time == datetime(2026, 10, 24, tzinfo=timezone.utc)
        assert ext.pour_aerer()[0] is ext.actuel

    def test_missing_values_are_skipped(self):
        data = self.response()
        data["hourly"]["temperature_2m"][62] = None   # Monday 14:00 UTC
        ext = analyser_meteo(data, PARIS, NOW)
        assert all(m.time != NOW + timedelta(hours=1) for m in ext.prevues)
