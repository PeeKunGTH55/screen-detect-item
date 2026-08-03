"""Find and click the odd card in a 2x3 on-screen grid."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import cv2
import mss
import numpy as np
import pyautogui


CONFIG = Path(__file__).with_name("config.json")
CONFIRM_TEMPLATE = Path(__file__).with_name("confirm_template.png")
TRAINING_DIR = Path(__file__).with_name("training_data")
TRAINING_FILE = TRAINING_DIR / "samples.npz"
MAX_SAMPLES_PER_CLASS = 250
ODD_DUPLICATE_DISTANCE = 0.10
REQUIRED_STABLE_FRAMES = 3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def grab_screen() -> np.ndarray:
    with mss.mss() as camera:
        monitor = camera.monitors[1]
        return np.asarray(camera.grab(monitor))[:, :, :3].copy()


def calibrate() -> None:
    frame = grab_screen()
    title = "ลากกรอบคลุมการ์ดทั้ง 6 ช่องตามเส้นแดง แล้วกด ENTER"
    x, y, w, h = map(int, cv2.selectROI(title, frame, False, False))
    cv2.destroyAllWindows()
    if w < 100 or h < 100:
        raise SystemExit("ยกเลิกการตั้งค่า")
    screen_h, screen_w = frame.shape[:2]
    config = {"grid": [x / screen_w, y / screen_h, w / screen_w, h / screen_h]}
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


def find_confirm_button(
    frame: np.ndarray, grid: list[float]
) -> tuple[int, int, int, int] | None:
    """Find the saved Confirm button using multi-scale edge template matching."""
    if not CONFIRM_TEMPLATE.exists():
        return None
    screen_h, screen_w = frame.shape[:2]
    gx, gy, gw, gh = grid
    gx, gy, gw, gh = gx * screen_w, gy * screen_h, gw * screen_w, gh * screen_h
    x1 = max(0, int(gx - gw * 0.45))
    y1 = max(0, int(gy - gh * 0.35))
    x2 = min(screen_w, int(gx + gw * 1.45))
    y2 = min(screen_h, int(gy + gh * 1.25))
    search = frame[y1:y2, x1:x2]
    template = cv2.imread(str(CONFIRM_TEMPLATE))
    if template is None:
        return None
    search_edge = cv2.Canny(cv2.cvtColor(search, cv2.COLOR_BGR2GRAY), 60, 150)
    best_score, best_box = 0.0, None
    # Allow LDPlayer/window scaling while still requiring the exact word/outline.
    for scale in np.linspace(0.60, 1.45, 18):
        tw = int(template.shape[1] * scale)
        th = int(template.shape[0] * scale)
        if tw < 60 or th < 25 or tw > search.shape[1] or th > search.shape[0]:
            continue
        resized = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
        template_edge = cv2.Canny(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY), 60, 150)
        result = cv2.matchTemplate(search_edge, template_edge, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            bx, by = location
            best_box = (x1 + bx, y1 + by, x1 + bx + tw, y1 + by + th)
    return best_box if best_score >= 0.40 else None


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


def run(click: bool, interval: float, delay: float, show: bool, learning_enabled: bool) -> None:
    if not CONFIG.exists():
        raise SystemExit("ยังไม่ได้ตั้งพื้นที่ กรุณารัน: python detector.py --calibrate")
    saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    if "grid" not in saved:
        raise SystemExit("กรุณาตั้งกรอบแบบใหม่: python detector.py --calibrate")
    grid = saved["grid"]
    last_click, previous_count, ready_at = 0.0, 0, 0.0
    learning = LearningStore(learning_enabled)
    pending = None
    candidate_slot, stable_frames = None, 0
    pyautogui.FAILSAFE = True
    print("เริ่มตรวจจับแล้ว — เลื่อนเมาส์ไปมุมซ้ายบนเพื่อหยุดฉุกเฉิน หรือกด Q ในหน้าต่าง Preview")
    while True:
        frame = grab_screen()
        confirm = find_confirm_button(frame, grid)
        if confirm and time.time() - last_click > 0.30:
            candidate_slot, stable_frames = None, 0
            x1, y1, x2, y2 = confirm
            if click:
                pyautogui.click((x1 + x2) // 2, (y1 + y2) // 2, duration=0.06)
                print("คลิกปุ่ม Confirm")
            last_click = time.time()
            time.sleep(interval)
            continue

        boxes = card_boxes(frame, grid)
        active_indices = active_card_indices(frame, boxes)
        active_count = len(active_indices)

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
            click_x, click_y = (x1 + x2) // 2, (y1 + y2) // 2
            pyautogui.click(click_x, click_y, duration=0.06)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="ตรวจจับการ์ดที่ต่างจากการ์ดส่วนใหญ่")
    parser.add_argument("--calibrate", action="store_true", help="เลือกกรอบรวมของการ์ด 6 ช่อง")
    parser.add_argument("--confirm-template", metavar="IMAGE", help="บันทึกภาพปุ่ม Confirm เป็นต้นแบบ")
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
    if args.reset_learning:
        reset_learning()
    elif args.learning_stats:
        print_learning_stats()
    elif args.confirm_template:
        save_confirm_template(args.confirm_template)
    elif args.calibrate:
        calibrate()
    else:
        run(not args.dry_run, args.interval, args.delay, not args.no_preview, args.learning)


if __name__ == "__main__":
    main()
