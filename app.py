"""
Flask dashboard for a bash-driven camera setup.

Both capture_loop.sh (stills) and video_capture.sh (video) run
INDEFINITELY as background processes, managed the same way:
  - started as a detached process group (start_new_session=True) so we
    can cleanly signal the whole group later
  - stdout/stderr redirected to a log file so failures are visible from
    the web UI instead of only in a terminal
  - stopped with SIGTERM, which each script's own trap turns into a
    clean shutdown (finishing/finalizing the current file, not just
    getting killed mid-write)
  - mutually exclusive: starting one always stops the other first,
    since both need exclusive access to the same camera device

Also handles:
  - querying the camera (via v4l2-ctl) for the pixel formats/resolutions/
    framerates it actually supports, so the settings forms can offer
    dropdowns instead of free-text fields
  - deleting individual captured files
  - combining a multi-selected set of video segments (oldest to newest,
    by timestamp) into one file in a separate "combined" section

Run with:
    python app.py
Then visit http://<device-ip>:5000
"""

import io
import json
import os
import re
import signal
import subprocess
import zipfile
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, send_file

# ============================================================================
# Which capture mode should start automatically when this app launches
# (e.g. on boot, via systemd)? Options: "photo", "video", or None.
#
# Photo and video capture can't run at the same time (see start_process
# below, which stops whichever one is running before starting the other),
# so only one of them can be the default here.
# ============================================================================
AUTO_START_MODE = "photo"

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
COMBINED_DIR = os.path.join(BASE_DIR, "combined_videos")
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
    },
    "video": {
        "device": "/dev/video0",
        "resolution": "1280x720",
        "input_format": "mjpeg",
        "fps": "15",
        "bitrate": "1.5M",
        "segment_minutes": 5,
        "target_dir": "video_captures",
    },
}

# Holds the running Popen handle for each managed process (None if stopped)
processes = {"photo": None, "video": None}

# Photo and video capture use the same camera device and cannot run at the
# same time. This maps each mode to "the other one".
OTHER_MODE = {"photo": "video", "video": "photo"}

# Simple one-shot status banner shown at the top of the page, set by routes
# that do something the user should get feedback on (delete, combine).
last_message = None


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
    # Photo and video share one camera and can't run at once -- stop
    # whichever one is currently running before starting this one.
    other = OTHER_MODE[name]
    if is_running(other):
        stop_process(other)
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


# --- Camera format detection ------------------------------------------------
# Maps common V4L2 FOURCC codes (as reported by v4l2-ctl) to the names
# ffmpeg's -input_format option expects. Extend this if your camera reports
# something not listed here -- unmapped codes fall back to lowercase as a
# best guess.
FOURCC_TO_INPUT_FORMAT = {
    "MJPG": "mjpeg",
    "YUYV": "yuyv422",
    "H264": "h264",
    "NV12": "nv12",
    "YU12": "yuv420p",
    "YV12": "yvu420p",
    "GREY": "gray",
    "RGB3": "rgb24",
    "BGR3": "bgr24",
}


def get_camera_formats(device, timeout=4):
    """
    Queries the camera via `v4l2-ctl --list-formats-ext` and returns:
        (formats, error)
    where formats is a nested dict:
        { input_format_name: { "WIDTHxHEIGHT": [fps, fps, ...] } }
    and error is None on success, or a short human-readable reason the
    dropdown couldn't be populated (no camera, tool missing, etc).
    On any failure, formats is {} and the UI falls back to manual text entry.
    """
    if not os.path.exists(device):
        return {}, f"{device} not found (camera not connected)"
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-formats-ext", "-d", device],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {}, "v4l2-ctl not installed (sudo apt install v4l-utils)"
    except subprocess.TimeoutExpired:
        return {}, "timed out querying camera"

    if result.returncode != 0:
        return {}, (result.stderr or "failed to query camera formats").strip()[:200]

    formats = {}
    current_format = None
    current_size = None
    for line in result.stdout.splitlines():
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if m:
            fourcc = m.group(1)
            current_format = FOURCC_TO_INPUT_FORMAT.get(fourcc, fourcc.lower())
            formats.setdefault(current_format, {})
            current_size = None
            continue
        m = re.search(r"Size:\s*Discrete\s*(\d+x\d+)", line)
        if m and current_format:
            current_size = m.group(1)
            formats[current_format].setdefault(current_size, [])
            continue
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and current_format and current_size:
            fps = int(round(float(m.group(1))))
            if fps not in formats[current_format][current_size]:
                formats[current_format][current_size].append(fps)

    for fmt in formats.values():
        for fps_list in fmt.values():
            fps_list.sort(reverse=True)

    if not formats:
        return {}, "camera returned no usable formats"
    return formats, None


# --- File helpers (delete / combine) ----------------------------------------
def safe_delete(directory, filename):
    """Deletes filename from directory, refusing to follow '..' or absolute
    paths outside of it."""
    safe_name = os.path.basename(filename)
    full = os.path.abspath(os.path.join(directory, safe_name))
    if os.path.abspath(directory) != os.path.commonpath([os.path.abspath(directory), full]):
        return False
    if os.path.isfile(full):
        os.remove(full)
        return True
    return False


