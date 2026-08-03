"""Find and click the odd card in a 2x3 on-screen grid."""

from __future__ import annotations

import argparse
import atexit
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import mss
import numpy as np
import pyautogui


CONFIG = Path(__file__).with_name("config.json")
CONFIRM_TEMPLATE = Path(__file__).with_name("confirm_template.png")
CANCEL_TEMPLATE = Path(__file__).with_name("cancel_template.png")
TRAINING_DIR = Path(__file__).with_name("training_data")
TRAINING_FILE = TRAINING_DIR / "samples.npz"
MAX_SAMPLES_PER_CLASS = 250
ODD_DUPLICATE_DISTANCE = 0.10
REQUIRED_STABLE_FRAMES = 3
DEFAULT_ADB_PATH = Path(r"C:\LDPlayer\LDPlayer14\adb.exe")
ADB_TIMEOUT = 5.0
OPERATION_RECORDS_DIR = Path(r"C:\LDPlayer\LDPlayer14\vms\operationRecords")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def grab_screen(monitor_index: int) -> tuple[np.ndarray, int, int]:
    with mss.mss() as camera:
        if monitor_index < 1 or monitor_index >= len(camera.monitors):
            available = len(camera.monitors) - 1
            raise SystemExit(f"ไม่พบจอ {monitor_index}; ระบบพบจอทั้งหมด {available} จอ")
        monitor = camera.monitors[monitor_index]
        frame = np.asarray(camera.grab(monitor))[:, :, :3].copy()
        return frame, int(monitor["left"]), int(monitor["top"])


def list_monitors() -> None:
    with mss.mss() as camera:
        for index, monitor in enumerate(camera.monitors[1:], start=1):
            print(
                f"monitor {index}: {monitor['width']}x{monitor['height']} "
                f"at ({monitor['left']}, {monitor['top']})"
            )


def find_adb_path() -> Path:
    if DEFAULT_ADB_PATH.exists():
        return DEFAULT_ADB_PATH
    discovered = shutil.which("adb")
    if discovered:
        return Path(discovered)
    raise SystemExit("ไม่พบ adb.exe; คาดว่าจะอยู่ที่ C:\\LDPlayer\\LDPlayer14\\adb.exe")


def run_adb(arguments: list[str], serial: str | None = None, timeout: float = ADB_TIMEOUT) -> bytes:
    command = [str(find_adb_path())]
    if serial:
        command += ["-s", serial]
    command += arguments
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise SystemExit("ADB ไม่ตอบสนองภายใน 5 วินาที กรุณาเปิด ADB debugging ใน LDPlayer") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"ADB exit code {result.returncode}")
    return result.stdout


def adb_devices() -> list[tuple[str, str]]:
    try:
        output = run_adb(["devices", "-l"]).decode("utf-8", errors="replace")
    except RuntimeError as error:
        raise SystemExit(f"อ่านรายการ ADB ไม่สำเร็จ: {error}") from error
    devices = []
    for line in output.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def list_adb_devices() -> None:
    devices = adb_devices()
    if not devices:
        print("ไม่พบ ADB device — เปิด ADB debugging ใน LDPlayer แล้วลองใหม่")
        return
    for serial, state in devices:
        print(f"{serial}: {state}")


def resolve_adb_device(requested: str | None) -> str:
    devices = adb_devices()
    online = [serial for serial, state in devices if state == "device"]
    if requested:
        states = {serial: state for serial, state in devices}
        if requested not in states:
            raise SystemExit(f"ไม่พบ ADB device: {requested}")
        if states[requested] != "device":
            raise SystemExit(f"ADB device {requested} มีสถานะ {states[requested]}")
        return requested
    if not online:
        raise SystemExit("ไม่พบ ADB device ที่พร้อมใช้งาน กรุณาเปิด ADB debugging ใน LDPlayer")
    if len(online) > 1:
        choices = ", ".join(online)
        raise SystemExit(f"พบหลาย ADB devices ({choices}); กรุณาระบุ --adb-device SERIAL")
    return online[0]


