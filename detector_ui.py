"""Simple desktop launcher for detector.py."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


ROOT = Path(__file__).resolve().parent
DETECTOR = ROOT / "detector.py"
ADB = Path(r"C:\LDPlayer\LDPlayer14\adb.exe")
MACRO_DIR = Path(r"C:\LDPlayer\LDPlayer14\vms\operationRecords")


class DetectorUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Odd Card Detector")
        self.root.geometry("820x590")
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[str] = queue.Queue()

        panel = ttk.Frame(root, padding=12)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="ADB device").grid(row=0, column=0, sticky="w", pady=4)
        self.device = ttk.Combobox(panel, state="readonly")
        self.device.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(panel, text="Refresh ADB", command=self.refresh_devices).grid(row=0, column=2)

        ttk.Label(panel, text="Macro").grid(row=1, column=0, sticky="w", pady=4)
        self.macro = ttk.Combobox(panel, state="readonly")
        self.macro.grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(panel, text="Refresh Macro", command=self.refresh_macros).grid(row=1, column=2)

        options = ttk.Frame(panel)
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)
        self.action = tk.BooleanVar(value=True)
        self.counter = tk.BooleanVar(value=True)
        self.learning = tk.BooleanVar(value=True)
        self.preview = tk.BooleanVar(value=True)
        self.dry_run = tk.BooleanVar(value=False)
        self.macro_once = tk.BooleanVar(value=False)
        for text, variable in (
            ("ตรวจปุ่ม Action", self.action),
            ("ตรวจเลข/Claim", self.counter),
            ("Learning", self.learning),
            ("Preview", self.preview),
            ("Dry run", self.dry_run),
            ("Macro รอบเดียว", self.macro_once),
        ):
            ttk.Checkbutton(options, text=text, variable=variable).pack(side="left", padx=(0, 12))

        buttons = ttk.Frame(panel)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.start_button = ttk.Button(buttons, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Calibrate ADB", command=self.calibrate).pack(side="left")
        self.status = ttk.Label(buttons, text="พร้อมใช้งาน")
        self.status.pack(side="right")

        self.log = tk.Text(panel, wrap="word", height=25, bg="#111827", fg="#e5e7eb")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew")
        panel.rowconfigure(4, weight=1)

        self.refresh_devices()
        self.refresh_macros()
        self.root.after(100, self.drain_messages)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def append(self, message: str) -> None:
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")

    def refresh_devices(self) -> None:
        try:
            result = subprocess.run(
                [str(ADB), "devices"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=6, check=False,
            )
            devices = [
                line.split()[0] for line in result.stdout.splitlines()[1:]
                if len(line.split()) >= 2 and line.split()[1] == "device"
            ]
        except (OSError, subprocess.TimeoutExpired) as error:
            devices = []
            self.append(f"อ่าน ADB devices ไม่สำเร็จ: {error}")
        self.device["values"] = devices
        if devices:
            self.device.current(0)
            self.status.config(text=f"พบ {len(devices)} device")
        else:
            self.device.set("")
            self.status.config(text="ไม่พบ ADB device")

    def refresh_macros(self) -> None:
        names = [""] + sorted(path.name for path in MACRO_DIR.glob("*.record"))
        self.macro["values"] = names
        if self.macro.get() not in names:
            self.macro.current(0)

    def build_command(self) -> list[str]:
        command = [sys.executable, "-u", str(DETECTOR)]
        if self.device.get():
            command += ["--adb-device", self.device.get()]
        if self.macro.get():
            command += ["--macro-file", self.macro.get()]
        if not self.action.get():
            command.append("--no-action")
        if not self.counter.get():
            command.append("--no-counter")
        if not self.learning.get():
            command.append("--no-learning")
        if not self.preview.get():
            command.append("--no-preview")
        if self.dry_run.get():
            command.append("--dry-run")
        if self.macro_once.get():
            command.append("--macro-once")
        return command

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if not self.device.get():
            messagebox.showerror("ADB", "ไม่พบ ADB device กรุณากด Refresh ADB")
            return
        command = self.build_command()
        self.append("\n> " + subprocess.list2cmdline(command))
        try:
            self.process = subprocess.Popen(
                command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            messagebox.showerror("Start", str(error))
            return
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.status.config(text="กำลังทำงาน")
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self) -> None:
        assert self.process is not None
        if self.process.stdout:
            for line in self.process.stdout:
                self.messages.put(line)
        code = self.process.wait()
        self.messages.put(f"\nโปรแกรมหยุด (exit code {code})\n")
        self.messages.put("__STOPPED__")

    def drain_messages(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                if message == "__STOPPED__":
                    self.start_button.config(state="normal")
                    self.stop_button.config(state="disabled")
                    self.status.config(text="หยุดแล้ว")
                else:
                    self.append(message)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_messages)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.status.config(text="กำลังหยุด...")

    def calibrate(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Calibrate", "กรุณากด Stop ก่อน calibrate")
            return
        command = [sys.executable, str(DETECTOR), "--calibrate", "--backend", "adb"]
        if self.device.get():
            command += ["--adb-device", self.device.get()]
        subprocess.Popen(command, cwd=ROOT)

    def close(self) -> None:
        self.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    DetectorUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
