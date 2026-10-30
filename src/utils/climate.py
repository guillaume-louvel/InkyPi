"""Indoor temperature and humidity from a BME280 sensor, recorded every hour.

ClimateRecorder reads the sensor in a background thread and keeps one week of readings in a small
SQLite database (about 170 rows). Plugins read them back with latest() and history(), which apply
the corrections set on the Settings page ("climate_temp_offset", "climate_humidity_offset"). Raw
readings are stored, so the corrections can be changed at any time, history included.
"""

import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from utils.app_utils import resolve_path

logger = logging.getLogger(__name__)

DB_FILE = resolve_path(os.path.join("config", "climate.sqlite3"))
INTERVAL_SECONDS = 60 * 60
MAX_AGE_SECONDS = INTERVAL_SECONDS + 30 * 60  # older than this, the sensor is considered missing
KEEP_SECONDS = 7 * 24 * 3600 + 3600  # a week, plus a margin for the start of the week graph
ADDRESSES = (0x76, 0x77)  # the two addresses a BME280 can use
I2C_BUS = 1


@dataclass(frozen=True)
class Reading:
    time: datetime          # UTC
    temperature: float      # °C
    humidity: float         # % relative humidity


def saturation_pressure(temperature):
    """Water vapour saturation pressure in hPa (Magnus formula)."""
    return 6.112 * math.exp(17.62 * temperature / (243.12 + temperature))


def vapour_pressure(temperature, humidity):
    """Actual water vapour pressure in hPa: compares how much water the air holds, whatever its temperature."""
    return humidity / 100 * saturation_pressure(temperature)


def corrected(reading, offset, humidity_offset=0):
    """Applies the corrections. For the temperature, the air holds the same amount of water, so
    relative humidity is recomputed for the corrected temperature (a sensor warmed by the Pi reads
    too dry). The humidity correction, in % points, then fixes the humidity sensor's own bias."""
    if not offset and not humidity_offset:
        return reading
    temperature = reading.temperature + offset
    humidity = vapour_pressure(reading.temperature, reading.humidity) / saturation_pressure(temperature) * 100
    return replace(reading, temperature=temperature, humidity=max(0.0, min(100.0, humidity + humidity_offset)))


def open_sensor():
    """Returns a ready BME280, or None when the library or the sensor is missing."""
    try:
        from bme280 import BME280
        from smbus2 import SMBus
    except ImportError:
        logger.info("BME280 library not installed, indoor climate disabled")
        return None
    for address in ADDRESSES:
        try:
            sensor = BME280(i2c_addr=address, i2c_dev=SMBus(I2C_BUS))
            sensor.setup(mode="forced")
            sensor.update_sensor()  # the first measurement after a reset is unreliable
            logger.info(f"BME280 found at 0x{address:02x}")
            return sensor
        except (OSError, RuntimeError):
            continue
    logger.info("No BME280 found, indoor climate disabled")
    return None


def _connect(db_file):
    connection = sqlite3.connect(db_file, timeout=10)
    connection.execute("CREATE TABLE IF NOT EXISTS readings (ts REAL PRIMARY KEY, temperature REAL, humidity REAL)")
    return connection


def store(reading, db_file=None):
    connection = _connect(db_file or DB_FILE)
    try:
        with connection:
            connection.execute("INSERT OR REPLACE INTO readings VALUES (?, ?, ?)",
                               (reading.time.timestamp(), reading.temperature, reading.humidity))
            connection.execute("DELETE FROM readings WHERE ts < ?", (reading.time.timestamp() - KEEP_SECONDS,))
    finally:
        connection.close()


def history(seconds, offset=0, humidity_offset=0, db_file=None, now=None):
    """Readings from the last `seconds`, oldest first, with the corrections applied."""
    db_file = db_file or DB_FILE
    if not os.path.exists(db_file):
        return []
    since = (now or datetime.now(timezone.utc)).timestamp() - seconds
    connection = _connect(db_file)
    try:
        rows = connection.execute("SELECT ts, temperature, humidity FROM readings WHERE ts >= ? ORDER BY ts",
                                  (since,)).fetchall()
    finally:
        connection.close()
    return [corrected(Reading(datetime.fromtimestamp(ts, timezone.utc), t, h), offset, humidity_offset)
            for ts, t, h in rows]


def latest(offset=0, humidity_offset=0, max_age_seconds=MAX_AGE_SECONDS, db_file=None, now=None):
    """The most recent reading if it is fresh enough, else None (sensor unplugged, recorder stopped)."""
    readings = history(max_age_seconds, offset, humidity_offset, db_file, now)
    return readings[-1] if readings else None


class ClimateRecorder:
    """Background thread storing a sensor reading every hour."""

    def __init__(self, sensor, db_file=None):
        self.sensor = sensor
        self.db_file = db_file
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        logger.info("Starting indoor climate recorder")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def record(self):
        self.sensor.update_sensor()
        reading = Reading(datetime.now(timezone.utc), self.sensor.temperature, self.sensor.humidity)
        store(reading, self.db_file)
        logger.debug(f"Indoor climate recorded | {reading}")
        return reading

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.record()
            except Exception:
                logger.exception("Failed to read the BME280")
            # Align on the clock (14:00, 15:00…) so the history lines up
            self.stop_event.wait(INTERVAL_SECONDS - time.time() % INTERVAL_SECONDS)
