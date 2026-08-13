#!/usr/bin/env python3
"""
webcam_gui.py - desktop window that controls the stream.

Pick a camera, set login and password, hit Start. Settings are remembered in
settings.json next to the program; the password is encrypted by Windows.

Run from source:   python webcam_gui.py
Build an exe:      build_exe.bat
"""

from __future__ import annotations

import os
import queue
import secrets
import string
import sys
import threading
import tkinter as tk
import webbrowser
from http.server import ThreadingHTTPServer
from tkinter import messagebox, ttk

import settings_store
import webcam_stream as ws

APP_TITLE = "Webcam Stream"
RESOLUTIONS = ["640x480", "854x480", "1280x720", "1920x1080"]


class LogPipe:
    """Redirects stdout so prints from webcam_stream land in the window.

    In a windowed exe sys.stdout is None and a plain print() would crash the app,
    so this fixes that too.
    """

    def __init__(self, sink: queue.Queue):
        self.sink = sink
        self._buffer = ""

    def write(self, text):
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self.sink.put(("log", line))
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.camera = None
        self.server = None
        self.tunnel = None
        self.running = False
        self.busy = False

        sys.stdout = sys.stderr = LogPipe(self.events)

        self.settings = settings_store.load()
        self.can_encrypt = settings_store.password_is_encrypted()

        root.title(APP_TITLE)
        root.minsize(560, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._apply_settings()
        self.root.after(100, self._drain_events)

        if self.settings.get("autostart"):
            self.log("autostart is on, starting in 2 seconds...")
            self.root.after(2000, self.start)


    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        root = self.root

        box = ttk.LabelFrame(root, text="Camera")
        box.pack(fill="x", **pad)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="Device:").pack(side="left")
        self.camera_var = tk.StringVar()
        self.camera_box = ttk.Combobox(row, textvariable=self.camera_var, state="readonly", width=34)
        self.camera_box.pack(side="left", padx=6)
        self.scan_btn = ttk.Button(row, text="Find cameras", command=self.scan_cameras)
        self.scan_btn.pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(row, text="Resolution:").pack(side="left")
        self.res_var = tk.StringVar()
        ttk.Combobox(row, textvariable=self.res_var, values=RESOLUTIONS,
                     state="readonly", width=11).pack(side="left", padx=6)
        ttk.Label(row, text="FPS:").pack(side="left", padx=(10, 0))
        self.fps_var = tk.IntVar()
        ttk.Spinbox(row, from_=1, to=30, textvariable=self.fps_var, width=5).pack(side="left", padx=6)
        ttk.Label(row, text="Quality:").pack(side="left", padx=(10, 0))
        self.quality_var = tk.IntVar()
        ttk.Spinbox(row, from_=20, to=95, increment=5, textvariable=self.quality_var,
                    width=5).pack(side="left", padx=6)

        box = ttk.LabelFrame(root, text="Access")
        box.pack(fill="x", **pad)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="Login:", width=10).pack(side="left")
        self.user_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.user_var, width=22).pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(row, text="Password:", width=10).pack(side="left")
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(row, textvariable=self.password_var, width=22, show="•")
        self.password_entry.pack(side="left")
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="show", variable=self.show_var,
                        command=self._toggle_password).pack(side="left", padx=6)
        ttk.Button(row, text="Generate", command=self._generate_password).pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Remember password", variable=self.remember_var).pack(side="left")
        note = ("encrypted by Windows" if self.can_encrypt
                else "warning: stored as plain text")
        ttk.Label(row, text=f"({note})", foreground="#777").pack(side="left", padx=6)

        box = ttk.LabelFrame(root, text="Network")
        box.pack(fill="x", **pad)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        self.tunnel_var = tk.BooleanVar()
        ttk.Checkbutton(row, text="Publish a link to the internet (Cloudflare)",
                        variable=self.tunnel_var).pack(side="left")
        ttk.Label(row, text="Port:").pack(side="left", padx=(16, 0))
        self.port_var = tk.IntVar()
        ttk.Spinbox(row, from_=1024, to=65535, textvariable=self.port_var,
                    width=7).pack(side="left", padx=6)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.autostart_var = tk.BooleanVar()
        ttk.Checkbutton(row, text="Start streaming as soon as the app opens",
                        variable=self.autostart_var).pack(side="left")

        row = ttk.Frame(root)
        row.pack(fill="x", **pad)
        self.start_btn = ttk.Button(row, text="Start streaming", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="stopped")
        ttk.Label(row, textvariable=self.status_var, foreground="#777").pack(side="left", padx=10)

        box = ttk.LabelFrame(root, text="Links for viewers")
        box.pack(fill="x", **pad)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=6)
        self.link_var = tk.StringVar(value="—")
        entry = ttk.Entry(row, textvariable=self.link_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Copy", command=self.copy_link).pack(side="left", padx=6)
        ttk.Button(row, text="Open", command=self.open_link).pack(side="left")

        self.local_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.local_var, foreground="#777").pack(
            anchor="w", padx=8, pady=(0, 8))

        box = ttk.LabelFrame(root, text="Activity")
        box.pack(fill="both", expand=True, **pad)
        wrap = ttk.Frame(box)
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_text = tk.Text(wrap, height=8, wrap="word", state="disabled",
                                background="#1b1b1f", foreground="#d8d8dc",
                                insertbackground="#d8d8dc", relief="flat")
        scroll = ttk.Scrollbar(wrap, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _apply_settings(self):
        s = self.settings
        self.camera_box["values"] = [f"Camera {i}" for i in range(5)]
        self.camera_var.set(f"Camera {s['camera']}")
        self._backend = s.get("backend", "auto")
        self.res_var.set(f"{s['width']}x{s['height']}")
        self.fps_var.set(s["fps"])
        self.quality_var.set(s["quality"])
        self.user_var.set(s["user"])
        self.password_var.set(s.get("password", ""))
        self.tunnel_var.set(s["tunnel"])
        self.port_var.set(s["port"])
        self.autostart_var.set(s["autostart"])
        if not self.password_var.get():
            self._generate_password()
        self.log(f"settings: {settings_store.settings_path()}")


    def log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _toggle_password(self):
        self.password_entry.configure(show="" if self.show_var.get() else "•")

    def _generate_password(self):
        alphabet = string.ascii_letters + string.digits
        self.password_var.set("".join(secrets.choice(alphabet) for _ in range(12)))

    def _selected_camera(self) -> int:
        text = self.camera_var.get()
        digits = "".join(ch for ch in text.split("(")[0] if ch.isdigit())
        return int(digits) if digits else 0

    def _drain_events(self):
        """The only place that touches widgets: worker threads post events here."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "cameras":
                    self._on_cameras_found(payload)
                elif kind == "started":
                    self._on_started(payload)
                elif kind == "failed":
                    self._on_failed(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.scan_btn.configure(state=state)
        if not self.running:
            self.start_btn.configure(state=state)


    def scan_cameras(self):
        if self.running:
            messagebox.showinfo(APP_TITLE, "Stop the stream first - the camera is busy.")
            return
        self._set_busy(True)
        self.status_var.set("looking for cameras...")
        self.log("looking for cameras, this can take a minute...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        found = []
        try:
            import cv2

            for index in range(5):
                for name, flag in ws.backend_list(cv2):
                    cap = cv2.VideoCapture(index, flag)
                    if not cap.isOpened():
                        cap.release()
                        continue
                    frame = ws.grab_first_frame(cap, seconds=2.5)
                    cap.release()
                    if frame is not None:
                        h, w = frame.shape[:2]
                        found.append((index, name, w, h))
                        break
        except Exception as exc:
            self.events.put(("log", f"scan error: {exc}"))
        self.events.put(("cameras", found))

    def _on_cameras_found(self, found):
        self._set_busy(False)
        self.status_var.set("stopped")
        if not found:
            self.camera_box["values"] = [f"Camera {i}" for i in range(5)]
            self.log("no working cameras found")
            messagebox.showwarning(APP_TITLE, ws.CAMERA_HELP.strip())
            return

        labels = [f"Camera {i} ({name}, {w}x{h})" for i, name, w, h in found]
        self.camera_box["values"] = labels
        self.camera_var.set(labels[0])
        self._backend = found[0][1]
        self.log(f"cameras found: {len(found)}")


    def start(self):
        if self.running or self.busy:
            return
        if not self.user_var.get().strip():
            messagebox.showwarning(APP_TITLE, "Enter a login.")
            return
        if len(self.password_var.get()) < 4:
            messagebox.showwarning(APP_TITLE, "The password must be at least 4 characters.")
            return

        self.save_settings()
        self._set_busy(True)
        self.start_btn.configure(state="disabled")
        self.status_var.set("starting...")
        self.link_var.set("—")
        self.local_var.set("")
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        width, _, height = self.res_var.get().partition("x")
        index = self._selected_camera()
        backend = None if self._backend in ("auto", None) else self._backend

        camera = ws.Camera(index, int(width), int(height), self.fps_var.get(),
                           self.quality_var.get(), backend)
        try:
            camera.start()
        except RuntimeError as exc:
            camera.stop()
            self.events.put(("failed", f"{exc}\n{ws.CAMERA_HELP}"))
            return

        ws.StreamHandler.camera = camera
        ws.StreamHandler.username = self.user_var.get().strip()
        ws.StreamHandler.password = self.password_var.get()

        port = int(self.port_var.get())
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), ws.StreamHandler)
        except OSError as exc:
            camera.stop()
            self.events.put(("failed", f"port {port} is not available ({exc}).\nPick another port."))
            return
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

        public = None
        if self.tunnel_var.get():
            exe = ws.ensure_cloudflared()
            if exe:
                self.events.put(("log", "starting the tunnel..."))
                self.tunnel, public = ws.start_tunnel(exe, port)
                if not public:
                    self.events.put(("log", "tunnel did not come up, local network only"))

        self.camera = camera
        self.server = server
        self.events.put(("started", (public, port)))

    def _on_started(self, payload):
        public, port = payload
        self.running = True
        self._set_busy(False)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("live")

        local = f"http://{ws.local_ip()}:{port}"
        self.link_var.set(public or local)
        self.local_var.set(f"Local network: {local}    This computer: http://localhost:{port}")
        self.log(f"streaming: {public or local}")
        self.log(f"login: {self.user_var.get()}    password: {self.password_var.get()}")

    def _on_failed(self, message):
        self._set_busy(False)
        self.start_btn.configure(state="normal")
        self.status_var.set("failed to start")
        self.log(message.splitlines()[0])
        messagebox.showerror(APP_TITLE, message)

    def stop(self):
        if not self.running:
            return
        self.status_var.set("stopping...")
        self.root.update_idletasks()
        self._shutdown()
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("stopped")
        self.link_var.set("—")
        self.local_var.set("")
        self.log("streaming stopped")

    def _shutdown(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        if self.camera:
            self.camera.stop()
            self.camera = None
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=5)
            except Exception:
                self.tunnel.kill()
        self.tunnel = None


    def copy_link(self):
        link = self.link_var.get()
        if link and link != "—":
            self.root.clipboard_clear()
            self.root.clipboard_append(link)
            self.status_var.set("link copied")

    def open_link(self):
        link = self.link_var.get()
        if link and link != "—":
            webbrowser.open(link)

    def save_settings(self):
        width, _, height = self.res_var.get().partition("x")
        values = {
            "camera": self._selected_camera(),
            "backend": self._backend or "auto",
            "width": int(width),
            "height": int(height),
            "fps": int(self.fps_var.get()),
            "quality": int(self.quality_var.get()),
            "port": int(self.port_var.get()),
            "user": self.user_var.get().strip(),
            "password": self.password_var.get(),
            "tunnel": bool(self.tunnel_var.get()),
            "autostart": bool(self.autostart_var.get()),
        }
        try:
            settings_store.save(values, remember_password=self.remember_var.get())
        except OSError as exc:
            self.log(f"could not save settings: {exc}")

    def on_close(self):
        if self.running and not messagebox.askokcancel(
                APP_TITLE, "The stream is live. Stop it and quit?"):
            return
        self.save_settings()
        self._shutdown()
        self.root.destroy()


def main():
    try:
        import cv2  # noqa: F401
    except ImportError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, "OpenCV is missing.\n\nRun:\n\npip install opencv-python")
        return 1

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
