"""WiFi fallback: start a setup hotspot when the device has no network.

Uses NetworkManager (nmcli), the default network stack on Raspberry Pi OS
Bookworm and later. When the device stays offline, NetworkMonitor starts a
hotspot and shows its name, password and a QR code on the display. Joining the
hotspot opens the setup page (see blueprints/wifi.py) where new WiFi details
can be entered. While the hotspot is up, saved networks are retried
periodically, so a router outage recovers without any user action.
"""

import logging
import os
import secrets
import shutil
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

WIFI_INTERFACE = "wlan0"
HOTSPOT_CONNECTION = "inkypi-hotspot"
HOTSPOT_SSID = "InkyPi-Setup"
# NetworkManager's default address for shared connections; the installer
# points every DNS name on the hotspot to it so phones open the setup page.
HOTSPOT_IP = "10.42.0.1"
NEW_CONNECTION = "inkypi-new-wifi"

STARTUP_GRACE_SECONDS = 90       # time NetworkManager gets to connect after boot
OFFLINE_GRACE_SECONDS = 180      # how long a connection can drop before the hotspot starts
CHECK_INTERVAL_SECONDS = 30
RETRY_SAVED_SECONDS = 300        # how often saved networks are retried from the hotspot
CONNECT_TIMEOUT_SECONDS = 45
HANDOVER_DELAY_SECONDS = 3       # lets the setup page receive its reply before the hotspot stops

PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
PASSWORD_LENGTH = 10

WIFI_TYPES = ("802-11-wireless", "wifi")
ETHERNET_TYPES = ("802-3-ethernet", "ethernet")
SECRET_ARGS = ("wifi-sec.psk", "802-11-wireless-security.psk")