def file_sort_key(filepath):
    """Sorts by the YYYYMMDD_HHMMSS timestamp embedded in the filename if
    present, else falls back to the file's modification time."""
    fname = os.path.basename(filepath)
    m = re.match(r"^(\d{8}_\d{6})", fname)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(os.path.getmtime(filepath))


def delete_selected_files(directory, filenames):
    """Deletes each of filenames from directory. Returns (deleted_count, total_count)."""
    deleted = 0
    for name in filenames:
        if safe_delete(directory, name):
            deleted += 1
    return deleted, len(filenames)


def build_zip_response(directory, filenames, download_name):
    """Zips the given filenames (must exist directly inside directory) in
    memory and returns a Flask response that downloads them as one file.
    Returns None (with last_message set) if nothing valid was selected."""
    global last_message
    valid_paths = []
    for name in filenames:
        full = os.path.join(directory, os.path.basename(name))
        if os.path.isfile(full):
            valid_paths.append(full)

    if not valid_paths:
        last_message = {"level": "error", "text": "No valid files selected to download."}
        return None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in valid_paths:
            zf.write(fp, arcname=os.path.basename(fp))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=download_name)


# --- Routes ------------------------------------------------------------
@app.route("/")
def index():
    global last_message
    config = load_config()
    photo_dir = os.path.join(BASE_DIR, config["photo"]["target_dir"])
    video_dir = os.path.join(BASE_DIR, config["video"]["target_dir"])
    photo_files = sorted(os.listdir(photo_dir))[-50:] if os.path.isdir(photo_dir) else []
    video_files = sorted(os.listdir(video_dir))[-50:] if os.path.isdir(video_dir) else []
    combined_files = sorted(os.listdir(COMBINED_DIR))[-50:] if os.path.isdir(COMBINED_DIR) else []

    photo_formats, photo_formats_err = get_camera_formats(config["photo"]["device"])
    video_formats, video_formats_err = get_camera_formats(config["video"]["device"])

    message = last_message
    last_message = None

    return render_template(
        "index.html",
        config=config,
        photo_running=is_running("photo"),
        video_running=is_running("video"),
        photo_log=tail_log(LOG_PATHS["photo"]),
        video_log=tail_log(LOG_PATHS["video"]),
        photo_files=photo_files,
        video_files=video_files,
        combined_files=combined_files,
        photo_formats=photo_formats,
        photo_formats_err=photo_formats_err,
        video_formats=video_formats,
        video_formats_err=video_formats_err,
        message=message,
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


@app.route("/photo/delete", methods=["POST"])
def delete_photo():
    global last_message
    config = load_config()
    filename = request.form.get("filename", "")
    directory = os.path.join(BASE_DIR, config["photo"]["target_dir"])
    if safe_delete(directory, filename):
        last_message = {"level": "success", "text": f"Deleted {filename}"}
    else:
        last_message = {"level": "error", "text": f"Could not delete {filename}"}
    return redirect(url_for("index"))


@app.route("/photo/delete_selected", methods=["POST"])
def delete_selected_photos():
    global last_message
    config = load_config()
    directory = os.path.join(BASE_DIR, config["photo"]["target_dir"])
    selected = request.form.getlist("selected")
    deleted, total = delete_selected_files(directory, selected)
    if total == 0:
        last_message = {"level": "error", "text": "Select at least one photo first."}
    else:
        last_message = {
            "level": "success" if deleted == total else "error",
            "text": f"Deleted {deleted} of {total} selected photo(s).",
        }
    return redirect(url_for("index"))


@app.route("/photo/download_selected", methods=["POST"])
def download_selected_photos():
    config = load_config()
    directory = os.path.join(BASE_DIR, config["photo"]["target_dir"])
    selected = request.form.getlist("selected")
    response = build_zip_response(directory, selected, "photos_selected.zip")
    return response if response is not None else redirect(url_for("index"))


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


@app.route("/video/delete", methods=["POST"])
def delete_video():
    global last_message
    config = load_config()
    filename = request.form.get("filename", "")
    directory = os.path.join(BASE_DIR, config["video"]["target_dir"])
    if safe_delete(directory, filename):
        last_message = {"level": "success", "text": f"Deleted {filename}"}
    else:
        last_message = {"level": "error", "text": f"Could not delete {filename}"}
    return redirect(url_for("index"))


@app.route("/video/delete_selected", methods=["POST"])
def delete_selected_videos():
    global last_message
    config = load_config()
    directory = os.path.join(BASE_DIR, config["video"]["target_dir"])
    selected = request.form.getlist("selected")
    deleted, total = delete_selected_files(directory, selected)
    if total == 0:
        last_message = {"level": "error", "text": "Select at least one video first."}
    else:
        last_message = {
            "level": "success" if deleted == total else "error",
            "text": f"Deleted {deleted} of {total} selected video(s).",
        }
    return redirect(url_for("index"))


@app.route("/video/download_selected", methods=["POST"])
def download_selected_videos():
    config = load_config()
    directory = os.path.join(BASE_DIR, config["video"]["target_dir"])
    selected = request.form.getlist("selected")
    response = build_zip_response(directory, selected, "videos_selected.zip")
    return response if response is not None else redirect(url_for("index"))


@app.route("/video/combine", methods=["POST"])
def combine_videos():
    global last_message
    selected = request.form.getlist("selected")
    if len(selected) < 2:
        last_message = {"level": "error", "text": "Select at least 2 videos to combine."}
        return redirect(url_for("index"))

    config = load_config()
    video_dir = os.path.join(BASE_DIR, config["video"]["target_dir"])
    filepaths = []
    for name in selected:
        full = os.path.join(video_dir, os.path.basename(name))
        if os.path.isfile(full):
            filepaths.append(full)

    if len(filepaths) < 2:
        last_message = {"level": "error", "text": "Could not find the selected video files."}
        return redirect(url_for("index"))

    filepaths.sort(key=file_sort_key)  # oldest to newest

    os.makedirs(COMBINED_DIR, exist_ok=True)
    first_ts = file_sort_key(filepaths[0]).strftime("%Y%m%d_%H%M%S")
    last_ts = file_sort_key(filepaths[-1]).strftime("%Y%m%d_%H%M%S")
    output_name = f"combined_{first_ts}_to_{last_ts}.mp4"
    output_path = os.path.join(COMBINED_DIR, output_name)
    list_path = os.path.join(COMBINED_DIR, f".concat_{first_ts}.txt")

    try:
        with open(list_path, "w") as f:
            for fp in filepaths:
                escaped = fp.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # Fast path: if all segments share the same codec/params, a plain
        # stream copy works and loses no quality.
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path],
            capture_output=True, text=True, timeout=300,
        )

        if result.returncode != 0:
            # Fallback: re-encode via filter_complex concat, which tolerates
            # segments with different resolutions/framerates/bitrates.
            inputs = []
            filter_parts = []
            for i, fp in enumerate(filepaths):
                inputs += ["-i", fp]
                filter_parts.append(f"[{i}:v]")
            filter_str = "".join(filter_parts) + f"concat=n={len(filepaths)}:v=1:a=0[outv]"
            result2 = subprocess.run(
                ["ffmpeg", "-y"] + inputs + [
                    "-filter_complex", filter_str, "-map", "[outv]",
                    "-c:v", "libx264", "-preset", "veryfast", output_path,
                ],
                capture_output=True, text=True, timeout=600,
            )
            if result2.returncode != 0:
                err_tail = (result2.stderr or "unknown error")[-400:]
                last_message = {"level": "error", "text": f"Combine failed: {err_tail}"}
                return redirect(url_for("index"))

        last_message = {
            "level": "success",
            "text": f"Combined {len(filepaths)} videos (oldest \u2192 newest) into {output_name}",
        }
    except subprocess.TimeoutExpired:
        last_message = {"level": "error", "text": "Combining timed out."}
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)

    return redirect(url_for("index"))


