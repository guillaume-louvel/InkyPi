import os
from unittest.mock import Mock

import pytest
from flask import Flask

import utils.wifi_manager as wifi_manager
from utils.wifi_manager import (
    HOTSPOT_CONNECTION, NEW_CONNECTION, NetworkMonitor, WifiManager, redact, split_terse,
)

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class FakeNmcli:
    """Stands in for run_nmcli: answers from a dict of responses and records every call."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, args, timeout=15):
        self.calls.append(args)
        for prefix, response in self.responses.items():
            if tuple(args[:len(prefix)]) == prefix:
                return response(args) if callable(response) else response
        return True, ""

    def called(self, *prefix):
        return [call for call in self.calls if tuple(call[:len(prefix)]) == prefix]


ACTIVE = ("-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active")
SAVED = ("-t", "-f", "NAME,TYPE", "connection", "show")
SSID = ("-t", "-g", "802-11-wireless.ssid", "connection", "show")
SCAN = ("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")


class TestNmcliHelpers:

    def test_split_terse_handles_escaped_colons(self):
        assert split_terse(r"My\:Net:802-11-wireless:activated") == ["My:Net", "802-11-wireless", "activated"]
        assert split_terse(r"back\\slash:x") == ["back\\slash", "x"]

    def test_redact_hides_passwords(self):
        logged = redact(["connection", "add", "wifi-sec.psk", "hunter22", "ssid", "home"])
        assert "hunter22" not in logged
        assert "home" in logged


class TestWifiManager:

    @pytest.mark.parametrize("active,expected", [
        (f"{HOTSPOT_CONNECTION}:802-11-wireless:activated", False),
        ("Home:802-11-wireless:activating", False),
        ("Home:802-11-wireless:activated", True),
        ("Wired connection 1:802-3-ethernet:activated", True),
        ("lo:loopback:activated", False),
    ])
    def test_is_online(self, monkeypatch, active, expected):
        monkeypatch.setattr(wifi_manager, "run_nmcli", FakeNmcli({ACTIVE: (True, active)}))
        assert WifiManager().is_online() is expected

    def test_scan_dedupes_and_sorts(self, monkeypatch):
        output = "\n".join(["Home:40:WPA2", "Home:70:WPA2", ":90:WPA2", "Cafe:55:", r"Odd\:Name:20:WPA3"])
        monkeypatch.setattr(wifi_manager, "run_nmcli", FakeNmcli({SCAN: (True, output)}))
        networks = WifiManager().scan()
        assert [n["ssid"] for n in networks] == ["Home", "Cafe", "Odd:Name"]
        assert networks[0]["signal"] == 70

    def test_connect_success_replaces_old_connection(self, monkeypatch):
        nmcli = FakeNmcli({
            ACTIVE: (True, f"{NEW_CONNECTION}:802-11-wireless:activated"),
            SAVED: (True, "preconfigured:802-11-wireless\nOther:802-11-wireless"),
            SSID: lambda args: (True, "Home" if args[-1] == "preconfigured" else "Other"),
        })
        monkeypatch.setattr(wifi_manager, "run_nmcli", nmcli)
        wifi = WifiManager()
        wifi.networks = [{"ssid": "Home", "signal": 80, "security": "WPA3"}]

        assert wifi.connect("Home", "password123") is True

        add = nmcli.called("connection", "add")[0]
        assert add[add.index("wifi-sec.key-mgmt") + 1] == "sae"  # WPA3-only network
        assert "802-11-wireless.hidden" not in add
        assert ["connection", "delete", "preconfigured"] in nmcli.calls
        assert ["connection", "delete", "Other"] not in nmcli.calls
        assert ["connection", "modify", NEW_CONNECTION, "connection.id", "Home"] in nmcli.calls

    def test_connect_failure_keeps_old_connection(self, monkeypatch):
        nmcli = FakeNmcli({
            ("--wait",): (False, "Secrets were required, but not provided"),
            ACTIVE: (True, ""),
            SAVED: (True, "preconfigured:802-11-wireless"),
            SSID: (True, "Home"),
        })
        monkeypatch.setattr(wifi_manager, "run_nmcli", nmcli)
        wifi = WifiManager()

        assert wifi.connect("Home", "wrongpass") is False

        add = nmcli.called("connection", "add")[0]
        assert "802-11-wireless.hidden" in add  # not in the scan results
        assert add[add.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
        assert nmcli.calls[-1] == ["connection", "delete", NEW_CONNECTION]
        assert ["connection", "delete", "preconfigured"] not in nmcli.calls


class FakeWifi:
    def __init__(self, online=False, saved=("Home",), clients=0, connect_ok=True, retry_ok=False):
        self.online = online
        self.saved = list(saved)
        self.clients = clients
        self.connect_ok = connect_ok
        self.retry_ok = retry_ok
        self.networks = [{"ssid": "Home", "signal": 70, "security": "WPA2"}]
        self.hotspot_starts = 0
        self.connect_calls = []
        self.retries = 0

    def is_online(self): return self.online
    def scan(self): return self.networks
    def saved_wifi_connections(self): return self.saved
    def hotspot_client_count(self): return self.clients
    def stop_hotspot(self): pass

    def start_hotspot(self, ssid, password):
        self.hotspot_starts += 1
        return True

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))
        return self.connect_ok

    def try_saved_networks(self):
        self.retries += 1
        return self.retry_ok


@pytest.fixture
def monitor(monkeypatch):
    monkeypatch.setattr(wifi_manager.time, "sleep", lambda seconds: None)
    config = {"resolution": [800, 480], "orientation": "horizontal"}
    device_config = Mock()
    device_config.get_config.side_effect = lambda key, default=None: config.get(key, default)
    device_config.update_value.side_effect = lambda key, value, write=False: config.__setitem__(key, value)
    device_config.get_resolution.return_value = (800, 480)

    monitor = NetworkMonitor(device_config, Mock(), Mock(), wifi=FakeWifi())
    monitor.running = True
    return monitor


class TestNetworkMonitor:

    def test_starts_hotspot_when_offline_at_boot(self, monitor):
        monitor._wait_for_network(0)

        assert monitor.in_setup_mode()
        assert monitor.wifi.hotspot_starts == 1
        monitor.display_manager.display_image.assert_called_once()
        password = monitor.get_hotspot_password()
        assert len(password) == 10 and monitor.get_hotspot_password() == password

    def test_stays_out_of_setup_mode_when_online(self, monitor):
        monitor.wifi.online = True
        monitor._wait_for_network(0)

        assert not monitor.in_setup_mode()
        assert monitor.wifi.hotspot_starts == 0

    def test_connection_request_only_in_setup_mode(self, monitor):
        assert monitor.request_connection("Home", "password123") is False

    def test_successful_connection_leaves_setup_mode(self, monitor):
        monitor._enter_setup_mode()
        assert monitor.request_connection("Home", "password123")

        monitor._check_setup_mode()

        assert monitor.wifi.connect_calls == [("Home", "password123")]
        assert not monitor.in_setup_mode()
        monitor.refresh_task.request_refresh.assert_called_once_with(immediate=False)

    def test_failed_connection_restarts_hotspot_with_error(self, monitor):
        monitor.wifi.connect_ok = False
        monitor._enter_setup_mode()
        monitor.request_connection("Home", "wrongpass")

        monitor._check_setup_mode()

        assert monitor.in_setup_mode()
        assert monitor.wifi.hotspot_starts == 2
        assert "Home" in monitor.get_status()["error"]
        monitor.refresh_task.request_refresh.assert_not_called()

    def test_failed_hotspot_leaves_setup_mode_once_back_online(self, monitor):
        monitor._enter_setup_mode()
        monitor.hotspot_up = False
        monitor.wifi.online = True

        monitor._check_setup_mode()

        assert not monitor.in_setup_mode()
        assert monitor.wifi.hotspot_starts == 1

    def test_failed_new_network_keeps_reconnected_saved_one(self, monitor):
        monitor.wifi.connect_ok = False
        monitor._enter_setup_mode()
        monitor.request_connection("Neighbour", "password123")
        monitor.wifi.online = True  # NetworkManager brought a saved network back meanwhile

        monitor._check_setup_mode()

        assert not monitor.in_setup_mode()
        assert monitor.wifi.hotspot_starts == 1

    def test_retries_saved_networks_and_recovers(self, monitor):
        monitor.wifi.retry_ok = True
        monitor._enter_setup_mode()
        monitor.next_retry = 0

        monitor._check_setup_mode()

        assert monitor.wifi.retries == 1
        assert not monitor.in_setup_mode()
        monitor.refresh_task.request_refresh.assert_called_once_with(immediate=True)

    @pytest.mark.parametrize("clients,saved", [(1, ["Home"]), (0, [])])
    def test_no_retry_while_in_use_or_nothing_saved(self, monitor, clients, saved):
        monitor.wifi.clients = clients
        monitor.wifi.saved = saved
        monitor._enter_setup_mode()
        monitor.next_retry = 0

        monitor._check_setup_mode()

        assert monitor.wifi.retries == 0
        assert monitor.in_setup_mode()


@pytest.fixture
def client():
    from blueprints.wifi import wifi_bp

    app = Flask(__name__, template_folder=os.path.join(SRC_DIR, "templates"))
    app.register_blueprint(wifi_bp)

    @app.route("/")
    def home():
        return "home"

    monitor = Mock()
    monitor.in_setup_mode.return_value = True
    monitor.request_connection.return_value = True
    monitor.get_hotspot_ssid.return_value = "InkyPi-Setup"
    monitor.get_status.return_value = {"setup_mode": True, "connecting_to": None, "error": None,
                                       "networks": [{"ssid": "Home", "signal": 70, "security": "WPA2"}]}
    app.config["NETWORK_MONITOR"] = monitor
    return app.test_client(), monitor


class TestWifiBlueprint:

    def test_redirects_other_hosts_to_setup_page(self, client):
        test_client, _ = client
        response = test_client.get("/generate_204", headers={"Host": "connectivitycheck.gstatic.com"})
        assert response.status_code == 302
        assert response.headers["Location"] == "http://10.42.0.1/wifi"

    def test_no_redirect_when_online(self, client):
        test_client, monitor = client
        monitor.in_setup_mode.return_value = False
        assert test_client.get("/", headers={"Host": "10.42.0.1"}).data == b"home"

    def test_setup_page_lists_networks(self, client):
        test_client, _ = client
        response = test_client.get("/wifi", headers={"Host": "10.42.0.1"})
        assert response.status_code == 200
        assert b"Home" in response.data

    @pytest.mark.parametrize("payload,status", [
        ({"ssid": "Home", "password": "password123"}, 200),
        ({"ssid": "Cafe", "password": ""}, 200),
        ({"ssid": "Home", "password": "short"}, 400),
        ({"ssid": "", "password": "password123"}, 400),
        ({"ssid": "x" * 33, "password": "password123"}, 400),
    ])
    def test_connect_validation(self, client, payload, status):
        test_client, monitor = client
        response = test_client.post("/wifi/connect", json=payload, headers={"Host": "10.42.0.1"})
        assert response.status_code == status
        assert monitor.request_connection.called == (status == 200)

    def test_connect_rejected_when_online(self, client):
        test_client, monitor = client
        monitor.request_connection.return_value = False
        response = test_client.post("/wifi/connect", json={"ssid": "Home", "password": "password123"},
                                    headers={"Host": "10.42.0.1"})
        assert response.status_code == 409
