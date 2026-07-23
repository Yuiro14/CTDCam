#!/usr/bin/env bash
#
# video_capture.sh
#
# Records continuously from the webcam using the hardware h264 encoder,
# split into fixed-length segments (default 5 min) so a sudden power loss
# only damages the segment that was open at the time -- every previously
# closed segment is a complete, playable mp4.
#
# If ffmpeg dies for any reason (camera unplugged, /dev/videoX not ready
# yet at boot, transient error), this script waits a moment and starts a
# new segment rather than exiting, so it recovers on its own.
#
# Config is read from environment variables (Flask sets these).
#
# Stop with Ctrl+C (or SIGTERM, e.g. from the web UI's Stop button) --
# this cleanly signals the in-flight ffmpeg process so the current
# segment is finalized properly instead of left as a partial file.
set -uo pipefail   # NOTE: no -e here -- we want to catch ffmpeg failures
                   # ourselves in the loop below and retry, not exit.

# ==== Configuration (env vars override these defaults) ====
DEVICE="${DEVICE:-/dev/video0}"
RESOLUTION="${RESOLUTION:-1280x720}"
INPUT_FORMAT="${INPUT_FORMAT:-mjpeg}"
FPS="${FPS:-15}"
BITRATE="${BITRATE:-1.5M}"
SEGMENT_SECONDS="${SEGMENT_SECONDS:-300}"   # length of each finalized chunk
TARGET_DIR="${1:-${TARGET_DIR:-./video_captures}}"
RETRY_DELAY="${RETRY_DELAY:-5}"             # seconds to wait before retrying after a failure

mkdir -p "$TARGET_DIR"

FFMPEG_PID=""

cleanup() {
    echo
    echo "Stopping video capture."
    if [[ -n "$FFMPEG_PID" ]] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
        kill -TERM "$FFMPEG_PID" 2>/dev/null || true
        wait "$FFMPEG_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "Video capture starting."
echo "  Device:      $DEVICE"
echo "  Resolution:  $RESOLUTION"
echo "  FPS:         $FPS"
echo "  Bitrate:     $BITRATE"
echo "  Segment:     ${SEGMENT_SECONDS}s"
echo "  Target dir:  $TARGET_DIR"
echo "Press Ctrl+C to stop."
echo

while true; do
    OUTPUT_PATTERN="${TARGET_DIR}/%Y%m%d_%H%M%S_${FPS}fps.mp4"

    ffmpeg -loglevel warning -f v4l2 -input_format "$INPUT_FORMAT" \
        -video_size "$RESOLUTION" -framerate "$FPS" \
        -thread_queue_size 512 -use_wallclock_as_timestamps 1 -i "$DEVICE" \
        -pix_fmt yuv420p -c:v h264_v4l2m2m -b:v "$BITRATE" -fps_mode vfr \
        -f segment -segment_time "$SEGMENT_SECONDS" -reset_timestamps 1 -strftime 1 \
        "$OUTPUT_PATTERN" &
    FFMPEG_PID=$!
    wait "$FFMPEG_PID"
    STATUS=$?
    FFMPEG_PID=""

    if (( STATUS != 0 )); then
        echo "Warning: ffmpeg exited with status $STATUS at $(date +"%Y%m%d_%H%M%S"); retrying in ${RETRY_DELAY}s" >&2
        sleep "$RETRY_DELAY"
    fi
done