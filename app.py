"""
Minimal Flask dashboard for a bash-driven camera capture setup.

Run with:
    python app.py

Then visit http://<device-ip>:5000 in a browser.
"""

import json
import os
import subprocess
from flask import Flask, render_template, request, redirect, url_for, send_from_directory

app = Flask(__name__)

# --- Paths -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CAPTURE_DIR = os.path.join(BASE_DIR, "captures")
CAPTURE_SCRIPT = os.path.join(BASE_DIR, "capture.sh")

# --- Config helpers ------------------------------------------------------
DEFAULT_CONFIG = {
    "resolution": "1920x1080",
    "interval_seconds": 60,
    "capture_count": 0,   # example "variable" that updates over time
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


# --- Routes --------------------------------------------------------------
@app.route("/")
def index():
    config = load_config()
    files = sorted(os.listdir(CAPTURE_DIR)) if os.path.isdir(CAPTURE_DIR) else []
    return render_template("index.html", config=config, files=files)


@app.route("/capture", methods=["POST"])
def trigger_capture():
    """Runs the bash capture script as a subprocess (no shell=True)."""
    config = load_config()
    try:
        subprocess.run(["bash", CAPTURE_SCRIPT], check=True, timeout=30)
        config["capture_count"] += 1
        save_config(config)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"Capture failed: {e}")
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def update_settings():
    config = load_config()
    config["resolution"] = request.form.get("resolution", config["resolution"])
    try:
        config["interval_seconds"] = int(request.form.get("interval_seconds", config["interval_seconds"]))
    except ValueError:
        pass
    save_config(config)
    return redirect(url_for("index"))


@app.route("/downloads/<path:filename>")
def download_file(filename):
    return send_from_directory(CAPTURE_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    # debug=False is safer if this device is reachable on your network
    app.run(host="0.0.0.0", port=5020, debug=False)