class ScreenBackend:
    name = "screen"

    def __init__(self, monitor_index: int) -> None:
        self.monitor_index = monitor_index
        grab_screen(monitor_index)  # Validate before entering the main loop.

    def grab(self) -> np.ndarray:
        frame, self.left, self.top = grab_screen(self.monitor_index)
        return frame

    def tap(self, x: int, y: int) -> None:
        pyautogui.click(self.left + x, self.top + y, duration=0.06)


class AdbBackend:
    name = "adb"

    def __init__(self, requested_serial: str | None) -> None:
        self.serial = resolve_adb_device(requested_serial)
        self.grab()  # Validate screenshot/decode before entering the main loop.

    def grab(self) -> np.ndarray:
        try:
            png = run_adb(["exec-out", "screencap", "-p"], self.serial)
        except RuntimeError as error:
            raise SystemExit(f"จับภาพผ่าน ADB ไม่สำเร็จ: {error}") from error
        frame = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise SystemExit("ADB ส่งภาพหน้าจอที่ decode ไม่ได้")
        self.height, self.width = frame.shape[:2]
        return frame

    def tap(self, x: int, y: int) -> None:
        try:
            run_adb(["shell", "input", "tap", str(x), str(y)], self.serial)
        except RuntimeError as error:
            raise SystemExit(f"ส่ง ADB tap ไม่สำเร็จ: {error}") from error

    def gesture(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        if abs(x2 - x1) < 3 and abs(y2 - y1) < 3 and duration_ms < 300:
            self.tap(x1, y1)
            return
        try:
            run_adb(
                ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(max(1, duration_ms))],
                self.serial,
            )
        except RuntimeError as error:
            raise SystemExit(f"ส่ง ADB gesture ไม่สำเร็จ: {error}") from error


class AdbMacroRunner:
    """Replay an LDPlayer .record file with a pausable ADB timeline."""

    def __init__(self, path: Path, backend: AdbBackend, loop: bool) -> None:
        self.path = path
        self.backend = backend
        self.loop = loop
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.error: BaseException | None = None
        self.absent_since: float | None = None
        self.actions, self.cycle_ms, self.record_width, self.record_height = self._load(path)
        self.thread = threading.Thread(target=self._run, name="adb-macro", daemon=True)
        atexit.register(self.stop)

    @staticmethod
    def _load(path: Path) -> tuple[list[tuple[int, int, int, int, int, int]], int, int, int]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"อ่าน macro ไม่สำเร็จ: {path} ({error})") from error
        downs: dict[int, tuple[int, int, int]] = {}
        actions = []
        ignored = set()
        for operation in data.get("operations", []):
            operation_id = operation.get("operationId")
            if operation_id != "PutMultiTouch":
                ignored.add(str(operation_id))
                continue
            timing = int(operation.get("timing", 0))
            for point in operation.get("points", []):
                touch_id = int(point.get("id", 0))
                state = int(point.get("state", -1))
                if state == 1:
                    downs[touch_id] = (timing, int(point["x"]), int(point["y"]))
                elif state == 0 and touch_id in downs:
                    start_ms, x1, y1 = downs.pop(touch_id)
                    actions.append((start_ms, x1, y1, int(point["x"]), int(point["y"]), max(1, timing - start_ms)))
        actions.sort(key=lambda item: item[0])
        if ignored:
            print(f"Macro: ข้าม operations ที่ไม่ใช่ touch: {', '.join(sorted(ignored))}")
        if not actions:
            raise SystemExit(f"macro ไม่มี touch operations: {path}")
        info = data.get("recordInfo", {})
        record_width = int(info.get("resolutionWidth", 0))
        record_height = int(info.get("resolutionHeight", 0))
        if record_width <= 0 or record_height <= 0:
            raise SystemExit(f"macro ไม่มี resolution ที่ถูกต้อง: {path}")
        cycle_ms = max(int(info.get("circleDuration", 0)), actions[-1][0] + actions[-1][5])
        return actions, cycle_ms, record_width, record_height

    def start(self) -> None:
        print(
            f"เริ่ม ADB macro: {self.path.name} ({len(self.actions)} gestures, "
            f"recorded {self.record_width}x{self.record_height}, device {self.backend.width}x{self.backend.height})"
        )
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        self.paused.clear()

    def set_paused(self, value: bool) -> None:
        before = self.paused.is_set()
        if value:
            self.paused.set()
        else:
            self.paused.clear()
        if before != value:
            print("Pause ADB macro" if value else "Resume ADB macro")

    def observe_scene(self, cards_or_confirm_visible: bool) -> None:
        if cards_or_confirm_visible:
            self.absent_since = None
            self.set_paused(True)
        elif self.paused.is_set():
            if self.absent_since is None:
                self.absent_since = time.monotonic()
            elif time.monotonic() - self.absent_since >= 0.5:
                self.set_paused(False)

    def _wait_until_active(
        self, target_seconds: float, cycle_start: float, paused_total: float
    ) -> tuple[bool, float]:
        while not self.stopped.is_set():
            if self.paused.is_set():
                pause_started = time.monotonic()
                while self.paused.is_set() and not self.stopped.is_set():
                    time.sleep(0.01)
                paused_total += time.monotonic() - pause_started
                continue
            elapsed = time.monotonic() - cycle_start - paused_total
            remaining = target_seconds - elapsed
            if remaining <= 0:
                return True, paused_total
            time.sleep(min(0.01, remaining))
        return False, paused_total

    def _run(self) -> None:
        try:
            while not self.stopped.is_set():
                cycle_start = time.monotonic()
                paused_total = 0.0
                for timing, x1, y1, x2, y2, duration in self.actions:
                    ready, paused_total = self._wait_until_active(timing / 1000.0, cycle_start, paused_total)
                    if not ready:
                        return
                    # LDPlayer .record stores touch coordinates as pixel * 12.
                    # Scale from the recorded resolution to the current ADB resolution.
                    sx = self.backend.width / (self.record_width * 12.0)
                    sy = self.backend.height / (self.record_height * 12.0)
                    self.backend.gesture(
                        round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy), duration
                    )
                if not self.loop:
                    return
                ready, _ = self._wait_until_active(self.cycle_ms / 1000.0, cycle_start, paused_total)
                if not ready:
                    return
        except BaseException as error:
            self.error = error
            self.stopped.set()


