import socket

from flask import Blueprint, request, jsonify, current_app, render_template, redirect

from utils.wifi_manager import HOTSPOT_IP

wifi_bp = Blueprint("wifi", __name__)

SETUP_PAGE_URL = f"http://{HOTSPOT_IP}/wifi"


@wifi_bp.before_app_request
def redirect_to_setup_page():
    """While the setup hotspot runs, send every page request to the setup page.

    The hotspot answers every DNS name with the device's address, so a phone's
    connectivity check (e.g. captive.apple.com, connectivitycheck.gstatic.com)
    lands here and the phone opens the setup page by itself.
    """
    monitor = current_app.config.get("NETWORK_MONITOR")
    if not monitor or not monitor.in_setup_mode():
        return None

    host = request.host.split(":")[0].lower()
    hostname = socket.gethostname().lower()
    own_host = host in (HOTSPOT_IP, hostname, f"{hostname}.local", "localhost", "127.0.0.1")
    if not own_host or request.path == "/":
        return redirect(SETUP_PAGE_URL, code=302)
    return None


@wifi_bp.route("/wifi")
def wifi_page():
    monitor = current_app.config.get("NETWORK_MONITOR")
    if not monitor:
        return render_template("wifi_setup.html", supported=False, status={}, hotspot_ssid="")
    return render_template("wifi_setup.html", supported=True, status=monitor.get_status(),
                           hotspot_ssid=monitor.get_hotspot_ssid())


@wifi_bp.route("/wifi/status")
def wifi_status():
    monitor = current_app.config.get("NETWORK_MONITOR")
    if not monitor:
        return jsonify({"supported": False})
    return jsonify({"supported": True, **monitor.get_status()})


@wifi_bp.route("/wifi/connect", methods=["POST"])
def wifi_connect():
    monitor = current_app.config.get("NETWORK_MONITOR")
    if not monitor:
        return jsonify({"error": "WiFi setup is not available on this device."}), 404

    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid", "")).strip()
    password = str(data.get("password", ""))

    if not ssid or len(ssid.encode("utf-8")) > 32:
        return jsonify({"error": "Enter a network name (up to 32 characters)."}), 400
    if password and not 8 <= len(password) <= 63:
        return jsonify({"error": "WiFi passwords are 8 to 63 characters long."}), 400

    if not monitor.request_connection(ssid, password):
        return jsonify({"error": "The device is already connected to a network."}), 409

    return jsonify({
        "success": True,
        "message": f'Connecting to "{ssid}". This page will now disconnect. '
                   f'Watch the InkyPi screen: it shows its new address once connected, '
                   f'or this setup screen again if something went wrong.'
    })
