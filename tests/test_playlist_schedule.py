"""Playlists with days of the week and their own refresh interval (e.g. a weekday morning playlist)."""
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
import pytz

from blueprints.playlist import parse_schedule
from model import Playlist, PlaylistManager, RefreshInfo
from refresh_task import RefreshTask

PARIS = pytz.timezone("Europe/Paris")
PLUGIN = {"plugin_id": "transport_idfm", "name": "Matin", "plugin_settings": {}, "refresh": {"interval": 300}}
COMIC = {"plugin_id": "comic", "name": "XKCD", "plugin_settings": {}, "refresh": {"interval": 3600}}

MONDAY = PARIS.localize(datetime(2026, 10, 26))
SATURDAY = PARIS.localize(datetime(2026, 10, 24))


def at(day, hhmm):
    hours, minutes = map(int, hhmm.split(":"))
    return day + timedelta(hours=hours, minutes=minutes)


def manager():
    return PlaylistManager([
        Playlist("Default", "00:00", "24:00", [COMIC]),
        Playlist("Matin", "07:30", "08:30", [PLUGIN], days=[0, 1, 2, 3, 4], cycle_seconds=300),
    ])


class TestPlaylistSchedule:

    @pytest.mark.parametrize("now,expected", [
        (at(MONDAY, "07:29"), "Default"),
        (at(MONDAY, "07:30"), "Matin"),
        (at(MONDAY, "08:29"), "Matin"),
        (at(MONDAY, "08:30"), "Default"),
        (at(SATURDAY, "07:45"), "Default"),  # weekends skip the morning playlist
    ])
    def test_active_playlist(self, now, expected):
        assert manager().determine_active_playlist(now).name == expected

    @pytest.mark.parametrize("now,seconds", [
        (at(MONDAY, "07:10"), 20 * 60),        # until the morning playlist starts
        (at(MONDAY, "08:00"), 30 * 60),        # until it ends
        (at(MONDAY, "23:00"), 60 * 60),        # until midnight (Default's boundary)
    ])
    def test_seconds_until_next_change(self, now, seconds):
        assert manager().seconds_until_next_change(now) == seconds

    def test_round_trip_and_old_configs(self):
        playlist = manager().playlists[1]
        assert Playlist.from_dict(playlist.to_dict()).to_dict() == playlist.to_dict()

        old = {"name": "Default", "start_time": "00:00", "end_time": "24:00", "plugins": []}
        restored = Playlist.from_dict(old)
        assert restored.days is None and restored.cycle_seconds is None
        assert "days" not in restored.to_dict() and "cycle_seconds" not in restored.to_dict()


def refresh_task(playlist_manager, now):
    device_config = Mock()
    device_config.get_config.side_effect = lambda key, default=None: {
        "plugin_cycle_interval_seconds": 3600, "timezone": "Europe/Paris"}.get(key, default)
    device_config.get_playlist_manager.return_value = playlist_manager
    task = RefreshTask(device_config, Mock())
    task._get_current_datetime = lambda: now
    return task


class TestRefreshTiming:

    @pytest.mark.parametrize("now,sleep", [
        (at(MONDAY, "07:00"), 30 * 60 + 1),   # device interval (1 h) cut short at 07:30
        (at(MONDAY, "07:40"), 300),           # the morning playlist's own interval
        (at(MONDAY, "08:28"), 2 * 60 + 1),    # cut short at 08:30, back to Default
        (at(SATURDAY, "07:40"), 50 * 60 + 1), # Default on weekends; the 08:30 boundary still wakes it
    ])
    def test_sleep_follows_the_schedule(self, now, sleep):
        assert refresh_task(manager(), now)._sleep_seconds() == sleep

    def test_switches_as_soon_as_the_playlist_changes(self):
        playlists = manager()
        now = at(MONDAY, "07:30")
        task = refresh_task(playlists, now)
        # Default refreshed the comic 10 minutes ago: its 1 h interval has not elapsed
        latest = RefreshInfo("Playlist", "comic", (now - timedelta(minutes=10)).isoformat(), "hash",
                             playlist="Default", plugin_instance="XKCD")

        playlist, plugin = task._determine_next_plugin(playlists, latest, now)

        assert playlist.name == "Matin" and plugin.name == "Matin"

    def test_uses_the_playlist_interval(self):
        playlists = manager()
        now = at(MONDAY, "07:50")
        task = refresh_task(playlists, now)
        latest = RefreshInfo("Playlist", "transport_idfm", (now - timedelta(minutes=4)).isoformat(), "hash",
                             playlist="Matin", plugin_instance="Matin")
        assert task._determine_next_plugin(playlists, latest, now) == (None, None)

        latest.refresh_time = (now - timedelta(minutes=5)).isoformat()
        assert task._determine_next_plugin(playlists, latest, now)[0].name == "Matin"


class TestScheduleForm:

    @pytest.mark.parametrize("data,expected", [
        ({}, (None, None)),
        ({"days": [4, 0, 1, 2, 3], "cycle_minutes": 5}, ([0, 1, 2, 3, 4], 300)),
        ({"days": list(range(7)), "cycle_minutes": ""}, (None, None)),  # every day = no restriction
    ])
    def test_valid(self, data, expected):
        assert parse_schedule(data) == expected

    @pytest.mark.parametrize("data", [{"days": []}, {"days": [7]}, {"cycle_minutes": 0}, {"cycle_minutes": "abc"}])
    def test_invalid(self, data):
        with pytest.raises(ValueError):
            parse_schedule(data)
