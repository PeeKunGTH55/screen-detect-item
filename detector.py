"""Find and click the odd card in a 2x3 on-screen grid."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import mss
import numpy as np
import pyautogui


CONFIG = Path(__file__).with_name("config.json")


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


def find_odd(frame: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> tuple[int, np.ndarray]:
    features = [feature(frame[y1:y2, x1:x2]) for x1, y1, x2, y2 in boxes]
    count = len(features)
    distances = np.zeros((count, count), dtype=np.float32)
    for i in range(count):
        for j in range(i + 1, count):
            distances[i, j] = distances[j, i] = np.mean(np.abs(features[i] - features[j]))
    # The normal pose is the medoid: the card with the smallest distance to others.
    medoid = int(np.argmin(np.median(distances + np.eye(count) * 99, axis=1)))
    scores = distances[medoid]
    return int(np.argmax(scores)), scores


def run(click: bool, threshold: float, interval: float, delay: float, show: bool) -> None:
    if not CONFIG.exists():
        raise SystemExit("ยังไม่ได้ตั้งพื้นที่ กรุณารัน: python detector.py --calibrate")
    saved = json.loads(CONFIG.read_text(encoding="utf-8"))
    if "grid" not in saved:
        raise SystemExit("กรุณาตั้งกรอบแบบใหม่: python detector.py --calibrate")
    grid = saved["grid"]
    last_click, previous_count, ready_at = 0.0, 0, 0.0
    pyautogui.FAILSAFE = True
    print("เริ่มตรวจจับแล้ว — เลื่อนเมาส์ไปมุมซ้ายบนเพื่อหยุดฉุกเฉิน หรือกด Q ในหน้าต่าง Preview")
    while True:
        frame = grab_screen()
        boxes = card_boxes(frame, grid)
        active_indices = active_card_indices(frame, boxes)
        active_count = len(active_indices)
        valid_scene = active_count in (5, 6)
        if valid_scene and active_count != previous_count:
            ready_at = time.time() + delay
            print(f"พบการ์ด {active_count} ใบ — รอ {delay:.1f} วินาทีก่อนตรวจ")
        previous_count = active_count if valid_scene else 0
        if not valid_scene or time.time() < ready_at:
            time.sleep(interval)
            continue
        active_boxes = [boxes[i] for i in active_indices]
        candidate_pos, scores = find_odd(frame, active_boxes)
        candidate = active_indices[candidate_pos]
        # Compare the strongest outlier with the normal cluster. This still works
        # when two cards briefly show the jumping animation in the same frame.
        confidence = float(scores[candidate_pos] - np.median(scores))
        if click and confidence >= threshold and time.time() - last_click > 1.2:
            x1, y1, x2, y2 = boxes[candidate]
            pyautogui.click((x1 + x2) // 2, (y1 + y2) // 2)
            print(f"คลิกใบที่ {candidate + 1} (ความมั่นใจ {confidence:.3f})")
            last_click = time.time()

        if show:
            preview = frame.copy()
            score_by_slot = {slot: scores[pos] for pos, slot in enumerate(active_indices)}
            for i, (x1, y1, x2, y2) in enumerate(boxes):
                color = (0, 0, 255) if i == candidate else ((0, 200, 0) if i in score_by_slot else (120, 120, 120))
                cv2.rectangle(preview, (x1, y1), (x2, y2), color, 3)
                label = f"{i + 1}: {score_by_slot[i]:.2f}" if i in score_by_slot else f"{i + 1}: empty"
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
    parser.add_argument("--dry-run", action="store_true", help="แสดงผลอย่างเดียว ไม่คลิกจริง")
    parser.add_argument("--no-preview", action="store_true", help="ไม่แสดงหน้าต่างตรวจสอบ")
    parser.add_argument("--threshold", type=float, default=0.08, help="ส่วนต่างขั้นต่ำก่อนคลิก")
    parser.add_argument("--interval", type=float, default=0.08, help="เวลาระหว่างเฟรม (วินาที)")
    parser.add_argument("--delay", type=float, default=0.5, help="เวลารอหลังพบกรอบ (วินาที)")
    args = parser.parse_args()
    if args.calibrate:
        calibrate()
    else:
        run(not args.dry_run, args.threshold, args.interval, args.delay, not args.no_preview)


if __name__ == "__main__":
    main()
