"""Find and click the odd card in a 2x3 on-screen grid."""

from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

import cv2
import mss
import numpy as np
import pyautogui


CONFIG = Path(__file__).with_name("config.json")
CONFIRM_TEMPLATE = Path(__file__).with_name("confirm_template.png")
CANCEL_TEMPLATE = Path(__file__).with_name("cancel_template.png")
CLOSE_TEMPLATE = Path(__file__).with_name("close_template.png")
UPGRADE_WINDOW_TEMPLATE = Path(__file__).with_name("upgrade_window_template.png")
ACTION_TEMPLATE = Path(__file__).with_name("action_template.png")
CLAIM_TEMPLATE = Path(__file__).with_name("claim_template.png")
TRAINING_DIR = Path(__file__).with_name("training_data")
TRAINING_FILE = TRAINING_DIR / "samples.npz"
PAUSE_CLIPS_DIR = Path(__file__).with_name("pause_clips")
MAX_PAUSE_CLIPS = 3
POST_PAUSE_RECORD_SECONDS = 0.3
OBS_PATH = Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
OBS_WEBSOCKET_CONFIG = Path.home() / r"AppData\Roaming\obs-studio\plugin_config\obs-websocket\config.json"
MAX_SAMPLES_PER_CLASS = 250
ODD_DUPLICATE_DISTANCE = 0.10
REQUIRED_STABLE_FRAMES = 1
CLOSE_RETRY_SECONDS = 0.70
CLOSE_MAX_ATTEMPTS = 5
CANCEL_RETRY_SECONDS = 0.65
CANCEL_MAX_ATTEMPTS = 5
ACTION_BURST_CLICKS = 10
ACTION_THRESHOLD = 0.45
TEMPLATE_DETECTION_SCALE = 0.50
TEMPLATE_SCALES = tuple(round(0.60 + index * 0.05, 2) for index in range(19))
DEFAULT_ADB_PATH = Path(r"C:\LDPlayer\LDPlayer14\adb.exe")
ADB_TIMEOUT = 5.0
ADB_RETRIES = 3
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
    label = " ".join(arguments[:3])
    last_error: BaseException | None = None
    for attempt in range(1, ADB_RETRIES + 1):
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
            if result.returncode == 0:
                if attempt > 1:
                    print(f"ADB กลับมาเชื่อมต่อสำเร็จ: {label} (รอบ {attempt}/{ADB_RETRIES})")
                return result.stdout
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            last_error = RuntimeError(detail or f"ADB exit code {result.returncode}")
        except subprocess.TimeoutExpired as error:
            last_error = error
            print(
                f"ADB timeout ขณะรัน '{label}' หลัง {timeout:g} วินาที "
                f"(รอบ {attempt}/{ADB_RETRIES})"
            )
        if attempt >= ADB_RETRIES:
            break
        adb = str(find_adb_path())
        try:
            if attempt == 1:
                reconnect = [adb]
                if serial:
                    reconnect += ["-s", serial]
                reconnect += ["reconnect"]
                subprocess.run(reconnect, capture_output=True, timeout=3, check=False)
                print("กำลัง reconnect ADB แล้วลองใหม่...")
            else:
                subprocess.run([adb, "kill-server"], capture_output=True, timeout=3, check=False)
                subprocess.run([adb, "start-server"], capture_output=True, timeout=5, check=False)
                print("รีสตาร์ต ADB server แล้วลองใหม่...")
        except subprocess.TimeoutExpired:
            print("คำสั่งกู้คืน ADB timeout — จะลองคำสั่งหลักอีกครั้ง")
        time.sleep(0.25)
    detail = str(last_error) if last_error else "ไม่ทราบสาเหตุ"
    raise RuntimeError(
        f"ADB ไม่ตอบสนองหลังลอง {ADB_RETRIES} รอบขณะรัน '{label}': {detail}"
    )


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

    def tap_burst(self, x: int, y: int, count: int) -> None:
        pyautogui.click(self.left + x, self.top + y, clicks=count, interval=0.0)


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

    def tap_burst(self, x: int, y: int, count: int) -> None:
        """Send the whole burst through one ADB process to minimize latency."""
        command = "; ".join(f"input tap {x} {y}" for _ in range(count))
        try:
            run_adb(["shell", "sh", "-c", command], self.serial)
        except RuntimeError as error:
            raise SystemExit(f"ส่ง ADB tap แบบรัวไม่สำเร็จ: {error}") from error

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


