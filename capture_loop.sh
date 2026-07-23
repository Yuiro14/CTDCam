#!/usr/bin/env bash
#
# capture_loop.sh
#
# Captures a still from the webcam every INTERVAL seconds using a
# warmup-frame burst (to let exposure/focus/WB settle), keeps only the
# last frame, discards the warmup frames, and saves the keeper as:
#   <TARGET_DIR>/<YYYYmmdd_HHMMSS>_<elapsed_seconds>s.jpg
#
# Config is read from environment variables (Flask sets these).
# Stop with Ctrl+C (or SIGTERM, e.g. from the web UI's Stop button).
set -euo pipefail

# ==== Configuration (env vars override these defaults) ====
DEVICE="${DEVICE:-/dev/video0}"
RESOLUTION="${RESOLUTION:-1920x1080}"
INPUT_FORMAT="${INPUT_FORMAT:-mjpeg}"
INTERVAL="${INTERVAL:-15}"          # seconds between the START of each capture
WARMUP_FRAMES="${WARMUP_FRAMES:-5}" # total frames grabbed per capture; only the last is kept
TARGET_DIR="${1:-${TARGET_DIR:-./captures}}"

# ==== Setup ====
mkdir -p "$TARGET_DIR"
START_TIME=$(date +%s)
LAST_FRAME_NUM=$(printf "%02d" "$WARMUP_FRAMES")

cleanup() {
    echo
    echo "Stopping capture loop."
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "Capture loop starting."
echo "  Device:      $DEVICE"
echo "  Resolution:  $RESOLUTION"
echo "  Interval:    ${INTERVAL}s"
echo "  Warmup:      ${WARMUP_FRAMES} frames"
echo "  Target dir:  $TARGET_DIR"
echo "Press Ctrl+C to stop."
echo

while true; do
    LOOP_START=$(date +%s)
    ELAPSED=$(( LOOP_START - START_TIME ))
    TS=$(date +"%Y%m%d_%H%M%S")
    TMP_PREFIX="${TARGET_DIR}/tmp_${TS}"
    FINAL_TMP="${TMP_PREFIX}_${LAST_FRAME_NUM}.jpg"
    OUTPUT_FILE="${TARGET_DIR}/${TS}_${ELAPSED}s.jpg"

    if ffmpeg -loglevel error -f v4l2 -input_format "$INPUT_FORMAT" \
        -video_size "$RESOLUTION" -use_wallclock_as_timestamps 1 -i "$DEVICE" \
        -frames:v "$WARMUP_FRAMES" -fps_mode vfr \
        "${TMP_PREFIX}_%02d.jpg"; then
        if [[ -f "$FINAL_TMP" ]]; then
            mv "$FINAL_TMP" "$OUTPUT_FILE"
            echo "Saved: $OUTPUT_FILE"
        else
            echo "Warning: expected frame not found: $FINAL_TMP" >&2
        fi
    else
        echo "Warning: ffmpeg capture failed at $TS" >&2
    fi

    rm -f "${TMP_PREFIX}"_*.jpg

    LOOP_END=$(date +%s)
    LOOP_DURATION=$(( LOOP_END - LOOP_START ))
    SLEEP_TIME=$(( INTERVAL - LOOP_DURATION ))
    if (( SLEEP_TIME > 0 )); then
        sleep "$SLEEP_TIME"
    fi
done