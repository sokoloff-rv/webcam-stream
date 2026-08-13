"""Settings storage and window logic, exercised through a tkinter stub.

Runs headless, no webcam and no display needed.

    python tests/test_gui.py
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tk_stub

tk, ttk, messagebox = tk_stub.install()

import cv2
import numpy as np

import settings_store
import webcam_gui
import webcam_stream as ws

FAILURES = []
PORT = 8098


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")
    if not condition:
        FAILURES.append(name)


def test_settings(work):
    print("\n--- settings ---")

    settings_store.app_dir = lambda: work
    path = os.path.join(work, "settings.json")
    if os.path.exists(path):
        os.remove(path)

    defaults = settings_store.load()
    check("defaults used when no file", defaults["port"] == 8080 and defaults["user"] == "admin")

    values = dict(defaults)
    values.update({"camera": 2, "backend": "dshow", "user": "roman", "password": "Sécret123",
                   "port": 9090, "fps": 12, "tunnel": False, "autostart": True,
                   "width": 640, "height": 480, "quality": 55})
    settings_store.save(values)
    loaded = settings_store.load()
    keys = ("camera", "backend", "user", "port", "fps", "tunnel", "autostart",
            "width", "height", "quality")
    check("settings round-trip", all(loaded[k] == values[k] for k in keys))
    check("password round-trip", loaded["password"] == "Sécret123", repr(loaded["password"]))

    raw = json.load(open(path, encoding="utf-8"))
    if settings_store.IS_WINDOWS:
        check("password encrypted on Windows", raw["password"] == "" and raw["password_enc"])
    else:
        check("password stored plainly off Windows (expected)", raw["password"] == "Sécret123")

    settings_store.save(values, remember_password=False)
    check("'do not remember' mode", settings_store.load()["password"] == "")
    check("login still saved", json.load(open(path, encoding="utf-8"))["user"] == "roman")

    open(path, "w").write("{not json at all")
    check("corrupt file survived", settings_store.load()["port"] == 8080)

    settings_store.save(values)
    check("no leftover temp file", not os.path.exists(path + ".tmp"))


def make_video(path):
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 20, (640, 480))
    for i in range(60):
        writer.write(np.full((480, 640, 3), (i * 4 % 255, 90, 170), dtype=np.uint8))
    writer.release()
    return path


def test_window(video):
    print("\n--- window ---")

    real_camera = ws.Camera

    class FileCamera(real_camera):
        """Stands in for a webcam: reads the generated video file instead."""
        def __init__(self, index, width, height, fps, quality, backend=None):
            super().__init__(video, width, height, fps, quality, "any")

    ws.Camera = FileCamera

    root = tk.Tk()
    real_stdout, real_stderr = sys.stdout, sys.stderr
    try:
        app = webcam_gui.App(root)
    finally:
        gui_pipe = sys.stdout
        sys.stdout, sys.stderr = real_stdout, real_stderr

    check("window builds", app is not None)
    check("enough widgets created", len(tk_stub.Widget.created) > 25,
          f"{len(tk_stub.Widget.created)}")

    gui_pipe.write("library output\n")
    piped = [payload for kind, payload in list(app.events.queue) if kind == "log"]
    check("library prints reach the log pane", "library output" in piped)

    check("saved settings applied", app.user_var.get() == "roman" and app.port_var.get() == 9090)
    check("resolution restored", app.res_var.get() == "640x480", app.res_var.get())

    app._generate_password()
    check("password generator", len(app.password_var.get()) == 12)

    app.camera_var.set("Camera 3 (dshow, 1280x720)")
    check("camera index parsed from label", app._selected_camera() == 3)
    app.camera_var.set("Camera 0")
    check("camera index without label", app._selected_camera() == 0)

    app._on_cameras_found([(1, "msmf", 1280, 720), (2, "dshow", 640, 480)])
    check("scan results fill the dropdown",
          app.camera_box["values"][0].startswith("Camera 1 (msmf"))
    check("backend remembered", app._backend == "msmf")

    before = len(messagebox.calls)
    app._on_cameras_found([])
    check("empty scan warns the user", len(messagebox.calls) > before)

    app.password_var.set("ab")
    app.start()
    check("short password refused", messagebox.calls[-1][0] == "warning" and not app.running)

    print("\n--- streaming from the window ---")
    app.camera_var.set("Camera 0")
    app.user_var.set("roman")
    app.password_var.set("Pass1234")
    app.port_var.set(PORT)
    app.tunnel_var.set(False)
    app.res_var.set("640x480")
    app.fps_var.set(10)
    app.start()

    deadline = time.time() + 45
    while time.time() < deadline and not app.running:
        app._drain_events()
        root.pending = [p for p in root.pending if p[1] != app._drain_events]
        time.sleep(0.1)
    check("stream started", app.running, app.status_var.get())

    if app.running:
        token = base64.b64encode(b"roman:Pass1234").decode()
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/snapshot.jpg")
        req.add_header("Authorization", "Basic " + token)
        data = urllib.request.urlopen(req, timeout=10).read()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        check("frame served over http", img is not None and img.shape == (480, 640, 3))

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5)
            check("password still required", False)
        except urllib.error.HTTPError as exc:
            check("password still required", exc.code == 401)

        check("link shown", app.link_var.get().startswith("http"), app.link_var.get())
        app.copy_link()
        check("copy to clipboard", root.clipboard == app.link_var.get())

        app.stop()
        check("stop works", not app.running and app.status_var.get() == "stopped")

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=3)
            check("port closed after stop", False, "server still answers")
        except urllib.error.HTTPError:
            check("port closed after stop", False, "server still answers")
        except Exception:
            check("port closed after stop", True)

    app.port_var.set(8123)
    app.on_close()
    check("closing saves settings", settings_store.load()["port"] == 8123)
    check("window closed", root.destroyed)

    ws.Camera = real_camera


def main():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_settings(tmp)
        test_window(make_video(os.path.join(tmp, "gui.mp4")))

    print()
    print("RESULT:", "all checks passed" if not FAILURES else f"failed: {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
