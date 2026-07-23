"""
Flask dashboard for a bash-driven camera setup.

Both capture_loop.sh (stills) and video_capture.sh (video) now run
INDEFINITELY as background processes, managed the same way:
  - started as a detached process group (start_new_session=True) so we
    can cleanly signal the whole group later
  - stdout/stderr redirected to a log file so failures are visible from
    the web UI instead of only in a terminal
  - stopped with SIGTERM, which each script's own trap turns into a
    clean shutdown (finishing/finalizing the current file, not just
    getting killed mid-write)

Designed for "runs from boot, powered off randomly":
  - Both scripts retry indefinitely on internal failure (camera not
    ready yet, transient error) rather than exiting.
  - Video is written in short, self-contained segments so a sudden power
    loss only damages the segment that was open, not the whole session.
  - Still photos are written to a temp name and only renamed into place
    after a successful capture, so a half-written photo is never left
    looking like a finished one.
  - If the whole device loses power, everything dies together and there
    is nothing Python can do about that -- see the systemd unit example
    at the bottom of this file's accompanying README for auto-start on
    boot, so the app (and hence these loops, via auto_start) comes back
    up on its own after a reboot.

Run with:
    python app.py
Then visit http://<device-ip>:5000
"""

import json
import os
import signal
import subprocess
from flask import Flask, render_template, request, redirect, url_for, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
SCRIPTS = {
    "photo": os.path.join(BASE_DIR, "capture_loop.sh"),
    "video": os.path.join(BASE_DIR, "video_capture.sh"),
}
LOG_PATHS = {
    "photo": os.path.join(LOG_DIR, "photo.log"),
    "video": os.path.join(LOG_DIR, "video.log"),
}
LOG_MAX_BYTES = 2 * 1024 * 1024   # rotate once a log passes 2MB
LOG_KEEP_BYTES = 256 * 1024       # keep the last 256KB when rotating
LOG_TAIL_LINES = 30               # lines shown in the UI

DEFAULT_CONFIG = {
    "photo": {
        "device": "/dev/video0",
        "resolution": "1920x1080",
        "input_format": "mjpeg",
        "interval": 15,
        "warmup_frames": 5,
        "target_dir": "captures",
        "auto_start": True,
    },
    "video": {
        "device": "/dev/video0",
        "resolution": "1280x720",
        "input_format": "mjpeg",
        "fps": "15",           # "15" or "10"
        "bitrate": "1.5M",
        "segment_minutes": 5,
        "target_dir": "video_captures",
        "auto_start": True,
    },
}

# Holds the running Popen handle for each managed process (None if stopped)
processes = {"photo": None, "video": None}


# --- Config helpers -------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r") as f:
        data = json.load(f)
    for section, defaults in DEFAULT_CONFIG.items():
        data.setdefault(section, {})
        for k, v in defaults.items():
            data[section].setdefault(k, v)
    return data


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


# --- Log helpers -----------------------------------------------------------
def rotate_log_if_large(path):
    if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
        with open(path, "rb") as f:
            f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
            tail = f.read()
        with open(path, "wb") as f:
            f.write(b"--- log rotated ---\n")
            f.write(tail)


def tail_log(path, lines=LOG_TAIL_LINES):
    if not os.path.exists(path):
        return "(no log yet)"
    with open(path, "r", errors="replace") as f:
        content = f.readlines()
    return "".join(content[-lines:]) or "(empty)"


# --- Process management (shared by photo + video) --------------------------
def is_running(name):
    proc = processes.get(name)
    return proc is not None and proc.poll() is None


def build_env(name, config):
    env = os.environ.copy()
    if name == "photo":
        p = config["photo"]
        env.update({
            "DEVICE": p["device"],
            "RESOLUTION": p["resolution"],
            "INPUT_FORMAT": p["input_format"],
            "INTERVAL": str(p["interval"]),
            "WARMUP_FRAMES": str(p["warmup_frames"]),
            "TARGET_DIR": os.path.join(BASE_DIR, p["target_dir"]),
        })
    else:
        v = config["video"]
        env.update({
            "DEVICE": v["device"],
            "RESOLUTION": v["resolution"],
            "INPUT_FORMAT": v["input_format"],
            "FPS": str(v["fps"]),
            "BITRATE": v["bitrate"],
            "SEGMENT_SECONDS": str(int(v["segment_minutes"]) * 60),
            "TARGET_DIR": os.path.join(BASE_DIR, v["target_dir"]),
        })
    return env


