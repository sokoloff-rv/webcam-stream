#!/usr/bin/env python3
"""
webcam_stream.py - stream a webcam to the internet behind a password.

What it does:
  1. Captures frames from a webcam (OpenCV).
  2. Serves them as MJPEG over a local HTTP server protected by Basic Auth.
  3. Starts a Cloudflare Tunnel and prints a public https link.

Usage:
    pip install opencv-python
    python webcam_stream.py

Handy flags:
    --list-cameras                   show which cameras and backends actually work
    --user roman --password secret   credentials (a random password is generated otherwise)
    --no-tunnel                      local network only, no public link
    --camera 1                       pick another camera
    --backend dshow                  force a capture backend
    --width 1280 --height 720        resolution
    --fps 15 --quality 70            bandwidth knobs
    --port 8080                      local server port
"""

from __future__ import annotations

import argparse
import base64
import os
import platform
import re
import secrets
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

IS_WINDOWS = platform.system() == "Windows"
IS_STORE_PYTHON = "WindowsApps" in sys.executable or "PythonSoftwareFoundation" in sys.executable

CLOUDFLARED_URLS = {
    ("Windows", "AMD64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    ("Windows", "ARM64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-arm64.exe",
    ("Darwin", "arm64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz",
    ("Darwin", "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
    ("Linux", "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    ("Linux", "aarch64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64",
}

CAMERA_HELP = """
Things to check:

  1. Windows Settings -> Privacy & security -> Camera.
     Turn on "Camera access" and "Let desktop apps access your camera".

  2. Close anything that may hold the camera: Zoom, Skype, Teams, Discord,
     OBS, browser tabs with a video call.

  3. If Python came from the Microsoft Store, it runs in a sandbox and often
     cannot reach the camera. Install Python from python.org instead
     (tick "Add Python to PATH") and run this again.

  4. List what is actually available:
         python webcam_stream.py --list-cameras
     then start with the right index, e.g. --camera 1
"""


def backend_list(cv2, only: str | None = None):
    """Capture backends in preference order: (name, OpenCV flag)."""
    if IS_WINDOWS:
        options = [
            ("msmf", cv2.CAP_MSMF),
            ("dshow", cv2.CAP_DSHOW),
            ("any", cv2.CAP_ANY),
        ]
    elif platform.system() == "Darwin":
        options = [("avfoundation", cv2.CAP_AVFOUNDATION), ("any", cv2.CAP_ANY)]
    else:
        options = [("v4l2", cv2.CAP_V4L2), ("any", cv2.CAP_ANY)]

    if only:
        picked = [item for item in options if item[0] == only.lower()]
        if picked:
            return picked
        print(f"[camera] unknown backend '{only}', trying all of them")
    return options


def grab_first_frame(cap, seconds=4.0):
    """Wait for a real frame. A camera can open fine and stay silent - catch that here."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        ok, frame = cap.read()
        if ok and frame is not None and getattr(frame, "size", 0) > 0:
            return frame
        time.sleep(0.05)
    return None


class Camera:
    """Reads the camera on its own thread and keeps one fresh JPEG frame.

    The dedicated thread is what keeps ten viewers from opening ten cameras:
    one capture, one shared frame, every client picks up the latest one.
    """

    def __init__(self, index=0, width=1280, height=720, fps=15, quality=70, backend=None):
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self.backend = backend

        self._jpeg: bytes | None = None
        self._seq = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None


    def start(self, timeout=60.0):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="camera")
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cond:
                if self._jpeg is not None:
                    return True
            if self._error:
                raise RuntimeError(self._error)
            if not self._thread.is_alive():
                raise RuntimeError(self._error or "capture thread died unexpectedly")
            time.sleep(0.1)
        raise RuntimeError(f"no frame from the camera within {int(timeout)} seconds")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def latest(self) -> bytes | None:
        with self._cond:
            return self._jpeg

    def wait_for_next(self, last_seq: int, timeout=5.0):
        """Block until a frame newer than last_seq shows up. Returns (jpeg, seq)."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._jpeg, self._seq


    def _open(self, quiet=False):
        """Try every backend until one delivers an actual frame."""
        import cv2

        for name, flag in backend_list(cv2, self.backend):
            if self._stop.is_set():
                return None, None
            cap = cv2.VideoCapture(self.index, flag)
            if not cap.isOpened():
                cap.release()
                if not quiet:
                    print(f"[camera] {name}: cannot open the camera")
                continue

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if grab_first_frame(cap) is None:
                cap.release()
                if not quiet:
                    print(f"[camera] {name}: camera opened but delivers no frames")
                continue

            if not quiet:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                print(f"[camera] camera #{self.index} works via {name}, {w}x{h}")
            return cap, name

        return None, None

    def _loop(self):
        import cv2

        cap, _ = self._open()
        if cap is None:
            self._error = f"could not get a picture from camera #{self.index}"
            return

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]
        interval = 1.0 / max(self.fps, 1)
        misses = 0

        try:
            while not self._stop.is_set():
                started = time.time()
                ok, frame = cap.read()

                if not ok or frame is None:
                    misses += 1
                    if misses > 30:
                        print("[camera] camera stopped responding, reopening...")
                        cap.release()
                        time.sleep(1.0)
                        cap, _ = self._open(quiet=True)
                        if cap is None:
                            self._error = "camera disconnected and will not reopen"
                            return
                        misses = 0
                    time.sleep(0.05)
                    continue

                misses = 0
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                cv2.putText(frame, stamp, (10, frame.shape[0] - 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(frame, stamp, (10, frame.shape[0] - 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)

                ok, buf = cv2.imencode(".jpg", frame, encode_params)
                if not ok:
                    continue

                with self._cond:
                    self._jpeg = buf.tobytes()
                    self._seq += 1
                    self._cond.notify_all()

                sleep_for = interval - (time.time() - started)
                if sleep_for > 0:
                    time.sleep(sleep_for)
        finally:
            if cap is not None:
                cap.release()
            with self._cond:
                self._cond.notify_all()
            print("[camera] camera released")


def list_cameras(max_index=5):
    """Diagnostics: which camera indexes and backends really produce a picture."""
    import cv2

    print(f"\nPython: {sys.executable}")
    if IS_STORE_PYTHON:
        print("WARNING: this is Microsoft Store Python, it often cannot access the camera.")
    print(f"OpenCV: {cv2.__version__}")
    print("\nProbing cameras, this can take a minute...\n")

    working = []
    for index in range(max_index):
        for name, flag in backend_list(cv2):
            cap = cv2.VideoCapture(index, flag)
            if not cap.isOpened():
                cap.release()
                print(f"  camera {index}, {name:<12} - cannot open")
                continue
            frame = grab_first_frame(cap, seconds=3.0)
            if frame is None:
                print(f"  camera {index}, {name:<12} - opens, but no frames")
            else:
                h, w = frame.shape[:2]
                print(f"  camera {index}, {name:<12} - WORKS, {w}x{h}")
                working.append((index, name))
            cap.release()

    print()
    if working:
        index, name = working[0]
        print("Working combinations:", ", ".join(f"--camera {i} --backend {b}" for i, b in working))
        print(f"\nStart it like this:\n    python webcam_stream.py --camera {index} --backend {name}")
    else:
        print("No camera produced an image.")
        print(CAMERA_HELP)
    return 0 if working else 1


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Camera</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: #101014; color: #e8e8ea;
    font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 14px; padding: 16px;
  }
  #wrap { position: relative; max-width: 100%; }
  img {
    display: block; max-width: 100%; max-height: 82vh; border-radius: 10px;
    background: #000; box-shadow: 0 8px 40px rgba(0,0,0,.6);
  }
  #bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: center; }
  button {
    background: #26262e; color: #e8e8ea; border: 1px solid #3a3a44;
    padding: 8px 16px; border-radius: 8px; font-size: 14px; cursor: pointer;
  }
  button:hover { background: #32323c; }
  #status { font-size: 13px; color: #8b8b96; min-width: 90px; }
  #status.live::before { content: "\\25CF "; color: #34c759; }
  #status.down::before { content: "\\25CF "; color: #ff453a; }
</style>
</head>
<body>
  <div id="wrap"><img id="cam" src="/stream.mjpg" alt="Camera feed"></div>
  <div id="bar">
    <span id="status" class="live">Live</span>
    <button onclick="document.getElementById('wrap').requestFullscreen()">Fullscreen</button>
    <button onclick="reconnect()">Refresh</button>
  </div>
<script>
  var img = document.getElementById('cam');
  var status = document.getElementById('status');

  function reconnect() {
    status.className = 'down';
    status.textContent = 'Reconnecting';
    img.src = '/stream.mjpg?r=' + Date.now();
  }

  img.onload = function () {
    status.className = 'live';
    status.textContent = 'Live';
  };

  img.onerror = function () {
    status.className = 'down';
    status.textContent = 'Offline';
    setTimeout(reconnect, 3000);
  };

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) reconnect();
  });
</script>
</body>
</html>
"""


class StreamHandler(BaseHTTPRequestHandler):
    server_version = "cam/1.0"
    protocol_version = "HTTP/1.1"

    camera: Camera = None
    username: str = ""
    password: str = ""


    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        user, sep, pwd = decoded.partition(":")
        if not sep:
            return False
        user_ok = secrets.compare_digest(user, self.username)
        pass_ok = secrets.compare_digest(pwd, self.password)
        return user_ok and pass_ok

    def _demand_auth(self):
        time.sleep(1)
        body = b"401 Unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Camera", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self._authorized():
            self._demand_auth()
            return

        if path == "/":
            self._send_page()
        elif path == "/stream.mjpg":
            self._send_stream()
        elif path == "/snapshot.jpg":
            self._send_snapshot()
        else:
            self.send_error(404, "Not Found")

    def do_HEAD(self):
        if not self._authorized():
            self._demand_auth()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_page(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_snapshot(self):
        jpeg = self.camera.latest()
        if jpeg is None:
            self.send_error(503, "Camera not ready")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpeg)

    def _send_stream(self):
        boundary = "frameboundary"
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.end_headers()

        peer = self.client_address[0]
        print(f"[http]   viewer connected: {peer}")
        seq = -1
        try:
            while True:
                jpeg, seq = self.camera.wait_for_next(seq, timeout=5.0)
                if jpeg is None:
                    continue
                self.wfile.write(
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError:
            pass
        finally:
            print(f"[http]   viewer disconnected: {peer}")

    def log_message(self, fmt, *args):
        pass


def app_dir() -> str:
    """Program folder. In a frozen exe this is next to the exe, not the temp unpack dir."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_cloudflared() -> str | None:
    """Path to cloudflared, downloading it next to the program if needed."""
    from shutil import which

    found = which("cloudflared")
    if found:
        return found

    local = os.path.join(app_dir(), "cloudflared.exe" if IS_WINDOWS else "cloudflared")
    if os.path.exists(local):
        return local

    key = (platform.system(), platform.machine())
    url = CLOUDFLARED_URLS.get(key)
    if url is None:
        print(f"[tunnel] no prebuilt cloudflared for {key}.")
        print("[tunnel] grab it manually: https://github.com/cloudflare/cloudflared/releases")
        return None

    if url.endswith(".tgz"):
        print("[tunnel] on macOS install cloudflared via Homebrew: brew install cloudflared")
        return None

    print("[tunnel] downloading cloudflared (~50 MB, once)...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "webcam-stream"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(local, "wb") as out:
            while chunk := resp.read(262144):
                out.write(chunk)
        if not IS_WINDOWS:
            os.chmod(local, 0o755)
        print(f"[tunnel] saved: {local}")
        return local
    except Exception as exc:
        print(f"[tunnel] could not download cloudflared: {exc}")
        if os.path.exists(local):
            os.remove(local)
        return None


def start_tunnel(exe: str, port: int):
    """Starts a quick tunnel, returns (process, public_url)."""
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
    )

    public_url: list[str] = []
    pattern = re.compile(r"https://[-\w]+\.trycloudflare\.com")

    def reader():
        for line in proc.stdout:
            match = pattern.search(line)
            if match and not public_url:
                public_url.append(match.group(0))
            if "ERR" in line and "context canceled" not in line:
                print(f"[tunnel] {line.rstrip()}")

    threading.Thread(target=reader, daemon=True, name="tunnel-log").start()

    deadline = time.time() + 45
    while time.time() < deadline:
        if public_url:
            return proc, public_url[0]
        if proc.poll() is not None:
            return proc, None
        time.sleep(0.3)
    return proc, None


def local_ip() -> str:
    """This machine's LAN address (no traffic actually leaves the box)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def make_password(length=14) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse_args():
    p = argparse.ArgumentParser(
        description="Stream a webcam to the internet behind a password",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--user", default=os.environ.get("CAM_USER", "admin"), help="login")
    p.add_argument("--password", default=os.environ.get("CAM_PASSWORD"),
                   help="password (random one is generated by default)")
    p.add_argument("--port", type=int, default=8080, help="local server port")
    p.add_argument("--camera", type=int, default=0, help="camera index")
    p.add_argument("--backend", default=None,
                   help="capture backend: msmf, dshow, any (all are tried by default)")
    p.add_argument("--width", type=int, default=1280, help="frame width")
    p.add_argument("--height", type=int, default=720, help="frame height")
    p.add_argument("--fps", type=int, default=15, help="frames per second")
    p.add_argument("--quality", type=int, default=70, help="JPEG quality, 1-100")
    p.add_argument("--no-tunnel", action="store_true",
                   help="local network only, no public link")
    p.add_argument("--list-cameras", action="store_true",
                   help="list available cameras and exit")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        import cv2  # noqa: F401
    except ImportError:
        print("OpenCV is missing. Run:\n\n    pip install opencv-python\n")
        return 1

    if args.list_cameras:
        return list_cameras()

    password = args.password or make_password()
    generated = args.password is None

    camera = Camera(args.camera, args.width, args.height, args.fps, args.quality, args.backend)
    print("[camera] opening the camera...")
    try:
        camera.start()
    except RuntimeError as exc:
        camera.stop()
        print(f"\n[camera] error: {exc}")
        print(CAMERA_HELP)
        return 1

    StreamHandler.camera = camera
    StreamHandler.username = args.user
    StreamHandler.password = password

    try:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), StreamHandler)
    except OSError as exc:
        camera.stop()
        print(f"[http]   port {args.port} is not available: {exc}. Try --port 8081")
        return 1
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    print(f"[http]   serving on port {args.port}")

    tunnel_proc = None
    public_url = None
    if not args.no_tunnel:
        exe = ensure_cloudflared()
        if exe:
            print("[tunnel] starting the tunnel...")
            tunnel_proc, public_url = start_tunnel(exe, args.port)
            if not public_url:
                print("[tunnel] tunnel did not come up - local network only")

    line = "=" * 62
    print(f"\n{line}")
    if public_url:
        print(f"  Public link    : {public_url}")
    print(f"  Local network  : http://{local_ip()}:{args.port}")
    print(f"  This computer  : http://localhost:{args.port}")
    print(f"\n  Login    : {args.user}")
    print(f"  Password : {password}")
    if generated:
        print("             (generated; set your own with --password)")
    print(f"\n  Stop with Ctrl+C")
    print(f"{line}\n")

    try:
        while True:
            time.sleep(1)
            if tunnel_proc and tunnel_proc.poll() is not None:
                print("[tunnel] tunnel died, the public link no longer works")
                tunnel_proc = None
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        server.shutdown()
        camera.stop()
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()
        print("done, the camera is free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
