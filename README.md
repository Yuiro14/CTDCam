# CTDCam


## Install dependencies

```bash
sudo apt install -y ffmpeg v4l-utils python3-pip python3-venv git
pip install flask --break-system-packages
```

- `ffmpeg` — does the actual photo/video capture
- `v4l-utils` — provides `v4l2-ctl`, which the app uses to detect the
  camera's supported resolutions/formats for the dropdown menus (optional
  but recommended; the app falls back to manual text entry without it)
- `python3-venv` / `git` — convenience, not strictly required

## 4. Get the CTDCam files onto the device

Copy the whole `CTDCam` folder (containing `app.py`, `templates/`,
`capture_loop.sh`, `video_capture.sh`) to the Pi. From your own computer:
```bash
scp -r CTDCam <username>@ctdcam.local:/home/<username>/
```
Or if it's in a git repo:
```bash
git clone <your-repo-url> /home/<username>/CTDCam
```

Then on the Pi, check line endings (see below) and make the scripts
executable:
```bash
cd ~/CTDCam
chmod +x capture_loop.sh video_capture.sh
```

### Fix line endings (if transferred from Windows)

Some editors or git configurations on Windows save files with `\r\n`
(CRLF) line endings instead of Unix `\n`. Bash treats the stray `\r` as
part of the command and breaks.

Check:
```bash
file *.sh app.py
```
If it says `with CRLF line terminators`, fix it:
```bash
sudo apt install -y dos2unix
dos2unix *.sh app.py
```
or without installing anything:
```bash
sed -i 's/\r$//' *.sh app.py
```
If transferring via git from Windows, add a `.gitattributes` file with:
```
*.sh text eol=lf
app.py text eol=lf
```

## 6. Camera permissions

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

## 7. System date/time permissions

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

## 8. Create the systemd service

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

## 9. Enable and start it

```bash
sudo systemctl daemon-reload
sudo systemctl enable CTDCam.service
sudo systemctl start CTDCam.service
```

The dashboard listens on port **5020**:


## 10. Useful commands

```bash
sudo systemctl status CTDCam
sudo journalctl -u CTDCam -f     # Flask-level logs (startup, crashes)
sudo systemctl restart CTDCam
```

The photo/video *capture* logs (ffmpeg errors, retries) are separate —
those show in the web UI, stored at `logs/photo.log` and `logs/video.log`.

---
- **Photos**: each capture writes to a temp filename, renamed into place
  only after a successful capture.
- **Video**: recorded in short segments (adjustable in the UI). Only the
  segment being written when power cuts out is at risk.
- **Both scripts retry indefinitely** on failure rather than exiting.
- **`Restart=always`** covers the Flask app itself crashing.
- **Mutual exclusion**: photo and video capture share one camera and can
  never run at the same time.

## Combining videos

Selecting 2+ segments and hitting "Combine Selected" sorts them oldest to
newest by the timestamp in their filename (falling back to file
modification time if that's missing) and joins them into
`combined_videos/`. It tries a fast lossless stream copy first; if the
segments have mismatched parameters (e.g. you changed resolution/bitrate
partway through), it automatically re-encodes instead. Original segments
are left untouched.

## The currently-recording segment is hidden

While video capture is running, the segment ffmpeg is actively writing
right now is automatically hidden from the Video Captures list (and can't
be selected, downloaded, deleted, or combined) — it isn't a complete,
playable file yet. It reappears once ffmpeg finalizes it and moves to the
next segment. The page shows a live countdown to that point.

## Delete All

Each of Photo Captures, Video Captures, and Combined Videos has its own
"Delete All" button. For Video Captures, this only deletes finalized
segments — the currently-recording one (if any) is always kept safe.

