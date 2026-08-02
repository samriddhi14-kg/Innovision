"""
app.py - Refactored Monkey Detector (Ultralytics YOLO Backend)

Architecture:
=============
- Model loaded via Ultralytics YOLO using `my_model/best.pt` and `my_model/data.yaml`
- CameraThread       : Grabs video frames at ~30 FPS, stores latest in `latest_frame`
- DetectionThread    : Runs inference every N frames, performs strict class filtering
                       and N-frame temporal debouncing before triggering relay/alert sound
- Sound Player       : Pygame mixer playing warning audio (`sounds/tiger.mp3`) on valid alert
- Serial Controller  : Thread-safe Arduino communication (b"1" = monkey alert, b"0" = clear)
- generate_frames()  : Reads `latest_frame`, draws `last_result` overlay, yields MJPEG stream
- /start /stop       : Flask HTTP lifecycle endpoints
"""

import os
import sys
import cv2
import time
import yaml
import signal
import atexit
import datetime
import threading
from typing import List, Dict, Tuple, Optional

# Fix duplicate OpenMP runtime issue on Windows
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import torch
import serial
import pygame
from flask import Flask, Response, render_template, jsonify
from flask_cors import CORS
from ultralytics import YOLO

# ---------------------------------------------------------------------------
# Flask Configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)

# CORS — allow the Vite dev server (port 3000 / 5173) and any localhost origin
CORS(app, origins=[
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://localhost:*",
], supports_credentials=False)

# ---------------------------------------------------------------------------
# Logging System (ASCII-safe for Windows console)
# ---------------------------------------------------------------------------
LOG_FILE_PATH = "prediction_log.txt"
log_file = open(LOG_FILE_PATH, "a", buffering=1, encoding="utf-8")

def log_msg(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), flush=True)
    log_file.write(line + "\n")
    log_file.flush()

# ---------------------------------------------------------------------------
# Model & Class Configuration
# ---------------------------------------------------------------------------
MODEL_PATH      = os.path.join("my_model", "best.pt")
DATA_YAML_PATH  = os.path.join("my_model", "data.yaml")
CONF_THRES      = 0.40   # Minimum confidence threshold for detection
DETECT_EVERY    = 3      # Run inference every Nth grabbed frame
DEBOUNCE_FRAMES = 3      # Consecutive detection frames required for alert (Time-in-Sight)

log_msg(f"Loading YOLO model from: {MODEL_PATH}")

if not os.path.exists(MODEL_PATH):
    # Fallback check
    alt_path = os.path.join("my_model", "my_model.pt")
    if os.path.exists(alt_path):
        MODEL_PATH = alt_path
        log_msg(f"Primary weights missing, using fallback: {MODEL_PATH}")
    else:
        log_msg(f"ERROR: Model file not found at {MODEL_PATH}")
        sys.exit(1)

model = YOLO(MODEL_PATH)
log_msg("YOLO model loaded successfully.")

# Parse class names dynamically from data.yaml or model.names
class_names: Dict[int, str] = {}
if os.path.exists(DATA_YAML_PATH):
    try:
        with open(DATA_YAML_PATH, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f)
            if yaml_data and "names" in yaml_data:
                yaml_names = yaml_data["names"]
                if isinstance(yaml_names, list):
                    class_names = {idx: name for idx, name in enumerate(yaml_names)}
                elif isinstance(yaml_names, dict):
                    class_names = {int(k): str(v) for k, v in yaml_names.items()}
                log_msg(f"Parsed class names from {DATA_YAML_PATH}: {class_names}")
    except Exception as exc:
        log_msg(f"Warning: Failed to parse {DATA_YAML_PATH}: {exc}")

if not class_names:
    class_names = model.names if hasattr(model, "names") else {0: "Monkey", 1: "Not monkey"}
    log_msg(f"Using model default class names: {class_names}")

# Identify Monkey class IDs strictly (matching 'monkey', excluding 'not monkey')
MONKEY_CLASS_IDS: List[int] = []
for cid, cname in class_names.items():
    cname_lower = str(cname).lower().strip()
    if "monkey" in cname_lower and "not monkey" not in cname_lower:
        MONKEY_CLASS_IDS.append(cid)

if not MONKEY_CLASS_IDS:
    # Default to class ID 0 if name parsing didn't find explicit 'Monkey'
    MONKEY_CLASS_IDS = [0]

