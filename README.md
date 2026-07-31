# CTDCamera

## Breif
This project uitizes a Raspberry Pi 02W to record either photos or video from a connected webcam. 
This is intended to be mounted on the CTD.

The goal of this project was to develop an underwater camera using off the shelf components including a Raspberry Pi and a disassembled webcam to reduce cost.

This cheap power efficient camera system will be housed in a reused pressure chamber, originally designed to be a photo flash, with modifications to the housing to allow it to connect to the FISH.

The camera system’s goal is to capture either video/photos of the water (and occasionally seafloor) as the CTD descends and acends.

# CTD Camera Usage
walking through every section on the dashboard (`http://<device>:5020`),
top to bottom.
 
**Default Capture Mode** — sets which mode (Photo, Video, or neither)
starts automatically when the app launches, e.g. after a reboot. Saving
this also switches to that mode immediately, not just on next boot.
 
**System Date & Time** — sets the Pi's system clock. Useful in the field
with no internet/NTP access to keep timestamps accurate. "Use Browser's
Time" fills the field with your phone/laptop's own clock as a convenient
source of truth; "Set System Time" applies it.
 
**Photo Capture Loop** — status (running/stopped), Start/Stop buttons,
and a settings form (device, input format, resolution, capture interval,
warmup frame count, output folder). Saving settings restarts the loop
with the new values. Input format and resolution become dropdowns
populated from what the camera actually supports, once one is connected
and detected (`v4l2-ctl`) — otherwise they're plain text fields. A log
panel below shows the last ~30 lines of that loop's output, including
any ffmpeg errors.
 
**Video Capture** — same layout as Photo, plus an FPS dropdown and a live
countdown showing time remaining until the current segment is finalized.
Recording is continuous, split into segments (length configurable in
seconds) so a sudden power loss only risks the segment being written at
that moment, not the whole recording session.
 
**Photo Captures / Video Captures / Combined Videos** — each is a
checkbox-selectable file list with:
- **Delete** (per row) — removes that one file, with a confirmation prompt.
- **Delete Selected** — removes every checked file.
- **Download Selected** — zips every checked file into one download.
- **Delete All** — clears the whole section.
- **Combine Selected** (Video Captures only) — joins 2+ checked segments,
  oldest to newest by timestamp, into one file placed under Combined
  Videos. Tries a fast lossless copy first, falling back to re-encoding
  if the segments don't share compatible settings.
In Video Captures, the segment currently being recorded is always hidden
from this list (and can't be selected/deleted/downloaded/combined) since
it isn't a complete, playable file yet — it reappears once finalized.
 
Every delete/combine/download action shows a green (success) or red
(error) banner at the top of the page after it completes.

### Important Notes:
The time/date should be set when CTDCam is started to ensure correct dates and times for recorded segments.

The higher resolution you use, and in the case of video: higher frame rates, will draw more power and processing power from the Pi. To prevent drawing too much current from the FISH, I would recommend a Max frame rate and resolution combination for video to be 15 FPS and 1280 x 720. Photo mode draws less power from the start and can capture at higher resolutions. 

If video is stopped before the segment is finished, that segment will be corrupted. So my recommendation is to let the timer finish for the segment and then stop the video.

For photos, the purpose of the warm-up frames is to let the camera auto-adjust brightness, contrast, and other important settings on its own to get the ideal image before saving a final picture.

### Extra Information:
The Pi's operating system is Dietpi.
THe Pi has 3 Users.

"root" with the password "brute"

"dietpi" with the password "brute"

"aomlphod" with the password "liveleaks"

Aomlphod is the same user that runs the CTDCam script and the script is housed in aomlphod's home directory.
Aomlphod has full sudoer's permissions.

The Pi hosts an SSH and an FTP server using vsftpd.
They both use the same authentication with the login for the user "aomlphod".

## CTD Camera Setup:

### Install dependencies

```bash
sudo apt install -y ffmpeg v4l-utils python3-pip python3-venv git
pip install flask --break-system-packages
```

- `ffmpeg` — does the actual photo/video capture
- `v4l-utils` — provides `v4l2-ctl`, which the app uses to detect the
  camera's supported resolutions/formats for the dropdown menus.
- `python3-venv` / `git` — convenience, not strictly required

### Get the CTDCam files onto the device

```bash
git clone 'https://github.com/Yuiro14/CTDCam' ~/CTDCam
```
Make the scripts executable:
```bash
chmod +x capture_loop.sh video_capture.sh
```

#### Fix line endings

Some editors or git configurations on Windows save files with `\r\n`
(CRLF) line endings instead of Unix `\n`. Bash treats the stray `\r` as
part of the command and breaks.

Check:
```bash
file *.sh app.py
```
If it says `with CRLF line terminators`, fix it:

```bash
sed -i 's/\r$//' *.sh app.py
```

### Fix Camera permissions

```bash
ls -l /dev/video0
```
should show it's group-owned by `video` (or similar). Add your user to
that group so it doesn't need root to access the camera:
```bash
sudo usermod -aG video <username>
```
To keep permissions consistent across reboots/hotplug:
```bash
sudo tee /etc/udev/rules.d/99-camera-permissions.rules << 'EOF'
SUBSYSTEM=="video4linux", GROUP="video", MODE="0660"
EOF
sudo udevadm control --reload
sudo udevadm trigger
```

### Fix System date/time permissions

The app's "System Date & Time" page lets you set the Pi's clock directly. 
It runs `sudo date -s "..."`, which needs explicit permission.

```bash
sudo tee /etc/sudoers.d/ctdcam-date << 'EOF'
<username> ALL=(ALL) NOPASSWD: /bin/date
EOF
sudo chmod 0440 /etc/sudoers.d/ctdcam-date
```
Verify:
```bash
sudo -u <username> sudo date -s "2026-01-01 00:00:00"
```
If this prompts for a password or fails, confirm the username matches
and run `sudo visudo -c` to check for syntax errors.

### Create the systemd service

Save as `/etc/systemd/system/CTDCam.service`:

```ini
[Unit]
Description=CTDCam camera controller and dashboard
After=network.target

[Service]
Type=simple
User=<username>
WorkingDirectory=/home/<username>/CTDCam
ExecStart=/usr/bin/python3 /home/<username>/CTDCam/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Enable and start it

```bash
sudo systemctl daemon-reload
sudo systemctl enable CTDCam.service
sudo systemctl start CTDCam.service
```

CTDCam's default ip is **192.168.50.1** 
The dashboard listens on port **5020**
So you can use **http://192.168.50.1:5020/** to access it by default.


### Useful commands

```bash
sudo systemctl status CTDCam
sudo journalctl -u CTDCam -f     # Flask-level logs (startup, crashes)
sudo systemctl restart CTDCam
```

The photo/video *capture* logs (ffmpeg errors, retries) are separate —
those show in the web UI, stored at `logs/photo.log` and `logs/video.log`.

---