def split_terse(line):
    """Split a line of `nmcli -t` output, honouring its backslash escapes."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def redact(args):
    """Return nmcli arguments with passwords hidden, for logging."""
    redacted = list(args)
    for i, arg in enumerate(redacted[:-1]):
        if arg in SECRET_ARGS:
            redacted[i + 1] = "******"
    return " ".join(redacted)


def run_nmcli(args, timeout=15):
    """Run nmcli and return (success, output). Never raises."""
    try:
        result = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"nmcli timed out | command: nmcli {redact(args)}")
        return False, "timeout"
    except OSError as e:
        logger.error(f"Failed to run nmcli: {e}")
        return False, str(e)

    if result.returncode != 0:
        logger.debug(f"nmcli failed | command: nmcli {redact(args)} | error: {result.stderr.strip()}")
        return False, result.stderr.strip()
    return True, result.stdout.strip()


class WifiManager:
    """Thin wrapper around the nmcli commands needed for the setup hotspot."""

    def __init__(self, interface=WIFI_INTERFACE):
        self.interface = interface
        self.networks = []  # last scan result, kept for the setup page while the hotspot is up

    @staticmethod
    def is_supported(interface=WIFI_INTERFACE):
        return shutil.which("nmcli") is not None and os.path.exists(f"/sys/class/net/{interface}")

    def is_online(self):
        """True when a WiFi or Ethernet connection, other than the hotspot, is active."""
        ok, output = run_nmcli(["-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"])
        if not ok:
            return False
        for line in output.splitlines():
            name, conn_type, state = (split_terse(line) + ["", "", ""])[:3]
            if name == HOTSPOT_CONNECTION:
                continue
            if conn_type in WIFI_TYPES + ETHERNET_TYPES and state == "activated":
                return True
        return False

    def saved_wifi_connections(self):
        """Names of the saved WiFi connections, excluding the hotspot."""
        ok, output = run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
        if not ok:
            return []
        names = []
        for line in output.splitlines():
            name, conn_type = (split_terse(line) + ["", ""])[:2]
            if conn_type in WIFI_TYPES and name not in (HOTSPOT_CONNECTION, NEW_CONNECTION):
                names.append(name)
        return names

    def connection_ssid(self, name):
        ok, output = run_nmcli(["-t", "-g", "802-11-wireless.ssid", "connection", "show", name])
        return split_terse(output)[0] if ok else None

    def scan(self):
        """Scan for nearby networks. Returns dicts with ssid, signal and security, strongest first."""
        ok, output = run_nmcli(
            ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
             "ifname", self.interface, "--rescan", "yes"],
            timeout=30,
        )
        if not ok:
            logger.warning("WiFi scan failed")
            return self.networks

        networks = {}
        for line in output.splitlines():
            ssid, signal, security = (split_terse(line) + ["", "", ""])[:3]
            if not ssid:
                continue  # hidden network
            try:
                signal = int(signal)
            except ValueError:
                signal = 0
            if ssid not in networks or signal > networks[ssid]["signal"]:
                networks[ssid] = {"ssid": ssid, "signal": signal, "security": security.strip()}

        self.networks = sorted(networks.values(), key=lambda n: n["signal"], reverse=True)
        return self.networks

    def start_hotspot(self, ssid, password):
        run_nmcli(["connection", "delete", HOTSPOT_CONNECTION])
        ok, output = run_nmcli([
            "connection", "add", "type", "wifi", "ifname", self.interface,
            "con-name", HOTSPOT_CONNECTION, "autoconnect", "no", "ssid", ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv4.addresses", f"{HOTSPOT_IP}/24",
            "ipv6.method", "disabled",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
            # WPA2-only with protected management frames off: the combination
            # phones connect to most reliably on the Pi's WiFi chip.
            "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
            "wifi-sec.pmf", "disable",
        ])
        if not ok:
            logger.error(f"Failed to create hotspot connection: {output}")
            return False

        ok, output = run_nmcli(["connection", "up", HOTSPOT_CONNECTION], timeout=30)
        if not ok:
            logger.error(f"Failed to start hotspot: {output}")
            return False
        logger.info(f"Setup hotspot started | ssid: {ssid}")
        return True

    def stop_hotspot(self):
        run_nmcli(["connection", "down", HOTSPOT_CONNECTION], timeout=20)

    def hotspot_client_count(self):
        """Number of devices connected to the hotspot (0 if unknown)."""
        try:
            result = subprocess.run(["iw", "dev", self.interface, "station", "dump"],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return 0
        return sum(1 for line in result.stdout.splitlines() if line.startswith("Station"))

    def try_saved_networks(self):
        """Try each saved network that is in range. The hotspot must be stopped first."""
        visible = {network["ssid"] for network in self.scan()}
        for name in self.saved_wifi_connections():
            if self.connection_ssid(name) not in visible:
                continue
            logger.info(f"Trying saved WiFi connection | name: {name}")
            ok, _ = run_nmcli(["--wait", str(CONNECT_TIMEOUT_SECONDS), "connection", "up", name],
                              timeout=CONNECT_TIMEOUT_SECONDS + 15)
            if ok and self.is_online():
                return True
        return self.is_online()

    def connect(self, ssid, password):
        """Save and activate a new WiFi network. The hotspot must be stopped first.

        The new connection replaces saved connections to the same network only
        once it works, so a mistyped password never loses a working one.
        """
        network = next((n for n in self.networks if n["ssid"] == ssid), None)

        run_nmcli(["connection", "delete", NEW_CONNECTION])
        args = ["connection", "add", "type", "wifi", "ifname", self.interface,
                "con-name", NEW_CONNECTION, "autoconnect", "yes", "ssid", ssid]
        if network is None:
            args += ["802-11-wireless.hidden", "yes"]
        if password:
            security = network["security"] if network else ""
            wpa3_only = "WPA3" in security and "WPA2" not in security and "WPA1" not in security
            args += ["wifi-sec.key-mgmt", "sae" if wpa3_only else "wpa-psk", "wifi-sec.psk", password]

        ok, output = run_nmcli(args)
        if not ok:
            logger.error(f"Failed to save WiFi connection: {output}")
            return False

        ok, output = run_nmcli(["--wait", str(CONNECT_TIMEOUT_SECONDS), "connection", "up", NEW_CONNECTION],
                               timeout=CONNECT_TIMEOUT_SECONDS + 15)
        if not (ok and self.is_online()):
            logger.warning(f"Failed to connect to WiFi | ssid: {ssid} | error: {output}")
            run_nmcli(["connection", "delete", NEW_CONNECTION])
            return False

        for name in self.saved_wifi_connections():
            if self.connection_ssid(name) == ssid:
                run_nmcli(["connection", "delete", name])
        run_nmcli(["connection", "modify", NEW_CONNECTION, "connection.id", ssid])
        logger.info(f"Connected to WiFi | ssid: {ssid}")
        return True


class NetworkMonitor:
    """Background thread that falls back to the setup hotspot while offline."""

    def __init__(self, device_config, display_manager, refresh_task, wifi=None):
        self.device_config = device_config
        self.display_manager = display_manager
        self.refresh_task = refresh_task
        self.wifi = wifi or WifiManager()

        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.running = False

        self.setup_mode = False
        self.hotspot_up = False
        self.next_retry = 0
        self.pending_connection = None
        self.connecting_to = None
        self.last_error = None

    @staticmethod
    def is_supported():
        return WifiManager.is_supported()

    def start(self):
        if not self.thread or not self.thread.is_alive():
            logger.info("Starting network monitor")
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        self.wake.set()

    def in_setup_mode(self):
        return self.setup_mode

    def get_hotspot_ssid(self):
        return HOTSPOT_SSID

    def get_hotspot_password(self):
        password = self.device_config.get_config("wifi_setup_password", default=None)
        if not password:
            password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))
            self.device_config.update_value("wifi_setup_password", password, write=True)
        return password

    def get_status(self):
        with self.lock:
            return {
                "setup_mode": self.setup_mode,
                "connecting_to": self.connecting_to,
                "error": self.last_error,
                "networks": list(self.wifi.networks),
            }

    def request_connection(self, ssid, password):
        """Queue new WiFi details from the setup page. Returns False outside setup mode."""
        with self.lock:
            if not self.setup_mode:
                return False
            self.pending_connection = (ssid, password)
            self.connecting_to = ssid
            self.last_error = None
        self.wake.set()
        return True

    def _run(self):
        try:
            self._wait_for_network(STARTUP_GRACE_SECONDS)
        except Exception:
            logger.exception("Error while waiting for the network at startup")

        offline_since = None
        while self.running:
            self.wake.clear()
            try:
                if self.setup_mode:
                    self._check_setup_mode()
                elif self.wifi.is_online():
                    offline_since = None
                else:
                    offline_since = offline_since or time.monotonic()
                    if time.monotonic() - offline_since >= OFFLINE_GRACE_SECONDS:
                        logger.warning("Network lost, starting WiFi setup hotspot")
                        self._enter_setup_mode()
                        offline_since = None
            except Exception:
                logger.exception("Error in network monitor")
            self.wake.wait(CHECK_INTERVAL_SECONDS)

    def _wait_for_network(self, timeout):
        deadline = time.monotonic() + timeout
        while self.running:
            if self.wifi.is_online():
                logger.info("Network is up")
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(5)
        if self.running:
            logger.warning(f"No network after {timeout}s, starting WiFi setup hotspot")
            self._enter_setup_mode()

    def _check_setup_mode(self):
        with self.lock:
            pending, self.pending_connection = self.pending_connection, None

        if pending:
            self._connect(*pending)
        elif not self.hotspot_up:
            # The hotspot failed to start, which leaves NetworkManager free to reconnect by itself.
            if self.wifi.is_online():
                self._leave_setup_mode(user_initiated=False)
            else:
                self._start_hotspot()
        elif time.monotonic() >= self.next_retry:
            self.next_retry = time.monotonic() + RETRY_SAVED_SECONDS
            # Don't pull the hotspot from under someone who is using the setup page.
            if self.wifi.hotspot_client_count() == 0 and self.wifi.saved_wifi_connections():
                self._retry_saved_networks()

    def _enter_setup_mode(self):
        # Scan first: once the radio runs the hotspot it can't see other networks.
        self.wifi.scan()
        with self.lock:
            self.setup_mode = True
        self.next_retry = time.monotonic() + RETRY_SAVED_SECONDS
        self._start_hotspot()
        self._show_setup_screen()

    def _start_hotspot(self):
        self.hotspot_up = self.wifi.start_hotspot(HOTSPOT_SSID, self.get_hotspot_password())

    def _stop_hotspot(self):
        self.wifi.stop_hotspot()
        self.hotspot_up = False
        time.sleep(HANDOVER_DELAY_SECONDS)

    def _retry_saved_networks(self):
        logger.info("Retrying saved WiFi networks")
        self._stop_hotspot()
        if self.wifi.try_saved_networks():
            self._leave_setup_mode(user_initiated=False)
        else:
            logger.info("Saved WiFi networks unavailable, restarting setup hotspot")
            self._start_hotspot()

    def _connect(self, ssid, password):
        logger.info(f"Connecting to WiFi from setup page | ssid: {ssid}")
        time.sleep(HANDOVER_DELAY_SECONDS)
        threading.Thread(target=self._show_message, args=(f"Connecting to {ssid}…",), daemon=True).start()
        self._stop_hotspot()
        self.wifi.scan()

        if self.wifi.connect(ssid, password):
            self._leave_setup_mode(user_initiated=True)
            return
        if self.wifi.is_online():
            # NetworkManager reconnected to another saved network meanwhile; keep it.
            logger.info("New WiFi network failed, but a saved network connected")
            self._leave_setup_mode(user_initiated=True)
            return

        with self.lock:
            self.connecting_to = None
            self.last_error = f'Could not connect to "{ssid}". Check the password and try again.'
        self._start_hotspot()
        self._show_setup_screen()

    def _leave_setup_mode(self, user_initiated):
        with self.lock:
            self.setup_mode = False
            self.connecting_to = None
            self.last_error = None
        logger.info("Network is back, leaving WiFi setup mode")

        if user_initiated:
            # Show the new address first; the next scheduled refresh replaces it.
            from utils.app_utils import generate_startup_image
            self._display(generate_startup_image(self._dimensions()))
            self.refresh_task.request_refresh(immediate=False)
        else:
            self.refresh_task.request_refresh(immediate=True)

    def _show_setup_screen(self):
        from utils.app_utils import generate_wifi_setup_image
        with self.lock:
            error = self.last_error
        image = generate_wifi_setup_image(
            self._dimensions(), HOTSPOT_SSID, self.get_hotspot_password(),
            f"http://{HOTSPOT_IP}", error=error,
        )
        self._display(image)

    def _show_message(self, message):
        from utils.app_utils import generate_message_image
        self._display(generate_message_image(self._dimensions(), message))

    def _display(self, image):
        try:
            self.display_manager.display_image(image)
        except Exception:
            logger.exception("Failed to update the display")

    def _dimensions(self):
        dimensions = self.device_config.get_resolution()
        if self.device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        return dimensions