log_msg(f"Strict Monkey Class IDs: {MONKEY_CLASS_IDS} -> {[class_names.get(i) for i in MONKEY_CLASS_IDS]}")

# ---------------------------------------------------------------------------
# Audio System (Multi-Engine Alert Sound Player)
# ---------------------------------------------------------------------------
SOUND_FILE = os.path.abspath(os.path.join("sounds", "tiger.mp3"))
_audio_lock = threading.Lock()
_sound_playing = False
_audio_engine = "none"   # "pygame" / "mci" / "beep"
_sound_object: Optional[pygame.mixer.Sound] = None
_sound_channel: Optional[pygame.mixer.Channel] = None

# Attempt Pygame mixer init
try:
    pygame.mixer.init()
    if os.path.exists(SOUND_FILE):
        _sound_object = pygame.mixer.Sound(SOUND_FILE)
        _audio_engine = "pygame"
        log_msg(f"Loaded alert audio via Pygame: {SOUND_FILE}")
except Exception as exc:
    log_msg(f"Pygame mixer sound load skipped: {exc}")

# Fallback to Windows MCI if pygame cannot load this specific file format
if _audio_engine == "none" and sys.platform == "win32" and os.path.exists(SOUND_FILE):
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        ret = winmm.mciSendStringW(f'open "{SOUND_FILE}" alias monkey_alert', None, 0, 0)
        if ret == 0:
            _audio_engine = "mci"
            log_msg(f"Loaded alert audio via Windows MCI: {SOUND_FILE}")
        else:
            log_msg(f"Windows MCI open code: {ret}")
    except Exception as exc:
        log_msg(f"Windows MCI init error: {exc}")

if _audio_engine == "none":
    _audio_engine = "beep"
    log_msg("Audio engine initialized in Beep mode.")

def play_alert_sound() -> None:
    global _sound_playing, _sound_channel
    with _audio_lock:
        if _sound_playing:
            return
        _sound_playing = True
        try:
            if _audio_engine == "pygame" and _sound_object:
                _sound_channel = _sound_object.play(loops=-1)
                log_msg("Audio alert started (Pygame)")
            elif _audio_engine == "mci":
                import ctypes
                ctypes.windll.winmm.mciSendStringW('play monkey_alert repeat', None, 0, 0)
                log_msg("Audio alert started (MCI)")
            elif _audio_engine == "beep" and sys.platform == "win32":
                import winsound
                winsound.Beep(1000, 400)
        except Exception as exc:
            log_msg(f"Error playing sound: {exc}")

def stop_alert_sound() -> None:
    global _sound_playing, _sound_channel
    with _audio_lock:
        if not _sound_playing:
            return
        try:
            if _audio_engine == "pygame":
                if _sound_channel:
                    _sound_channel.stop()
                elif _sound_object:
                    _sound_object.stop()
            elif _audio_engine == "mci":
                import ctypes
                ctypes.windll.winmm.mciSendStringW('stop monkey_alert', None, 0, 0)
            log_msg("Audio alert stopped")
        except Exception as exc:
            log_msg(f"Error stopping sound: {exc}")
        finally:
            _sound_playing = False
            _sound_channel = None


# ---------------------------------------------------------------------------
# Arduino Hardware Communication
# ---------------------------------------------------------------------------
ARDUINO_PORT = "COM9"
ARDUINO_BAUD = 9600
arduino: Optional[serial.Serial] = None

try:
    arduino = serial.Serial(ARDUINO_PORT, ARDUINO_BAUD, timeout=1)
    time.sleep(2)
    log_msg(f"Arduino connected on {ARDUINO_PORT}")
except Exception as exc:
    log_msg(f"Arduino connection failed on {ARDUINO_PORT}: {exc}")
    arduino = None

_state_lock = threading.Lock()
_detection_state: Optional[bool] = None   # None / True / False

def send_arduino(value: bytes, label: str) -> None:
    global _detection_state
    new_state = (value == b"1")
    with _state_lock:
        if _detection_state == new_state:
            return
        _detection_state = new_state

    if arduino:
        try:
            arduino.write(value)
            arduino.flush()
            log_msg(f"Arduino << {value.decode()} ({label})")
        except Exception as exc:
            log_msg(f"Arduino write error: {exc}")

# ---------------------------------------------------------------------------
# Shared State & Locks
# ---------------------------------------------------------------------------
_frame_lock  = threading.Lock()
latest_frame: Optional[object] = None   # Raw BGR numpy array

