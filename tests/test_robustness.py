import json
import os
import time
from datetime import datetime
from unittest.mock import Mock

import pytz

import utils.image_utils as image_utils
from config import Config
from model import Playlist, PlaylistManager, RefreshInfo
from refresh_task import RefreshTask
from utils.app_utils import get_ip_address


def make_refresh_task(playlist_manager, network_monitor=None):
    device_config = Mock()
    settings = {"plugin_cycle_interval_seconds": 3600, "timezone": "UTC"}
    device_config.get_config.side_effect = lambda key, default=None: settings.get(key, default)
    device_config.get_playlist_manager.return_value = playlist_manager
    task = RefreshTask(device_config, Mock())
    task.network_monitor = network_monitor
    task._sleep_seconds = lambda: 0.01  # the schedule itself is covered in test_playlist_schedule.py
    return task


PLUGIN = {"plugin_id": "clock", "name": "Clock", "plugin_settings": {}, "refresh": {"interval": 300}}


def run_briefly(task):
    task.start()
    time.sleep(0.2)
    task.stop()


class TestRefreshTask:

    def test_skips_refresh_while_wifi_setup_is_showing(self):
        playlist_manager = Mock()
        playlist_manager.determine_active_playlist.return_value = None
        playlist_manager.seconds_until_next_change.return_value = None
        monitor = Mock()
        monitor.in_setup_mode.return_value = True
        task = make_refresh_task(playlist_manager, monitor)
        task._determine_next_plugin = Mock()

        run_briefly(task)

        task._determine_next_plugin.assert_not_called()
        task.display_manager.display_image.assert_not_called()

    def test_failed_refresh_is_retried_sooner(self):
        playlist_manager = Mock()
        playlist_manager.determine_active_playlist.side_effect = RuntimeError("network down")
        task = make_refresh_task(playlist_manager)

        run_briefly(task)

        assert task.retry_after_failure is True

    def test_forced_refresh_ignores_cycle_interval(self):
        now = datetime.now(pytz.UTC)
        playlist = Playlist("Default", "00:00", "24:00", [PLUGIN])
        playlist_manager = PlaylistManager([playlist])
        latest_refresh = RefreshInfo("Playlist", "clock", now.isoformat(), "hash", playlist="Default")
        task = make_refresh_task(playlist_manager)
        task.device_config.get_config.side_effect = lambda key, default=None: 3600

        assert task._determine_next_plugin(playlist_manager, latest_refresh, now) == (None, None)
        assert task._determine_next_plugin(playlist_manager, latest_refresh, now, force=True)[0] is playlist


class TestConfigWrite:

    def test_write_config_replaces_file_atomically(self, tmp_path):
        config_file = tmp_path / "device.json"
        config_file.write_text("{}")
        config = Config.__new__(Config)
        config.config_file = str(config_file)
        config.config = {"name": "InkyPi"}
        config.playlist_manager = Mock(to_dict=Mock(return_value={"playlists": []}))
        config.refresh_info = Mock(to_dict=Mock(return_value={}))

        config.write_config()

        assert json.loads(config_file.read_text())["name"] == "InkyPi"
        assert os.listdir(tmp_path) == ["device.json"]  # no temp files left behind


class TestScreenshot:

    def test_hung_browser_is_killed(self, tmp_path, monkeypatch):
        browser = tmp_path / "fake-chromium"
        browser.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")  # with a child process, like Chromium
        browser.chmod(0o755)
        monkeypatch.setattr(image_utils, "_find_chromium_binary", lambda: str(browser))
        monkeypatch.setattr(image_utils, "SCREENSHOT_TIMEOUT_SECONDS", 0.5)
        monkeypatch.setattr(image_utils.tempfile, "tempdir", str(tmp_path))

        start = time.monotonic()
        assert image_utils.take_screenshot_html("<p>hi</p>", (800, 480)) is None
        assert time.monotonic() - start < 5
        assert sorted(os.listdir(tmp_path)) == ["fake-chromium"]  # temp html and png removed


def test_get_ip_address_without_network(monkeypatch):
    def unreachable(self, address):
        raise OSError("Network is unreachable")
    monkeypatch.setattr("socket.socket.connect", unreachable)
    assert get_ip_address() is None