def make_backend(name: str, monitor_index: int, adb_device: str | None):
    if name == "adb":
        return AdbBackend(adb_device)
    return ScreenBackend(monitor_index)


def calibrate(backend_name: str, monitor_index: int, adb_device: str | None) -> None:
    backend = make_backend(backend_name, monitor_index, adb_device)
    frame = backend.grab()
    title = "ลากกรอบคลุมการ์ดทั้ง 6 ช่องตามเส้นแดง แล้วกด ENTER"
    x, y, w, h = map(int, cv2.selectROI(title, frame, False, False))
    cv2.destroyAllWindows()
    if w < 100 or h < 100:
        raise SystemExit("ยกเลิกการตั้งค่า")
    screen_h, screen_w = frame.shape[:2]
    config = {
        "backend": backend_name,
        "grid": [x / screen_w, y / screen_h, w / screen_w, h / screen_h],
    }
    if backend_name == "adb":
        config["adb_device"] = backend.serial
    else:
        config["monitor"] = monitor_index
    CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"บันทึกพื้นที่ตรวจจับแล้ว: {CONFIG}")


def save_confirm_template(source: str) -> None:
    image = cv2.imread(source)
    if image is None:
        raise SystemExit(f"อ่านภาพปุ่มไม่ได้: {source}")
    if image.shape[0] < 30 or image.shape[1] < 80:
        raise SystemExit("ภาพปุ่มมีขนาดเล็กเกินไป")
    if not cv2.imwrite(str(CONFIRM_TEMPLATE), image):
        raise SystemExit("บันทึกภาพต้นแบบ Confirm ไม่สำเร็จ")
    print(f"บันทึกภาพต้นแบบ Confirm แล้ว: {CONFIRM_TEMPLATE}")