class PauseClipRecorder:
    """Toggle LDPlayer's native recorder with F8 without taking foreground focus."""

    def __init__(self, serial: str) -> None:
        self.window = self._find_window()
        self.recording = False
        self.before_files: dict[Path, tuple[int, int]] = {}
        self.stamp: str | None = None
        PAUSE_CLIPS_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _find_window() -> int:
        """Return the largest visible top-level dnplayer window."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        process_query_limited_information = 0x1000
        windows: list[tuple[int, int]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(process_query_limited_information, False, pid.value)
            if not process:
                return True
            try:
                size = wintypes.DWORD(32768)
                path = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    return True
                if Path(path.value).name.lower() != "dnplayer.exe":
                    return True
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                windows.append((area, int(hwnd)))
            finally:
                kernel32.CloseHandle(process)
            return True

        user32.EnumWindows(visit, 0)
        if not windows:
            raise SystemExit("ไม่พบหน้าต่าง LDPlayer — กรุณาเปิดหน้าต่างไว้และอย่าปิดลง system tray")
        return max(windows)[1]

    @staticmethod
    def _send_f8(hwnd: int) -> None:
        import ctypes

        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            raise RuntimeError("หน้าต่าง LDPlayer ถูกปิดระหว่างทำงาน")
        vk_f8, wm_keydown, wm_keyup = 0x77, 0x0100, 0x0101
        scan = user32.MapVirtualKeyW(vk_f8, 0)
        user32.PostMessageW(hwnd, wm_keydown, vk_f8, 1 | (scan << 16))
        user32.PostMessageW(hwnd, wm_keyup, vk_f8, 1 | (scan << 16) | 0xC0000000)

    @staticmethod
    def _video_snapshot() -> dict[Path, tuple[int, int]]:
        snapshot = {}
        for directory in LDPLAYER_VIDEO_DIRS:
            if directory.exists():
                for path in directory.rglob("*.mp4"):
                    snapshot[path] = (path.stat().st_mtime_ns, path.stat().st_size)
        return snapshot

    def record(self, frame: np.ndarray, paused: bool) -> None:
        if paused:
            return
        self.start()

    def start(self) -> None:
        if not self.recording:
            if not self.window:
                self.window = self._find_window()
            self.before_files = self._video_snapshot()
            self.stamp = time.strftime("%Y-%m-%d_%H-%M-%S") + f"_{int(time.time() * 1000) % 1000:03d}"
            self._send_f8(self.window)
            self.recording = True
            time.sleep(0.25)
            print(f"เริ่มอัดคลิปด้วย LDPlayer F8: {self.stamp}")

    def finish(self, reason: str) -> None:
        if not self.recording or self.stamp is None:
            return
        self._send_f8(self.window)
        self.recording = False
        final_path = PAUSE_CLIPS_DIR / f"{self.stamp}_{reason}.mp4"
        deadline = time.monotonic() + 15.0
        source = None
        while time.monotonic() < deadline:
            current = self._video_snapshot()
            changed = [path for path, state in current.items() if self.before_files.get(path) != state and state[1] > 0]
            if changed:
                candidate = max(changed, key=lambda path: path.stat().st_mtime_ns)
                size_before = candidate.stat().st_size
                time.sleep(0.3)
                if candidate.exists() and candidate.stat().st_size == size_before:
                    source = candidate
                    break
            time.sleep(0.15)
        if source is None:
            searched = ", ".join(str(path) for path in LDPLAYER_VIDEO_DIRS)
            raise RuntimeError(f"LDPlayer หยุดอัดแล้วแต่ไม่พบ MP4 ใหม่ใน: {searched}")
        shutil.move(str(source), str(final_path))
        self.stamp = None
        clips = sorted(PAUSE_CLIPS_DIR.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old_path in clips[MAX_PAUSE_CLIPS:]:
            old_path.unlink(missing_ok=True)
        print(f"บันทึกคลิปก่อน pause ({reason}): {final_path}")


class ObsClipRecorder:
    """Control OBS recording through its local WebSocket API."""

    def __init__(self, _serial: str) -> None:
        if not OBS_WEBSOCKET_CONFIG.exists():
            raise SystemExit("ไม่พบ config ของ OBS WebSocket")
        config = json.loads(OBS_WEBSOCKET_CONFIG.read_text(encoding="utf-8-sig"))
        if not config.get("server_enabled"):
            raise SystemExit("OBS WebSocket ยังปิดอยู่ — เปิด Tools > WebSocket Server Settings > Enable WebSocket server")
        self.port = int(config.get("server_port", 4455))
        self.password = str(config.get("server_password", ""))
        self.socket: socket.socket | None = None
        self.recv_buffer = b""
        self.recording = False
        self.stamp: str | None = None
        self._connect_or_launch()

    def _connect_or_launch(self) -> None:
        try:
            self._connect()
        except OSError as error:
            raise SystemExit("ยังเชื่อมต่อ OBS ไม่ได้ — กรุณาเปิด OBS ด้วยมือก่อนรัน detector.py") from error

    def _connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)
        headers, self.recv_buffer = response.split(b"\r\n\r\n", 1)
        if b" 101 " not in headers.split(b"\r\n", 1)[0]:
            sock.close()
            raise OSError("OBS WebSocket handshake failed")
        self.socket = sock
        hello = self._receive_json()
        auth_data = hello.get("d", {}).get("authentication")
        identify = {"rpcVersion": 1}
        if auth_data:
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth_data["salt"]).encode()).digest()
            ).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth_data["challenge"]).encode()).digest()
            ).decode()
        self._send_json({"op": 1, "d": identify})
        identified = self._receive_json()
        if identified.get("op") != 2:
            sock.close()
            self.socket = None
            raise SystemExit("OBS WebSocket authentication ไม่สำเร็จ")

    def _send_json(self, value: dict) -> None:
        if self.socket is None:
            raise RuntimeError("OBS WebSocket disconnected")
        payload = json.dumps(value).encode()
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x81])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.extend([0xFE]); header.extend(struct.pack("!H", length))
        else:
            header.extend([0xFF]); header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes(header) + mask + masked)

    def _receive_json(self) -> dict:
        if self.socket is None:
            raise RuntimeError("OBS WebSocket disconnected")
        first = self._recv_exact(2)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if first[1] & 0x80:
            mask = self._recv_exact(4)
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(self._recv_exact(length)))
        else:
            payload = self._recv_exact(length)
        return json.loads(payload.decode())

    def _recv_exact(self, length: int) -> bytes:
        data = self.recv_buffer[:length]
        self.recv_buffer = self.recv_buffer[length:]
        while len(data) < length:
            part = self.socket.recv(length - len(data))
            if not part:
                raise RuntimeError("OBS WebSocket disconnected")
            data += part
        return data

    def _request(self, request_type: str) -> dict:
        request_id = str(uuid.uuid4())
        self._send_json({"op": 6, "d": {"requestType": request_type, "requestId": request_id}})
        while True:
            response = self._receive_json()
            data = response.get("d", {})
            if response.get("op") == 7 and data.get("requestId") == request_id:
                status = data.get("requestStatus", {})
                if not status.get("result"):
                    raise RuntimeError(f"OBS {request_type} ไม่สำเร็จ: {status.get('comment', status.get('code'))}")
                return data.get("responseData", {})

    def record(self, _frame: np.ndarray, paused: bool) -> None:
        if not paused:
            self.start()

    def start(self) -> None:
        if not self.recording:
            self._request("StartRecord")
            self.recording = True
            self.stamp = time.strftime("%Y-%m-%d_%H-%M-%S") + f"_{int(time.time() * 1000) % 1000:03d}"
            print(f"เริ่มอัดคลิปด้วย OBS: {self.stamp}")

    def finish(self, reason: str) -> None:
        if not self.recording or self.stamp is None:
            return
        response = self._request("StopRecord")
        self.recording = False
        source = Path(response.get("outputPath", ""))
        if not source.exists():
            raise RuntimeError(f"OBS หยุดอัดแล้วแต่ไม่พบไฟล์: {source}")
        self.stamp = None
        video_extensions = {".mkv", ".mp4", ".mov", ".flv", ".avi", ".webm", ".ts"}
        clips = sorted(
            (
                path for path in source.parent.iterdir()
                if path.is_file() and path.suffix.lower() in video_extensions
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old_path in clips[MAX_PAUSE_CLIPS:]:
            old_path.unlink(missing_ok=True)
        print(f"OBS หยุดอัด ({reason}): {source} — เก็บ 3 คลิปล่าสุดในโฟลเดอร์นี้")


def pause_macro(macro: AdbMacroRunner, recorder: ObsClipRecorder, reason: str) -> None:
    if not macro.paused.is_set():
        # Freeze the macro first. Finalizing and pulling an MP4 can take a
        # moment, and no macro action should slip through during that work.
        macro.observe_scene(True)
        # Keep the frozen scene briefly so the detected card/button is
        # visible at the end of LDPlayer's native recording.
        time.sleep(POST_PAUSE_RECORD_SECONDS)
        recorder.finish(reason)
    else:
        macro.observe_scene(True)


def resume_macro_with_recording(macro: AdbMacroRunner, recorder: ObsClipRecorder) -> None:
    """Start the recorder before releasing a paused macro timeline."""
    if not macro.paused.is_set():
        return
    if macro.absent_since is None:
        macro.absent_since = time.monotonic()
    elif time.monotonic() - macro.absent_since >= 0.5:
        recorder.start()
        macro.absent_since = None
        macro.set_paused(False)


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


def save_close_template(source: str) -> None:
    image = cv2.imread(source)
    if image is None:
        raise SystemExit(f"อ่านภาพปุ่มไม่ได้: {source}")
    if image.shape[0] < 30 or image.shape[1] < 30:
        raise SystemExit("ภาพปุ่ม X มีขนาดเล็กเกินไป")
    if not cv2.imwrite(str(CLOSE_TEMPLATE), image):
        raise SystemExit("บันทึกภาพต้นแบบปุ่ม X ไม่สำเร็จ")
    print(f"บันทึกภาพต้นแบบปุ่ม X แล้ว: {CLOSE_TEMPLATE}")


def save_upgrade_window_template(source: str) -> None:
    image = cv2.imread(source)
    if image is None:
        raise SystemExit(f"อ่านภาพหน้าต่าง Buy Upgrades ไม่ได้: {source}")
    if not cv2.imwrite(str(UPGRADE_WINDOW_TEMPLATE), image):
        raise SystemExit("บันทึกภาพต้นแบบ Buy Upgrades ไม่สำเร็จ")
    print(f"บันทึกภาพต้นแบบ Buy Upgrades แล้ว: {UPGRADE_WINDOW_TEMPLATE}")


def save_action_template(source: str) -> None:
    image = cv2.imread(source)
    if image is None:
        raise SystemExit(f"อ่านภาพปุ่ม action ไม่ได้: {source}")
    if image.shape[0] < 30 or image.shape[1] < 30:
        raise SystemExit("ภาพปุ่ม action มีขนาดเล็กเกินไป")
    if not cv2.imwrite(str(ACTION_TEMPLATE), image):
        raise SystemExit("บันทึกภาพต้นแบบปุ่ม action ไม่สำเร็จ")
    print(f"บันทึกภาพต้นแบบปุ่ม action แล้ว: {ACTION_TEMPLATE}")


def save_claim_template(source: str) -> None:
    image = cv2.imread(source)
    if image is None:
        raise SystemExit(f"อ่านภาพปุ่ม Claim ไม่ได้: {source}")
    if image.shape[0] < 30 or image.shape[1] < 80:
        raise SystemExit("ภาพปุ่ม Claim มีขนาดเล็กเกินไป")
    if not cv2.imwrite(str(CLAIM_TEMPLATE), image):
        raise SystemExit("บันทึกภาพต้นแบบ Claim ไม่สำเร็จ")
    cached_text_template.cache_clear()
    print(f"บันทึกภาพต้นแบบปุ่ม Claim แล้ว: {CLAIM_TEMPLATE}")


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
        # A real card interior is overwhelmingly pale cream. The old 0.48
        # threshold mistook bright desert/gameplay backgrounds for 5 cards.
        if float(cream.mean()) > 0.60:
            active.append(i)
    return active


def white_text_mask(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return (((hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 185)) * 255).astype(np.uint8)


@lru_cache(maxsize=8)
def cached_text_template(path_string: str):
    template = cv2.imread(path_string)
    if template is None:
        return None
    template_h, template_w = template.shape[:2]
    left, right = int(template_w * 0.16), int(template_w * 0.84)
    top, bottom = int(template_h * 0.22), int(template_h * 0.88)
    mask = white_text_mask(template[top:bottom, left:right])
    variants = []
    for scale in TEMPLATE_SCALES:
        width = max(1, int(mask.shape[1] * scale * TEMPLATE_DETECTION_SCALE))
        height = max(1, int(mask.shape[0] * scale * TEMPLATE_DETECTION_SCALE))
        variants.append((scale, cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)))
    return template_h, template_w, left, top, variants


@lru_cache(maxsize=8)
def cached_edge_template(path_string: str, kind: str):
    template = cv2.imread(path_string)
    if template is None:
        return None
    if kind == "upgrade":
        height, width = template.shape[:2]
        template = template[int(height * 0.05):int(height * 0.30), int(width * 0.04):int(width * 0.47)]
    edge = cv2.Canny(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY), 60, 150)
    variants = []
    for scale in TEMPLATE_SCALES:
        width = max(1, int(edge.shape[1] * scale * TEMPLATE_DETECTION_SCALE))
        height = max(1, int(edge.shape[0] * scale * TEMPLATE_DETECTION_SCALE))
        variants.append((scale, cv2.resize(edge, (width, height), interpolation=cv2.INTER_AREA)))
    return template.shape[:2], variants


@lru_cache(maxsize=2)
def cached_close_template(path_string: str):
    template = cv2.imread(path_string)
    if template is None:
        return None
    hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
    mask = (((hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 200)) * 255).astype(np.uint8)
    variants = []
    for scale in TEMPLATE_SCALES:
        width = max(1, int(mask.shape[1] * scale * TEMPLATE_DETECTION_SCALE))
        height = max(1, int(mask.shape[0] * scale * TEMPLATE_DETECTION_SCALE))
        variants.append((scale, cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)))
    return template.shape[:2], variants


@lru_cache(maxsize=2)
def cached_action_template(path_string: str):
    template = cv2.imread(path_string)
    if template is None:
        return None
    variants = []
    for scale in TEMPLATE_SCALES:
        width = max(1, int(template.shape[1] * scale * TEMPLATE_DETECTION_SCALE))
        height = max(1, int(template.shape[0] * scale * TEMPLATE_DETECTION_SCALE))
        resized = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
        edge = cv2.Canny(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY), 60, 150)
        variants.append((scale, edge))
    return template.shape[:2], variants


def find_button(
    frame: np.ndarray, grid: list[float], template_path: Path, threshold: float = 0.82
) -> tuple[int, int, int, int] | None:
    """Find button text in a half-size popup ROI using cached templates."""
    data = cached_text_template(str(template_path)) if template_path.exists() else None
    if data is None:
        return None
    template_h, template_w, crop_left, crop_top, variants = data
    screen_h, screen_w = frame.shape[:2]
    gx, gy, gw, gh = grid
    gx, gy, gw, gh = gx * screen_w, gy * screen_h, gw * screen_w, gh * screen_h
    x1 = max(0, int(gx - gw * 0.25))
    y1 = max(0, int(gy - gh * 0.25))
    x2 = min(screen_w, int(gx + gw * 1.25))
    y2 = min(screen_h, int(gy + gh * 1.15))
    search = frame[y1:y2, x1:x2]
    search_mask = cv2.resize(
        white_text_mask(search), None,
        fx=TEMPLATE_DETECTION_SCALE, fy=TEMPLATE_DETECTION_SCALE,
        interpolation=cv2.INTER_NEAREST,
    )
    best_score, best_box = 0.0, None
    for scale, resized in variants:
        height, width = resized.shape[:2]
        if width > search_mask.shape[1] or height > search_mask.shape[0]:
            continue
        _, score, _, location = cv2.minMaxLoc(
            cv2.matchTemplate(search_mask, resized, cv2.TM_CCOEFF_NORMED)
        )
        if score > best_score:
            best_score = float(score)
            text_x = location[0] / TEMPLATE_DETECTION_SCALE
            text_y = location[1] / TEMPLATE_DETECTION_SCALE
            button_x = int(x1 + text_x - crop_left * scale)
            button_y = int(y1 + text_y - crop_top * scale)
            button_w, button_h = int(template_w * scale), int(template_h * scale)
            best_box = (
                max(0, button_x), max(0, button_y),
                min(screen_w, button_x + button_w), min(screen_h, button_y + button_h),
            )
    return best_box if best_score >= threshold else None


def find_cancel_button(frame: np.ndarray, grid: list[float]) -> tuple[int, int, int, int] | None:
    return find_button(frame, grid, CANCEL_TEMPLATE, threshold=0.75)


def find_claim_button(frame: np.ndarray, grid: list[float]) -> tuple[int, int, int, int] | None:
    """Find the Claim text and require a substantial green button background."""
    box = find_button(frame, grid, CLAIM_TEMPLATE, threshold=0.72)
    if box is None:
        return None
    x1, y1, x2, y2 = box
    button = frame[y1:y2, x1:x2]
    if button.size == 0:
        return None
    hsv = cv2.cvtColor(button, cv2.COLOR_BGR2HSV)
    green = (
        (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 90) &
        (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 65)
    )
    return box if float(green.mean()) >= 0.18 else None


def _normalized_glyph(mask: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = box
    glyph = mask[y:y + h, x:x + w]
    return cv2.resize(glyph, (24, 32), interpolation=cv2.INTER_NEAREST)


def _glyph_match(
    mask: np.ndarray,
    lhs: tuple[int, int, int, int],
    rhs: tuple[int, int, int, int],
) -> tuple[bool, float, float]:
    """Compare printed digits robustly despite per-position anti-aliasing."""
    lx, ly, lw, lh = lhs
    rx, ry, rw, rh = rhs
    raw_a = mask[ly:ly + lh, lx:lx + lw]
    raw_b = mask[ry:ry + rh, rx:rx + rw]
    contours_a, _ = cv2.findContours(raw_a, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours_b, _ = cv2.findContours(raw_b, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours_a or not contours_b:
        return False, 0.0, float("inf")
    contour_a = max(contours_a, key=cv2.contourArea)
    contour_b = max(contours_b, key=cv2.contourArea)
    shape_distance = float(cv2.matchShapes(contour_a, contour_b, cv2.CONTOURS_MATCH_I1, 0.0))
    a = _normalized_glyph(mask, lhs)
    b = _normalized_glyph(mask, rhs)
    pixel_similarity = float(
        1.0 - np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))) / 255.0
    )
    return shape_distance <= 1.0 and pixel_similarity >= 0.72, pixel_similarity, shape_distance


def find_completed_counter(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Find a top-screen N/N counter by comparing glyphs around its slash."""
    screen_h, screen_w = frame.shape[:2]
    # This reward counter occupies a stable HUD slot. Searching the whole top
    # band caused unrelated scores/text with diagonal strokes to look like N/N.
    hud_x1, hud_x2 = int(screen_w * 0.34), int(screen_w * 0.52)
    hud_y1, hud_y2 = int(screen_h * 0.08), int(screen_h * 0.22)
    roi_h = max(1, hud_y2)
    roi = frame[:roi_h]
    mask = white_text_mask(roi)
    # Join outlines within each printed glyph without merging adjacent digits.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    components: list[tuple[int, int, int, int]] = []
    min_h = max(10, int(screen_h * 0.014))
    max_h = max(min_h + 1, int(screen_h * 0.09))
    for x, y, w, h, area in stats[1:count]:
        if min_h <= h <= max_h and area >= max(8, h // 2) and w <= h * 1.25:
            components.append((int(x), int(y), int(w), int(h)))

    for slash in components:
        sx, sy, sw, sh = slash
        slash_center_x = sx + sw / 2
        slash_center_y = sy + sh / 2
        if not (hud_x1 <= slash_center_x <= hud_x2 and hud_y1 <= slash_center_y <= hud_y2):
            continue
        if not (0.22 <= sw / sh <= 0.75):
            continue
        points = np.column_stack(np.where(mask[sy:sy + sh, sx:sx + sw] > 0))
        if len(points) < 6:
            continue
        # A slash slopes from top-right to bottom-left in image coordinates.
        if float(np.corrcoef(points[:, 0], points[:, 1])[0, 1]) > -0.35:
            continue
        center_y = sy + sh / 2
        peers = [
            item for item in components
            if item != slash
            and abs((item[1] + item[3] / 2) - center_y) <= sh * 0.28
            and 0.65 <= item[3] / sh <= 1.35
        ]
        left = sorted(
            (item for item in peers if item[0] + item[2] <= sx and sx - (item[0] + item[2]) <= sh * 2.8),
            key=lambda item: item[0],
        )
        right = sorted(
            (item for item in peers if item[0] >= sx + sw and item[0] - (sx + sw) <= sh * 2.8),
            key=lambda item: item[0],
        )
        # Support one- and multi-digit values, but reject unrelated long text.
        if not left or len(left) != len(right) or len(left) > 3:
            continue
        similarities = []
        shape_distances = []
        all_matched = True
        for lhs, rhs in zip(left, right):
            matched, similarity, shape_distance = _glyph_match(mask, lhs, rhs)
            similarities.append(similarity)
            shape_distances.append(shape_distance)
            if not matched:
                all_matched = False
                break
        if not all_matched or len(similarities) != len(left):
            continue
        x1 = max(0, min(item[0] for item in left) - int(sh * 2.2))
        y1 = max(0, min(item[1] for item in left + [slash] + right) - int(sh * 0.55))
        x2 = min(screen_w, max(item[0] + item[2] for item in left + [slash] + right) + int(sh * 0.55))
        y2 = min(roi_h, max(item[1] + item[3] for item in left + [slash] + right) + int(sh * 0.55))
        print(
            f"พบตัวนับเลขเท่ากัน ความเหมือน {min(similarities) * 100:.1f}% "
            f"(shape {max(shape_distances):.3f})"
        )
        return x1, y1, x2, y2
    return None


def find_close_button(frame: np.ndarray, grid: list[float]) -> tuple[int, int, int, int] | None:
    """Find the white X mark in the top screen band."""
    data = cached_close_template(str(CLOSE_TEMPLATE)) if CLOSE_TEMPLATE.exists() else None
    if data is None:
        return None
    screen_h, screen_w = frame.shape[:2]
    (template_h, template_w), variants = data
    x1, y1, x2, y2 = 0, 0, screen_w, min(screen_h, int(screen_h * 0.35))
    search_hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    search_mask = (((search_hsv[:, :, 1] < 60) & (search_hsv[:, :, 2] > 200)) * 255).astype(np.uint8)
    search_mask = cv2.resize(
        search_mask, None,
        fx=TEMPLATE_DETECTION_SCALE, fy=TEMPLATE_DETECTION_SCALE,
        interpolation=cv2.INTER_NEAREST,
    )
    best_score, best_box = 0.0, None
    for scale, mask in variants:
        height, width = mask.shape[:2]
        if width > search_mask.shape[1] or height > search_mask.shape[0]:
            continue
        result = cv2.matchTemplate(search_mask, mask, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            bx = int(location[0] / TEMPLATE_DETECTION_SCALE)
            by = int(location[1] / TEMPLATE_DETECTION_SCALE)
            full_w, full_h = int(template_w * scale), int(template_h * scale)
            best_box = (x1 + bx, y1 + by, x1 + bx + full_w, y1 + by + full_h)
    return best_box if best_score >= 0.78 else None


def is_upgrade_window(frame: np.ndarray) -> bool:
    """Recognize the Buy Upgrades header whose X button must be ignored."""
    data = cached_edge_template(str(UPGRADE_WINDOW_TEMPLATE), "upgrade") if UPGRADE_WINDOW_TEMPLATE.exists() else None
    if data is None:
        return False
    _, variants = data
    top = frame[:max(1, int(frame.shape[0] * 0.30)), :]
    top = cv2.resize(
        top, None, fx=TEMPLATE_DETECTION_SCALE, fy=TEMPLATE_DETECTION_SCALE,
        interpolation=cv2.INTER_AREA,
    )
    frame_edge = cv2.Canny(cv2.cvtColor(top, cv2.COLOR_BGR2GRAY), 60, 150)
    best_score = 0.0
    for _scale, resized in variants:
        th, tw = resized.shape[:2]
        if tw > frame_edge.shape[1] or th > frame_edge.shape[0]:
            continue
        result = cv2.matchTemplate(frame_edge, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        best_score = max(best_score, float(score))
    return best_score >= 0.68


def find_action_button(
    frame: np.ndarray,
) -> tuple[tuple[int, int, int, int], float] | None:
    """Find the action icon in the central gameplay area and return its score."""
    data = cached_action_template(str(ACTION_TEMPLATE)) if ACTION_TEMPLATE.exists() else None
    if data is None:
        return None
    (template_h, template_w), variants = data
    screen_h, screen_w = frame.shape[:2]
    # The action prompt appears in the gameplay area. Cropping avoids UI icons
    # around the edges and makes matching substantially faster.
    roi_x1, roi_y1 = int(screen_w * 0.20), int(screen_h * 0.18)
    roi_x2, roi_y2 = int(screen_w * 0.80), int(screen_h * 0.82)
    search = cv2.resize(
        frame[roi_y1:roi_y2, roi_x1:roi_x2],
        None,
        fx=TEMPLATE_DETECTION_SCALE,
        fy=TEMPLATE_DETECTION_SCALE,
        interpolation=cv2.INTER_AREA,
    )
    search_edge = cv2.Canny(cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), 60, 150)
    best_score, best_box = 0.0, None
    for scale, edge in variants:
        height, width = edge.shape[:2]
        if width > search_edge.shape[1] or height > search_edge.shape[0]:
            continue
        _, score, _, location = cv2.minMaxLoc(
            cv2.matchTemplate(search_edge, edge, cv2.TM_CCOEFF_NORMED)
        )
        if score > best_score:
            best_score = float(score)
            x = roi_x1 + int(location[0] / TEMPLATE_DETECTION_SCALE)
            y = roi_y1 + int(location[1] / TEMPLATE_DETECTION_SCALE)
            width_full, height_full = int(template_w * scale), int(template_h * scale)
            best_box = (x, y, x + width_full, y + height_full)
    return (best_box, best_score) if best_box is not None and best_score >= ACTION_THRESHOLD else None


def find_confirm_button(frame: np.ndarray, grid: list[float]) -> tuple[int, int, int, int] | None:
    box = find_button(frame, grid, CONFIRM_TEMPLATE, threshold=0.75)
    if box is None:
        return None
    x1, y1, x2, y2 = box
    button = frame[y1:y2, x1:x2]
    if button.size == 0:
        return None
    hsv = cv2.cvtColor(button, cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    green = (
        (hue >= 35) & (hue <= 85) &
        (saturation > 80) & (value > 70)
    )
    # Confirm-like words on cyan/blue buttons must not pause or be tapped.
    return box if float(green.mean()) >= 0.20 else None


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
    action_enabled: bool,
    completed_counter_enabled: bool,
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
    clip_recorder = None
    if macro_file:
        if not isinstance(backend, AdbBackend):
            raise SystemExit("--macro-file ใช้ได้เฉพาะ backend adb")
        macro_path = Path(macro_file)
        if not macro_path.is_absolute():
            macro_path = OPERATION_RECORDS_DIR / macro_path
        if not macro_path.exists():
            raise SystemExit(f"ไม่พบ macro file: {macro_path}")
        macro = AdbMacroRunner(macro_path, backend, loop=not macro_once)
        # OBS clip recording is temporarily disabled.
        # clip_recorder = ObsClipRecorder(backend.serial)
        # clip_recorder.start()
        macro.start()
    pending = None
    candidate_slot, stable_frames = None, 0
    close_latched = False
    close_last_tap, close_tap_attempts = 0.0, 0
    action_latched = False
    cancel_latched = False
    cancel_last_tap, cancel_tap_attempts = 0.0, 0
    completed_counter_latched = False
    waiting_for_claim = False
    claim_clicked = False
    claim_started_at = 0.0
    claim_last_tap = 0.0
    claim_tap_attempts = 0
    if backend_name == "screen":
        pyautogui.FAILSAFE = True
        print("เริ่ม screen backend — เลื่อนเมาส์ไปมุมซ้ายบนเพื่อหยุดฉุกเฉิน หรือกด Q")
    else:
        print(f"เริ่ม ADB backend ({backend.serial}) — เมาส์จริงจะไม่ถูกขยับ; กด Q เพื่อหยุด")
    while True:
        if macro and macro.error:
            raise SystemExit(f"ADB macro หยุดเพราะข้อผิดพลาด: {macro.error}")
        frame = backend.grab()
        if macro and clip_recorder:
            clip_recorder.record(frame, macro.paused.is_set())

        if waiting_for_claim:
            # Claim flow has exclusive priority. X, Cancel and Confirm must not
            # run until the Claim tap has succeeded and the button disappears.
            if macro:
                macro.observe_scene(True)
            candidate_slot, stable_frames = None, 0
            claim = find_claim_button(frame, grid)
            now = time.time()
            if claim:
                if click and claim_tap_attempts < 5 and now - claim_last_tap >= 0.65:
                    x1, y1, x2, y2 = claim
                    backend.tap((x1 + x2) // 2, (y1 + y2) // 2)
                    claim_clicked = True
                    claim_tap_attempts += 1
                    claim_last_tap = now
                    last_click = now
                    print(f"คลิกปุ่ม Claim ครั้งที่ {claim_tap_attempts}/5")
                time.sleep(interval)
                continue
            if claim_clicked:
                waiting_for_claim = False
                print("Claim หายแล้ว — เปิดการตรวจ X, Cancel และ Confirm ตามปกติ")
                time.sleep(interval)
                continue
            if now - claim_started_at < 6.0:
                time.sleep(interval)
                continue
            waiting_for_claim = False
            print("ไม่พบ Claim ภายใน 6 วินาที — ยกเลิกโหมดบล็อกปุ่ม")
            time.sleep(interval)
            continue

        completed_counter = find_completed_counter(frame) if completed_counter_enabled else None
        if completed_counter:
            if not completed_counter_latched:
                completed_counter_latched = True
                x1, y1, x2, y2 = completed_counter
                if click:
                    backend.tap((x1 + x2) // 2, (y1 + y2) // 2)
                    print("คลิกตัวนับที่เลขหน้า/หลังเท่ากัน — รอปุ่ม Claim")
                else:
                    print("พบตัวนับที่เลขหน้า/หลังเท่ากัน (dry-run)")
                waiting_for_claim = True
                claim_clicked = False
                claim_started_at = time.time()
                claim_last_tap = 0.0
                claim_tap_attempts = 0
                if macro:
                    macro.observe_scene(True)
            time.sleep(interval)
            continue
        completed_counter_latched = False

        upgrade_visible = is_upgrade_window(frame)
        action = (
            find_action_button(frame)
            if action_enabled and not upgrade_visible
            else None
        )
        if action:
            candidate_slot, stable_frames = None, 0
            if not action_latched:
                action_latched = True
                action_box, action_score = action
                x1, y1, x2, y2 = action_box
                if click:
                    tap_x, tap_y = (x1 + x2) // 2, (y1 + y2) // 2
                    backend.tap_burst(tap_x, tap_y, ACTION_BURST_CLICKS)
                    last_click = time.time()
                    print(
                        f"พบปุ่ม action {action_score * 100:.1f}% — "
                        f"คลิกรัว {ACTION_BURST_CLICKS} ครั้งที่ ({tap_x}, {tap_y})"
                    )
                else:
                    print(f"พบปุ่ม action {action_score * 100:.1f}% (dry-run)")
            time.sleep(interval)
            continue
        action_latched = False

        close = None if upgrade_visible else find_close_button(frame, grid)
        if close:
            finish_recording = bool(macro and clip_recorder and not macro.paused.is_set())
            if macro:
                macro.observe_scene(True)
            candidate_slot, stable_frames = None, 0
            if not close_latched:
                close_latched = True
                close_last_tap, close_tap_attempts = 0.0, 0
            now = time.time()
            if (
                click
                and close_tap_attempts < CLOSE_MAX_ATTEMPTS
                and now - close_last_tap >= CLOSE_RETRY_SECONDS
            ):
                x1, y1, x2, y2 = close
                backend.tap((x1 + x2) // 2, (y1 + y2) // 2)
                close_tap_attempts += 1
                close_last_tap = now
                last_click = now
                print(f"คลิกปุ่ม X ครั้งที่ {close_tap_attempts}/{CLOSE_MAX_ATTEMPTS}")
            if finish_recording:
                time.sleep(POST_PAUSE_RECORD_SECONDS)
                clip_recorder.finish("close")
            time.sleep(interval)
            continue
        close_latched = False
        close_last_tap, close_tap_attempts = 0.0, 0

        cancel = find_cancel_button(frame, grid)
        if cancel:
            finish_recording = bool(macro and clip_recorder and not macro.paused.is_set())
            if macro:
                macro.observe_scene(True)
            candidate_slot, stable_frames = None, 0
            if not cancel_latched:
                cancel_latched = True
                cancel_last_tap, cancel_tap_attempts = 0.0, 0
            now = time.time()
            if (
                click
                and cancel_tap_attempts < CANCEL_MAX_ATTEMPTS
                and now - cancel_last_tap >= CANCEL_RETRY_SECONDS
            ):
                x1, y1, x2, y2 = cancel
                tap_x, tap_y = (x1 + x2) // 2, (y1 + y2) // 2
                backend.tap(tap_x, tap_y)
                cancel_tap_attempts += 1
                cancel_last_tap = now
                last_click = now
                print(
                    f"คลิกปุ่ม Cancel ครั้งที่ {cancel_tap_attempts}/{CANCEL_MAX_ATTEMPTS} "
                    f"ตำแหน่ง ({tap_x}, {tap_y})"
                )
            elif not click and cancel_tap_attempts == 0:
                cancel_tap_attempts = 1
                print("พบปุ่ม Cancel (dry-run)")
            if finish_recording:
                time.sleep(POST_PAUSE_RECORD_SECONDS)
                clip_recorder.finish("cancel")
            time.sleep(interval)
            continue
        cancel_latched = False
        cancel_last_tap, cancel_tap_attempts = 0.0, 0

        confirm = find_confirm_button(frame, grid)
        if confirm and time.time() - last_click > 0.30:
            finish_recording = bool(macro and clip_recorder and not macro.paused.is_set())
            if macro:
                macro.observe_scene(True)
            candidate_slot, stable_frames = None, 0
            x1, y1, x2, y2 = confirm
            if click:
                backend.tap((x1 + x2) // 2, (y1 + y2) // 2)
                print("คลิกปุ่ม Confirm")
            last_click = time.time()
            if finish_recording:
                time.sleep(POST_PAUSE_RECORD_SECONDS)
                clip_recorder.finish("confirm")
            time.sleep(interval)
            continue

        boxes = card_boxes(frame, grid)
        active_indices = active_card_indices(frame, boxes)
        active_count = len(active_indices)
        if macro:
            if active_count in (5, 6) and clip_recorder:
                pause_macro(macro, clip_recorder, f"cards_{active_count}")
            elif active_count in (5, 6):
                macro.observe_scene(True)
            elif clip_recorder:
                resume_macro_with_recording(macro, clip_recorder)
            else:
                macro.observe_scene(False)

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
            ready_at = time.time() + delay if delay > 0 else 0.0
            candidate_slot, stable_frames = None, 0
            if delay > 0:
                print(f"พบการ์ด {active_count} ใบ — รอ {delay:.1f} วินาทีก่อนตรวจ")
            else:
                print(f"พบการ์ด {active_count} ใบ — เริ่มตรวจทันที")
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
    if clip_recorder:
        clip_recorder.finish("stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="ตรวจจับการ์ดที่ต่างจากการ์ดส่วนใหญ่")
    parser.add_argument("--ui", action="store_true", help="เปิดหน้าต่างควบคุมแบบ GUI")
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
    parser.add_argument("--close-template", metavar="IMAGE", help="บันทึกภาพปุ่ม X เป็นต้นแบบ")
    parser.add_argument("--upgrade-window-template", metavar="IMAGE", help="บันทึกหน้าต่าง Buy Upgrades ที่ต้องยกเว้นปุ่ม X")
    parser.add_argument("--action-template", metavar="IMAGE", help="บันทึกภาพปุ่ม action ที่ต้องตรวจและกด")
    parser.add_argument("--claim-template", metavar="IMAGE", help="บันทึกภาพปุ่ม Claim เป็นต้นแบบ")
    parser.add_argument(
        "--no-action",
        action="store_true",
        help="ปิดการตรวจและกดปุ่ม action ชั่วคราว",
    )
    parser.add_argument(
        "--no-counter",
        action="store_true",
        help="ปิดการตรวจตัวนับเลขเท่ากันและปุ่ม Claim ชั่วคราว",
    )
    parser.add_argument("--dry-run", action="store_true", help="แสดงผลอย่างเดียว ไม่คลิกจริง")
    parser.add_argument("--no-preview", action="store_true", help="ไม่แสดงหน้าต่างตรวจสอบ")
    parser.add_argument("--interval", type=float, default=0.08, help="เวลาระหว่างเฟรม (วินาที)")
    parser.add_argument("--delay", type=float, default=0.0, help="เวลารอหลังพบหรือจำนวนการ์ดเปลี่ยน (ค่าเริ่มต้น 0)")
    learning_group = parser.add_mutually_exclusive_group()
    learning_group.add_argument("--learning", dest="learning", action="store_true", help="เปิดระบบเรียนรู้ (ค่าเริ่มต้น)")
    learning_group.add_argument("--no-learning", dest="learning", action="store_false", help="ปิดระบบเรียนรู้ชั่วคราว")
    parser.set_defaults(learning=True)
    parser.add_argument("--reset-learning", action="store_true", help="ล้างข้อมูลเรียนรู้ทั้งหมด")
    parser.add_argument("--learning-stats", action="store_true", help="แสดงจำนวนตัวอย่างที่เรียนรู้")
    args = parser.parse_args()
    if args.ui:
        from detector_ui import main as ui_main
        ui_main()
    elif args.list_monitors:
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
    elif args.close_template:
        save_close_template(args.close_template)
    elif args.upgrade_window_template:
        save_upgrade_window_template(args.upgrade_window_template)
    elif args.action_template:
        save_action_template(args.action_template)
    elif args.claim_template:
        save_claim_template(args.claim_template)
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
            not args.no_action,
            not args.no_counter,
        )


if __name__ == "__main__":
    main()
