from __future__ import annotations

import inspect
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import detector
from detector_ui import build_detector_command


ROOT = Path(__file__).resolve().parents[1]
GRID = [0.265625, 0.2411111111111111, 0.46, 0.7122222222222222]


class DetectionTests(unittest.TestCase):
    def test_completed_counter_fixtures(self) -> None:
        completed = cv2.imread(str(ROOT / "counter_live_debug.png"))
        unrelated = cv2.imread(str(ROOT / "counter_false_debug.png"))
        self.assertIsNotNone(detector.find_completed_counter(completed))
        self.assertIsNone(detector.find_completed_counter(unrelated))

    def test_confirm_requires_green(self) -> None:
        template = cv2.imread(str(detector.CONFIRM_TEMPLATE))
        frame = np.zeros((900, 1600, 3), np.uint8)
        h, w = template.shape[:2]
        # Deliberately place it outside the calibrated card-relative ROI.
        frame[650:650 + h, 20:20 + w] = template
        self.assertIsNotNone(detector.find_confirm_button(frame, GRID))

        hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
        green = (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] > 80)
        hsv[:, :, 0][green] = 95
        blue_template = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        frame[650:650 + h, 20:20 + w] = blue_template
        self.assertIsNone(detector.find_confirm_button(frame, GRID))

    def test_card_scene_supports_six_and_five_cards(self) -> None:
        frame = np.zeros((900, 1600, 3), np.uint8)
        boxes = detector.card_boxes(frame, GRID)
        for x1, y1, x2, y2 in boxes:
            frame[y1:y2, x1:x2] = (195, 230, 245)
        self.assertEqual(detector.active_card_indices(frame, boxes), list(range(6)))
        x1, y1, x2, y2 = boxes[2]
        frame[y1:y2, x1:x2] = 0
        self.assertEqual(detector.active_card_indices(frame, boxes), [0, 1, 3, 4, 5])

    def test_stability_is_one_frame(self) -> None:
        _, frames = detector.update_stability(None, 0, 3)
        self.assertEqual(detector.REQUIRED_STABLE_FRAMES, 1)
        self.assertGreaterEqual(frames, detector.REQUIRED_STABLE_FRAMES)

    def test_runtime_priority_order_is_preserved(self) -> None:
        source = inspect.getsource(detector.run)
        markers = (
            "if waiting_for_claim:",
            "completed_counter =",
            "upgrade_visible =",
            "close =",
            "cancel =",
            "confirm =",
            "boxes = card_boxes",
        )
        positions = [source.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))


class StateTests(unittest.TestCase):
    def test_retry_click_state(self) -> None:
        state = detector.RetryClickState()
        state.begin()
        self.assertTrue(state.ready(10.0, 0.65, 5))
        state.tapped(10.0)
        self.assertFalse(state.ready(10.5, 0.65, 5))
        self.assertTrue(state.ready(10.7, 0.65, 5))
        for moment in (11.0, 12.0, 13.0, 14.0):
            state.tapped(moment)
        self.assertFalse(state.ready(20.0, 0.65, 5))
        state.reset()
        self.assertEqual((state.latched, state.attempts), (False, 0))

    def test_adb_timeout_reconnects_then_succeeds(self) -> None:
        success = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")
        calls = {"main": 0}

        def fake_run(command, **_kwargs):
            if command[-3:] == ["shell", "echo", "ok"]:
                calls["main"] += 1
                if calls["main"] == 1:
                    raise subprocess.TimeoutExpired(command, 5)
            return success

        with (
            mock.patch.object(detector.subprocess, "run", side_effect=fake_run) as run,
            mock.patch.object(detector.time, "sleep"),
        ):
            self.assertEqual(detector.run_adb(["shell", "echo", "ok"], "serial"), b"ok")
        self.assertTrue(any(call.args[0][-1] == "reconnect" for call in run.call_args_list))


class InterfaceTests(unittest.TestCase):
    def test_cli_flags_remain_available(self) -> None:
        parser = detector.build_parser()
        options = {option for action in parser._actions for option in action.option_strings}
        expected = {
            "--ui", "--calibrate", "--backend", "--monitor", "--list-monitors",
            "--adb-device", "--list-adb-devices", "--macro-file", "--macro-once",
            "--confirm-template", "--cancel-template", "--close-template",
            "--upgrade-window-template", "--action-template", "--claim-template",
            "--no-action", "--no-counter", "--dry-run", "--no-preview",
            "--interval", "--delay", "--learning", "--no-learning",
            "--reset-learning", "--learning-stats",
        }
        self.assertTrue(expected.issubset(options))

    def test_ui_command_flags(self) -> None:
        command = build_detector_command(
            device="emulator-5554", macro="6.record", action=False,
            counter=False, learning=False, preview=False, dry_run=True, macro_once=True,
        )
        self.assertEqual(command[3:], [
            "--adb-device", "emulator-5554", "--macro-file", "6.record",
            "--no-action", "--no-counter",
            "--no-learning", "--no-preview", "--dry-run", "--macro-once",
        ])


if __name__ == "__main__":
    unittest.main()