def save_cancel_template(source: str) -> None:
    image = cv2.imread(source)
    if image is None:
        raise SystemExit(f"อ่านภาพปุ่มไม่ได้: {source}")
    if image.shape[0] < 30 or image.shape[1] < 80:
        raise SystemExit("ภาพปุ่มมีขนาดเล็กเกินไป")
    if not cv2.imwrite(str(CANCEL_TEMPLATE), image):
        raise SystemExit("บันทึกภาพต้นแบบ Cancel ไม่สำเร็จ")
    print(f"บันทึกภาพต้นแบบ Cancel แล้ว: {CANCEL_TEMPLATE}")


def card_boxes(frame: np.ndarray, grid: list[float]) -> list[tuple[int, int, int, int]]:
    screen_h, screen_w = frame.shape[:2]
    x, y, w, h = grid
    x, y, w, h = int(x * screen_w), int(y * screen_h), int(w * screen_w), int(h * screen_h)
    boxes = []
    for row in range(2):
        for col in range(3):
            x1 = x + round(col * w / 3)
            x2 = x + round((col + 1) * w / 3)
            y1 = y + round(row * h / 2)
            y2 = y + round((row + 1) * h / 2)
            # Ignore borders and gaps; retain the character-bearing interior.
            pad_x, pad_y = int((x2 - x1) * 0.13), int((y2 - y1) * 0.12)
            boxes.append((x1 + pad_x, y1 + pad_y, x2 - pad_x, y2 - pad_y))
    return boxes