@app.route("/combined/delete", methods=["POST"])
def delete_combined():
    global last_message
    filename = request.form.get("filename", "")
    if safe_delete(COMBINED_DIR, filename):
        last_message = {"level": "success", "text": f"Deleted {filename}"}
    else:
        last_message = {"level": "error", "text": f"Could not delete {filename}"}
    return redirect(url_for("index"))


@app.route("/combined/delete_selected", methods=["POST"])
def delete_selected_combined():
    global last_message
    selected = request.form.getlist("selected")
    deleted, total = delete_selected_files(COMBINED_DIR, selected)
    if total == 0:
        last_message = {"level": "error", "text": "Select at least one combined video first."}
    else:
        last_message = {
            "level": "success" if deleted == total else "error",
            "text": f"Deleted {deleted} of {total} selected combined video(s).",
        }
    return redirect(url_for("index"))


@app.route("/combined/download_selected", methods=["POST"])
def download_selected_combined():
    selected = request.form.getlist("selected")
    response = build_zip_response(COMBINED_DIR, selected, "combined_selected.zip")
    return response if response is not None else redirect(url_for("index"))


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


@app.route("/downloads/combined/<path:filename>")
def download_combined(filename):
    return send_from_directory(COMBINED_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    cfg = load_config()
    os.makedirs(os.path.join(BASE_DIR, cfg["photo"]["target_dir"]), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, cfg["video"]["target_dir"]), exist_ok=True)
    os.makedirs(COMBINED_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    if AUTO_START_MODE in ("photo", "video"):
        start_process(AUTO_START_MODE, cfg)
    try:
        # threaded=True so a slow combine operation doesn't block status
        # checks or other requests while it runs.
        app.run(host="0.0.0.0", port=5020, debug=False, threaded=True)
    finally:
        stop_process("photo")
        stop_process("video")