def start_process(name, config):
    if is_running(name):
        return
    log_path = LOG_PATHS[name]
    os.makedirs(LOG_DIR, exist_ok=True)
    rotate_log_if_large(log_path)
    env = build_env(name, config)
    with open(log_path, "a") as log_f:
        log_f.write(f"\n--- starting {name} process ---\n")
        log_f.flush()
        proc = subprocess.Popen(
            ["bash", SCRIPTS[name]],
            env=env,
            cwd=BASE_DIR,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # own process group -> clean group signaling
        )
    processes[name] = proc


def stop_process(name):
    proc = processes.get(name)
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    processes[name] = None


def restart_process(name, config):
    stop_process(name)
    start_process(name, config)


# --- Routes ------------------------------------------------------------
@app.route("/")
def index():
    config = load_config()
    photo_dir = os.path.join(BASE_DIR, config["photo"]["target_dir"])
    video_dir = os.path.join(BASE_DIR, config["video"]["target_dir"])
    photo_files = sorted(os.listdir(photo_dir))[-50:] if os.path.isdir(photo_dir) else []
    video_files = sorted(os.listdir(video_dir))[-50:] if os.path.isdir(video_dir) else []
    return render_template(
        "index.html",
        config=config,
        photo_running=is_running("photo"),
        video_running=is_running("video"),
        photo_log=tail_log(LOG_PATHS["photo"]),
        video_log=tail_log(LOG_PATHS["video"]),
        photo_files=photo_files,
        video_files=video_files,
    )


@app.route("/photo/settings", methods=["POST"])
def update_photo_settings():
    config = load_config()
    p = config["photo"]
    p["device"] = request.form.get("device", p["device"])
    p["resolution"] = request.form.get("resolution", p["resolution"])
    p["input_format"] = request.form.get("input_format", p["input_format"])
    p["target_dir"] = request.form.get("target_dir", p["target_dir"])
    try:
        p["interval"] = int(request.form.get("interval", p["interval"]))
    except ValueError:
        pass
    try:
        p["warmup_frames"] = int(request.form.get("warmup_frames", p["warmup_frames"]))
    except ValueError:
        pass
    save_config(config)
    restart_process("photo", config)
    return redirect(url_for("index"))


@app.route("/photo/start", methods=["POST"])
def start_photo():
    start_process("photo", load_config())
    return redirect(url_for("index"))


@app.route("/photo/stop", methods=["POST"])
def stop_photo():
    stop_process("photo")
    return redirect(url_for("index"))


@app.route("/video/settings", methods=["POST"])
def update_video_settings():
    config = load_config()
    v = config["video"]
    v["device"] = request.form.get("device", v["device"])
    v["resolution"] = request.form.get("resolution", v["resolution"])
    v["input_format"] = request.form.get("input_format", v["input_format"])
    v["fps"] = request.form.get("fps", v["fps"])
    v["bitrate"] = request.form.get("bitrate", v["bitrate"])
    v["target_dir"] = request.form.get("target_dir", v["target_dir"])
    try:
        v["segment_minutes"] = int(request.form.get("segment_minutes", v["segment_minutes"]))
    except ValueError:
        pass
    save_config(config)
    restart_process("video", config)
    return redirect(url_for("index"))


@app.route("/video/start", methods=["POST"])
def start_video():
    start_process("video", load_config())
    return redirect(url_for("index"))


@app.route("/video/stop", methods=["POST"])
def stop_video():
    stop_process("video")
    return redirect(url_for("index"))


@app.route("/downloads/photo/<path:filename>")
def download_photo(filename):
    config = load_config()
    return send_from_directory(
        os.path.join(BASE_DIR, config["photo"]["target_dir"]), filename, as_attachment=True
    )


@app.route("/downloads/video/<path:filename>")
def download_video(filename):
    config = load_config()
    return send_from_directory(
        os.path.join(BASE_DIR, config["video"]["target_dir"]), filename, as_attachment=True
    )


if __name__ == "__main__":
    cfg = load_config()
    os.makedirs(os.path.join(BASE_DIR, cfg["photo"]["target_dir"]), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, cfg["video"]["target_dir"]), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    if cfg["photo"].get("auto_start"):
        start_process("photo", cfg)
    if cfg["video"].get("auto_start"):
        start_process("video", cfg)
    try:
        app.run(host="0.0.0.0", port=5020, debug=False)
    finally:
        stop_process("photo")
        stop_process("video")