def active_card_indices(frame: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> list[int]:
    """Return card slots that still contain a pale cream card interior."""
    active = []
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        crop = frame[y1:y2, x1:x2]
        blue, green, red = cv2.split(crop)
        cream = (red > 225) & (green > 215) & ((red.astype(np.int16) - blue) > 14)
        if float(cream.mean()) > 0.48:
            active.append(i)
    return active


def find_button(
    frame: np.ndarray, grid: list[float], template_path: Path, threshold: float = 0.82
) -> tuple[int, int, int, int] | None:
    """Find a button by its white centre text, not its colour or outer shape."""
    if not template_path.exists():
        return None
    screen_h, screen_w = frame.shape[:2]
    gx, gy, gw, gh = grid
    gx, gy, gw, gh = gx * screen_w, gy * screen_h, gw * screen_w, gh * screen_h
    x1 = max(0, int(gx - gw * 0.45))
    y1 = max(0, int(gy - gh * 0.35))
    x2 = min(screen_w, int(gx + gw * 1.45))
    y2 = min(screen_h, int(gy + gh * 1.25))
    search = frame[y1:y2, x1:x2]
    template = cv2.imread(str(template_path))
    if template is None:
        return None
    template_h, template_w = template.shape[:2]
    crop_left, crop_right = int(template_w * 0.16), int(template_w * 0.84)
    crop_top, crop_bottom = int(template_h * 0.22), int(template_h * 0.88)
    text_template = template[crop_top:crop_bottom, crop_left:crop_right]

    def white_text_mask(image: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        return (((hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 185)) * 255).astype(np.uint8)

    search_mask = white_text_mask(search)
    template_mask = white_text_mask(text_template)
    if cv2.countNonZero(template_mask) < 20:
        return None
    best_score, best_box = 0.0, None
    # Allow emulator resolution scaling while requiring the exact word pattern.
    for scale in np.linspace(0.60, 1.45, 18):
        text_w = int(text_template.shape[1] * scale)
        text_h = int(text_template.shape[0] * scale)
        if text_w < 40 or text_h < 20 or text_w > search.shape[1] or text_h > search.shape[0]:
            continue
        resized = cv2.resize(template_mask, (text_w, text_h), interpolation=cv2.INTER_NEAREST)
        result = cv2.matchTemplate(search_mask, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            text_x, text_y = location
            button_x = x1 + text_x - int(crop_left * scale)
            button_y = y1 + text_y - int(crop_top * scale)
            button_w = int(template_w * scale)
            button_h = int(template_h * scale)
            best_box = (
                max(0, button_x), max(0, button_y),
                min(screen_w, button_x + button_w), min(screen_h, button_y + button_h),
            )
    return best_box if best_score >= threshold else None


def find_cancel_button(frame: np.ndarray, grid: list[float]) -> tuple[int, int, int, int] | None:
    return find_button(frame, grid, CANCEL_TEMPLATE)


def find_confirm_button(frame: np.ndarray, grid: list[float]) -> tuple[int, int, int, int] | None:
    return find_button(frame, grid, CONFIRM_TEMPLATE)


def feature(crop: np.ndarray) -> np.ndarray:
    small = cv2.resize(crop, (96, 128), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)
    gray = lab[:, :, 0]
    edges = cv2.Canny(gray, 45, 110)
    # Colour separates the character from the nearly uniform card background.
    chroma = cv2.GaussianBlur(lab[:, :, 1:], (5, 5), 0)
    data = np.dstack((gray, edges * 2, chroma)).astype(np.float32)
    data -= data.mean(axis=(0, 1), keepdims=True)
    scale = data.std(axis=(0, 1), keepdims=True) + 1e-6
    return data / scale


def learning_descriptor(crop: np.ndarray) -> np.ndarray:
    """Compact feature vector suitable for persistent nearest-neighbour learning."""
    return cv2.resize(feature(crop), (24, 32), interpolation=cv2.INTER_AREA).reshape(-1).astype(np.float32)


class LearningStore:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.normal = np.empty((0, 32 * 24 * 4), dtype=np.float32)
        self.odd = np.empty((0, 32 * 24 * 4), dtype=np.float32)
        if enabled and TRAINING_FILE.exists():
            try:
                with np.load(TRAINING_FILE) as data:
                    self.normal = data["normal"].astype(np.float32)
                    self.odd = data["odd"].astype(np.float32)
                    self.odd = self._deduplicate(self.odd)
            except (OSError, ValueError, KeyError) as error:
                print(f"คำเตือน: โหลดข้อมูลเรียนรู้ไม่ได้ ({error}) — เริ่มด้วยข้อมูลว่าง")

    @staticmethod
    def _deduplicate(samples: np.ndarray) -> np.ndarray:
        unique = []
        for sample in samples:
            if not unique or min(float(np.mean(np.abs(sample - item))) for item in unique) >= ODD_DUPLICATE_DISTANCE:
                unique.append(sample)
        return np.asarray(unique, dtype=np.float32) if unique else samples[:0]

    def add_odd(self, odd: np.ndarray) -> None:
        if not self.enabled:
            return
        nearest = self._closest(odd, self.odd)
        if nearest is not None and nearest < ODD_DUPLICATE_DISTANCE:
            print(f"ข้าม odd ที่ซ้ำกับข้อมูลเดิม (distance={nearest:.3f})")
            return
        self.odd = np.vstack((self.odd, odd[None]))[-MAX_SAMPLES_PER_CLASS:]
        TRAINING_DIR.mkdir(parents=True, exist_ok=True)
        temp = TRAINING_DIR / "samples.tmp"
        with temp.open("wb") as handle:
            np.savez_compressed(handle, normal=self.normal.astype(np.float16), odd=self.odd.astype(np.float16))
        temp.replace(TRAINING_FILE)
        print(f"เรียนรู้ odd ใหม่แล้ว: odd={len(self.odd)}")

    @staticmethod
    def _closest(descriptor: np.ndarray, samples: np.ndarray) -> float | None:
        if len(samples) == 0:
            return None
        return float(np.min(np.mean(np.abs(samples - descriptor), axis=1)))

    def learned_scores(self, descriptors: list[np.ndarray]) -> np.ndarray | None:
        if not self.enabled or len(self.odd) == 0:
            return None
        scores = []
        for descriptor in descriptors:
            d_odd = self._closest(descriptor, self.odd)
            # Exact/near odd examples approach 1.0; unrelated poses approach 0.
            scores.append(float(np.exp(-float(d_odd) / 0.25)))
        return np.asarray(scores, dtype=np.float32)


def reset_learning() -> None:
    if TRAINING_DIR.exists():
        shutil.rmtree(TRAINING_DIR)
    print("ล้างข้อมูลเรียนรู้แล้ว")


def print_learning_stats() -> None:
    store = LearningStore(True)
    size = TRAINING_FILE.stat().st_size if TRAINING_FILE.exists() else 0
    print(f"odd_used={len(store.odd)}, normal_ignored={len(store.normal)}, file={size:,} bytes")


def find_odd(
    frame: np.ndarray, boxes: list[tuple[int, int, int, int]], learning: LearningStore | None = None
) -> tuple[int, np.ndarray, list[np.ndarray]]:
    crops = [frame[y1:y2, x1:x2] for x1, y1, x2, y2 in boxes]
    features = [feature(crop) for crop in crops]
    descriptors = [learning_descriptor(crop) for crop in crops]
    count = len(features)
    distances = np.zeros((count, count), dtype=np.float32)
    for i in range(count):
        for j in range(i + 1, count):
            distances[i, j] = distances[j, i] = np.mean(np.abs(features[i] - features[j]))
    # The normal pose is the medoid: the card with the smallest distance to others.
    medoid = int(np.argmin(np.median(distances + np.eye(count) * 99, axis=1)))
    frame_scores = distances[medoid]
    scores = frame_scores.copy()
    learned = learning.learned_scores(descriptors) if learning else None
    if learned is not None:
        low, high = float(frame_scores.min()), float(frame_scores.max())
        normalized_frame = (frame_scores - low) / (high - low + 1e-6)
        # Odd-only library supplements, but never replaces, the live majority vote.
        weight = min(0.40, len(learning.odd) / 20.0 * 0.40)
        scores = (1.0 - weight) * normalized_frame + weight * learned
    return int(np.argmax(scores)), scores, descriptors


def update_stability(
    previous_slot: int | None, stable_frames: int, current_slot: int
) -> tuple[int, int]:
    """Count consecutive frames won by the same card slot."""
    if previous_slot == current_slot:
        return current_slot, stable_frames + 1
    return current_slot, 1


def run(
    click: bool,
    interval: float,
    delay: float,
    show: bool,
    learning_enabled: bool,
    monitor_override: int | None,
    backend_override: str | None,
    adb_device_override: str | None,
    macro_file: str | None,
    macro_once: bool,
) -> None:
    if not CONFIG.exists():
        raise SystemExit("ยังไม่ได้ตั้งพื้นที่ กรุณารัน: python detector.py --calibrate")
    saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    if "grid" not in saved:
        raise SystemExit("กรุณาตั้งกรอบแบบใหม่: python detector.py --calibrate")
    grid = saved["grid"]
    monitor_index = monitor_override or int(saved.get("monitor", 1))
    saved_backend = saved.get("backend", "screen")
    if backend_override and backend_override != saved_backend:
        raise SystemExit(
            f"config ถูก calibrate สำหรับ {saved_backend}; กรุณารัน "
            f"python detector.py --calibrate --backend {backend_override}"
        )
    backend_name = backend_override or saved_backend
    adb_device = adb_device_override or saved.get("adb_device")
    backend = make_backend(backend_name, monitor_index, adb_device)
    last_click, previous_count, ready_at = 0.0, 0, 0.0
    learning = LearningStore(learning_enabled)
    macro = None
    if macro_file:
        if not isinstance(backend, AdbBackend):
            raise SystemExit("--macro-file ใช้ได้เฉพาะ backend adb")
        macro_path = Path(macro_file)
        if not macro_path.is_absolute():
            macro_path = OPERATION_RECORDS_DIR / macro_path
        if not macro_path.exists():
            raise SystemExit(f"ไม่พบ macro file: {macro_path}")
        macro = AdbMacroRunner(macro_path, backend, loop=not macro_once)
        macro.start()
    pending = None
    candidate_slot, stable_frames = None, 0
    cancel_latched = False
    if backend_name == "screen":
        pyautogui.FAILSAFE = True
        print("เริ่ม screen backend — เลื่อนเมาส์ไปมุมซ้ายบนเพื่อหยุดฉุกเฉิน หรือกด Q")
    else:
        print(f"เริ่ม ADB backend ({backend.serial}) — เมาส์จริงจะไม่ถูกขยับ; กด Q เพื่อหยุด")
    while True:
        if macro and macro.error:
            raise SystemExit(f"ADB macro หยุดเพราะข้อผิดพลาด: {macro.error}")
        frame = backend.grab()
        cancel = find_cancel_button(frame, grid)
        if cancel:
            if macro:
                macro.observe_scene(True)
            candidate_slot, stable_frames = None, 0
            if not cancel_latched:
                x1, y1, x2, y2 = cancel
                if click:
                    backend.tap((x1 + x2) // 2, (y1 + y2) // 2)
                    print("คลิกปุ่ม Cancel")
                last_click = time.time()
                cancel_latched = True
            time.sleep(interval)
            continue
        cancel_latched = False

        confirm = find_confirm_button(frame, grid)
        if confirm and time.time() - last_click > 0.30:
            if macro:
                macro.observe_scene(True)
            candidate_slot, stable_frames = None, 0
            x1, y1, x2, y2 = confirm
            if click:
                backend.tap((x1 + x2) // 2, (y1 + y2) // 2)
                print("คลิกปุ่ม Confirm")
            last_click = time.time()
            time.sleep(interval)
            continue

        boxes = card_boxes(frame, grid)
        active_indices = active_card_indices(frame, boxes)
        active_count = len(active_indices)
        if macro:
            macro.observe_scene(active_count in (5, 6))

        if pending is not None:
            clicked_slot = pending["slot"]
            if clicked_slot not in active_indices:
                learning.add_odd(
                    pending["descriptors"][clicked_slot],
                )
                pending = None
            elif time.time() >= pending["deadline"]:
                print(f"การ์ดช่อง {clicked_slot + 1} ไม่หาย — ไม่บันทึกข้อมูลเรียนรู้")
                pending = None

        if pending is not None:
            candidate_slot, stable_frames = None, 0
            time.sleep(interval)
            continue

        valid_scene = active_count in (5, 6)
        if valid_scene and active_count != previous_count:
            ready_at = time.time() + delay
            candidate_slot, stable_frames = None, 0
            print(f"พบการ์ด {active_count} ใบ — รอ {delay:.1f} วินาทีก่อนตรวจ")
        previous_count = active_count if valid_scene else 0
        if not valid_scene or time.time() < ready_at:
            if not valid_scene:
                candidate_slot, stable_frames = None, 0
            time.sleep(interval)
            continue
        active_boxes = [boxes[i] for i in active_indices]
        candidate_pos, scores, descriptors = find_odd(frame, active_boxes, learning)
        candidate = active_indices[candidate_pos]
        candidate_slot, stable_frames = update_stability(candidate_slot, stable_frames, candidate)
        display_stable_frames = stable_frames
        # Display the winning outlier score; the highest-scoring card is clicked.
        confidence = float(scores[candidate_pos])
        if click and stable_frames >= REQUIRED_STABLE_FRAMES and time.time() - last_click > 0.30:
            x1, y1, x2, y2 = boxes[candidate]
            click_x = (x1 + x2) // 2
            click_y = (y1 + y2) // 2
            backend.tap(click_x, click_y)
            print(f"คลิกใบที่ {candidate + 1} (ความมั่นใจ {confidence:.3f})")
            last_click = time.time()
            pending = {
                "slot": candidate,
                "deadline": time.time() + 2.0,
                "descriptors": {slot: descriptors[pos] for pos, slot in enumerate(active_indices)},
            }
            print(f"กำลังรอยืนยัน odd ช่อง {candidate + 1} (สูงสุด 2 วินาที)")
            candidate_slot, stable_frames = None, 0

        if show:
            preview = frame.copy()
            score_by_slot = {slot: scores[pos] for pos, slot in enumerate(active_indices)}
            for i, (x1, y1, x2, y2) in enumerate(boxes):
                if i == candidate:
                    color = (0, 0, 255)
                else:
                    color = (0, 200, 0) if i in score_by_slot else (120, 120, 120)
                cv2.rectangle(preview, (x1, y1), (x2, y2), color, 3)
                if i in score_by_slot:
                    label = f"{i + 1}: {score_by_slot[i]:.2f}"
                    if i == candidate:
                        label += f" stable {min(display_stable_frames, REQUIRED_STABLE_FRAMES)}/{REQUIRED_STABLE_FRAMES}"
                else:
                    label = f"{i + 1}: empty"
                cv2.putText(preview, label, (x1, y1 + 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
            cv2.imshow("Odd card detector - Q to quit", cv2.resize(preview, None, fx=.65, fy=.65))
            if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
                break
        time.sleep(interval)
    cv2.destroyAllWindows()
    if macro:
        macro.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="ตรวจจับการ์ดที่ต่างจากการ์ดส่วนใหญ่")
    parser.add_argument("--calibrate", action="store_true", help="เลือกกรอบรวมของการ์ด 6 ช่อง")
    parser.add_argument("--backend", choices=("screen", "adb"), help="แหล่งภาพและวิธีคลิก")
    parser.add_argument("--monitor", type=int, help="หมายเลขจอที่ต้องการใช้ เช่น 1 หรือ 2")
    parser.add_argument("--list-monitors", action="store_true", help="แสดงจอที่ตรวจพบ")
    parser.add_argument("--adb-device", help="ADB serial สำหรับเลือก LDPlayer instance")
    parser.add_argument("--list-adb-devices", action="store_true", help="แสดง ADB devices ที่ตรวจพบ")
    parser.add_argument("--macro-file", help="ไฟล์ .record ที่ให้โปรแกรมเล่นผ่าน ADB")
    parser.add_argument("--macro-once", action="store_true", help="เล่น macro รอบเดียวแทนการวนซ้ำ")
    parser.add_argument("--confirm-template", metavar="IMAGE", help="บันทึกภาพปุ่ม Confirm เป็นต้นแบบ")
    parser.add_argument("--cancel-template", metavar="IMAGE", help="บันทึกภาพปุ่ม Cancel เป็นต้นแบบ")
    parser.add_argument("--dry-run", action="store_true", help="แสดงผลอย่างเดียว ไม่คลิกจริง")
    parser.add_argument("--no-preview", action="store_true", help="ไม่แสดงหน้าต่างตรวจสอบ")
    parser.add_argument("--interval", type=float, default=0.08, help="เวลาระหว่างเฟรม (วินาที)")
    parser.add_argument("--delay", type=float, default=0.2, help="เวลารอหลังพบหรือจำนวนการ์ดเปลี่ยน (วินาที)")
    learning_group = parser.add_mutually_exclusive_group()
    learning_group.add_argument("--learning", dest="learning", action="store_true", help="เปิดระบบเรียนรู้ (ค่าเริ่มต้น)")
    learning_group.add_argument("--no-learning", dest="learning", action="store_false", help="ปิดระบบเรียนรู้ชั่วคราว")
    parser.set_defaults(learning=True)
    parser.add_argument("--reset-learning", action="store_true", help="ล้างข้อมูลเรียนรู้ทั้งหมด")
    parser.add_argument("--learning-stats", action="store_true", help="แสดงจำนวนตัวอย่างที่เรียนรู้")
    args = parser.parse_args()
    if args.list_monitors:
        list_monitors()
    elif args.list_adb_devices:
        list_adb_devices()
    elif args.reset_learning:
        reset_learning()
    elif args.learning_stats:
        print_learning_stats()
    elif args.confirm_template:
        save_confirm_template(args.confirm_template)
    elif args.cancel_template:
        save_cancel_template(args.cancel_template)
    elif args.calibrate:
        calibrate(args.backend or "screen", args.monitor or 1, args.adb_device)
    else:
        run(
            not args.dry_run,
            args.interval,
            args.delay,
            not args.no_preview,
            args.learning,
            args.monitor,
            args.backend,
            args.adb_device,
            args.macro_file,
            args.macro_once,
        )


if __name__ == "__main__":
    main()