_result_lock = threading.Lock()
last_result: Optional[Dict] = None   # {"monkey_alert": bool, "debounced": bool, "count": int, "boxes": [...]}

running = False

# ---------------------------------------------------------------------------
# Camera Thread
# ---------------------------------------------------------------------------
class CameraThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="CameraThread")
        self._cap: Optional[cv2.VideoCapture] = None

    def open(self) -> bool:
        self._cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            # Fallback for systems without CAP_DSHOW
            self._cap = cv2.VideoCapture(1)

        if not self._cap.isOpened():
            log_msg("Failed to open webcam")
            self._cap = None
            return False

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        log_msg("Webcam opened successfully")
        return True

    def release(self) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None
            log_msg("Webcam released")

    def run(self) -> None:
        global latest_frame
        log_msg("CameraThread running")
        while True:
            if not running or self._cap is None:
                time.sleep(0.02)
                continue

            ret, frame = self._cap.read()
            if not ret or frame is None:
                time.sleep(0.02)
                continue

            with _frame_lock:
                latest_frame = frame

camera_thread = CameraThread()

# ---------------------------------------------------------------------------
# Detection Thread with Strict Class Filtering & Temporal Debouncing
# ---------------------------------------------------------------------------
class DetectionThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="DetectionThread")

    def run(self) -> None:
        global last_result
        frame_counter = 0
        consecutive_monkey_count = 0
        log_msg("DetectionThread running")

        while True:
            if not running:
                consecutive_monkey_count = 0
                time.sleep(0.05)
                continue

            with _frame_lock:
                frame = latest_frame

            if frame is None:
                time.sleep(0.05)
                continue

            frame_counter += 1
            if frame_counter % DETECT_EVERY != 0:
                time.sleep(0.01)
                continue

            try:
                # Run inference via Ultralytics
                results = model(frame, verbose=False, conf=CONF_THRES)
                
                boxes_summary = []
                monkey_detected_in_frame = False
                best_monkey_conf = 0.0

                if len(results) > 0 and len(results[0].boxes) > 0:
                    det_boxes = results[0].boxes
                    for box in det_boxes:
                        xyxy = box.xyxy[0].cpu().numpy().astype(int)
                        x1, y1, x2, y2 = int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])
                        conf = float(box.conf[0].item())
                        cls_id = int(box.cls[0].item())
                        cls_name = class_names.get(cls_id, f"class_{cls_id}")

                        is_monkey = (cls_id in MONKEY_CLASS_IDS)
                        boxes_summary.append({
                            "bbox": (x1, y1, x2, y2),
                            "conf": conf,
                            "cls_id": cls_id,
                            "cls_name": cls_name,
                            "is_monkey": is_monkey
                        })

                        if is_monkey:
                            monkey_detected_in_frame = True
                            if conf > best_monkey_conf:
                                best_monkey_conf = conf

                # Temporal Debouncing Logic (Time-in-Sight filter)
                if monkey_detected_in_frame:
                    consecutive_monkey_count += 1
                else:
                    consecutive_monkey_count = 0

                debounced_alert = (consecutive_monkey_count >= DEBOUNCE_FRAMES)

                with _result_lock:
                    last_result = {
                        "monkey_detected_raw": monkey_detected_in_frame,
                        "debounced": debounced_alert,
                        "consecutive_count": consecutive_monkey_count,
                        "best_conf": best_monkey_conf,
                        "boxes": boxes_summary
                    }

                # Trigger Hardware Payload & Audio Warning
                if debounced_alert:
                    log_msg(f"MONKEY ALERT CONFIRMED (Count={consecutive_monkey_count}/{DEBOUNCE_FRAMES}, Conf={best_monkey_conf:.2f})")
                    send_arduino(b"1", "MONKEY DETECTED")
                    play_alert_sound()
                else:
                    if monkey_detected_in_frame:
                        log_msg(f"Monkey seen (Debouncing count={consecutive_monkey_count}/{DEBOUNCE_FRAMES})")
                    send_arduino(b"0", "clear/no monkey")
                    stop_alert_sound()

            except Exception as exc:
                log_msg(f"Inference processing error: {exc}")

detection_thread = DetectionThread()

# ---------------------------------------------------------------------------
# MJPEG Frame Generator
# ---------------------------------------------------------------------------
def generate_frames():
    while running:
        with _frame_lock:
            frame = latest_frame

        if frame is None:
            time.sleep(0.03)
            continue

        display = frame.copy()

        with _result_lock:
            result = last_result

        if result:
            debounced = result.get("debounced", False)
            raw_monkey = result.get("monkey_detected_raw", False)
            count = result.get("consecutive_count", 0)

            for item in result.get("boxes", []):
                x1, y1, x2, y2 = item["bbox"]
                conf = item["conf"]
                cname = item["cls_name"]
                is_m = item["is_monkey"]

                if is_m:
                    color = (0, 255, 0) if debounced else (0, 255, 255)
                    label = f"🐒 {cname}: {conf * 100:.1f}%"
                else:
                    color = (180, 180, 180)
                    label = f"{cname}: {conf * 100:.1f}%"

                # Draw bounding box
                cv2.rectangle(display, (x1, y1), (x2, y2), color, 2)

                # Draw label box
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
                lbl_y = max(y1 - 8, th + 8)
                cv2.rectangle(display, (x1, lbl_y - th - 6), (x1 + tw + 6, lbl_y + 2), color, -1)
                cv2.putText(display, label, (x1 + 3, lbl_y - 3),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

            # Header Overlay
            if debounced:
                cv2.putText(display, f"ALERT: MONKEY DETECTED [{count}/{DEBOUNCE_FRAMES}]", (10, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 0, 255), 2)
            elif raw_monkey:
                cv2.putText(display, f"VERIFYING MONKEY... [{count}/{DEBOUNCE_FRAMES}]", (10, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
            else:
                cv2.putText(display, "STATUS: CLEAR (No Monkey)", (10, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (200, 200, 200), 2)
        else:
            cv2.putText(display, "STATUS: INITIALIZING...", (10, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75, (200, 200, 200), 2)

        ok, buf = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" +
            buf.tobytes() +
            b"\r\n"
        )
        time.sleep(0.033)   # ~30 FPS

# ---------------------------------------------------------------------------
# Flask HTTP Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start", methods=["POST"])
def start():
    global running, last_result, _detection_state
    if running:
        return "Already running", 200

    if not camera_thread.open():
        return "Failed to open webcam", 500

    last_result      = None
    _detection_state = None
    running          = True
    log_msg("Detection system started via /start endpoint")
    return "Started", 200

@app.route("/stop", methods=["POST"])
def stop():
    global running
    if not running:
        return "Not running", 200

    running = False
    time.sleep(0.15)
    camera_thread.release()
    stop_alert_sound()
    send_arduino(b"0", "relay OFF on stop")
    log_msg("Detection system stopped via /stop endpoint")
    return "Stopped", 200

@app.route("/video_feed")
def video_feed():
    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma":        "no-cache",
            "Expires":       "0",
            "Access-Control-Allow-Origin": "*",
        },
    )

@app.route("/status")
def status():
    """Health-check endpoint for the frontend to poll."""
    with _result_lock:
        result = last_result
    monkey_alert = False
    confidence = 0.0
    if result:
        monkey_alert = result.get("debounced", False)
        confidence = result.get("best_conf", 0.0)
    return jsonify({
        "running": running,
        "monkey_alert": monkey_alert,
        "confidence": round(confidence * 100, 1),
        "camera_online": running,
    })

# ---------------------------------------------------------------------------
# Graceful Shutdown Handler
# ---------------------------------------------------------------------------
def _shutdown(signum=None, frame_arg=None):
    global running
    running = False
    camera_thread.release()
    stop_alert_sound()
    if arduino:
        try:
            if arduino.is_open:
                arduino.write(b"0")
                arduino.flush()
                arduino.close()
        except Exception:
            pass
    log_msg("Application shutdown complete.")
    sys.exit(0)

for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, _shutdown)
atexit.register(_shutdown)

# ---------------------------------------------------------------------------
# Program Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Auto-start camera and detection on boot so frontend gets a live stream immediately
    if camera_thread.open():
        last_result      = None
        _detection_state = None
        running          = True
        log_msg("Camera auto-started on boot.")
    else:
        log_msg("WARNING: Camera auto-start failed. POST /start when camera is ready.")

    camera_thread.start()
    detection_thread.start()
    log_msg("Starting Flask application server on http://127.0.0.1:5000")
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000, threaded=True)
