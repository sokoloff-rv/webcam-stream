"""Capture loop and HTTP layer: auth, MJPEG, backend fallback, recovery.

No webcam required - a generated video file stands in for the camera.

    python tests/test_server.py
"""

import base64
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

import webcam_stream as ws

FAILURES = []
PORT = 8097


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")
    if not condition:
        FAILURES.append(name)


def make_video(path, frames=60, size=(640, 480)):
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 20, size)
    for i in range(frames):
        writer.write(np.full((size[1], size[0], 3), (i * 4 % 255, 90, 170), dtype=np.uint8))
    writer.release()
    return path


def request(path, user=None, pwd=None, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if user is not None:
        token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    return urllib.request.urlopen(req, timeout=timeout)


def test_capture(video):
    print("\n--- capture ---")

    camera = ws.Camera(index=video, width=640, height=480, fps=10)
    started = camera.start()
    check("camera starts", started is True)

    jpeg = camera.latest()
    check("jpeg produced", jpeg is not None and jpeg.startswith(b"\xff\xd8"))

    _, seq1 = camera.wait_for_next(-1, timeout=5)
    _, seq2 = camera.wait_for_next(seq1, timeout=5)
    check("frames keep coming", seq2 > seq1, f"{seq1} -> {seq2}")

    frame = cv2.imdecode(np.frombuffer(camera.latest(), np.uint8), cv2.IMREAD_COLOR)
    check("frame decodes back", frame is not None and frame.shape == (480, 640, 3),
          str(None if frame is None else frame.shape))

    strip = frame[440:475, 0:260]
    check("timestamp is drawn", int((strip.max(axis=2) > 200).sum()) > 40)

    camera.stop()
    return camera


def test_backends(video):
    print("\n--- backend selection ---")

    camera = ws.Camera(index=video, fps=10, backend="nonsense")
    camera.start()
    check("unknown backend falls back to probing", camera.latest() is not None)
    camera.stop()

    dead = ws.Camera(index=77)
    started = time.time()
    try:
        dead.start(timeout=30)
        check("dead camera raises", False, "no exception")
    except RuntimeError as exc:
        check("dead camera raises quickly", time.time() - started < 25, f"{exc}")
    dead.stop()


def test_http(video):
    print("\n--- http ---")

    camera = ws.Camera(index=video, width=640, height=480, fps=10)
    camera.start()

    ws.StreamHandler.camera = camera
    ws.StreamHandler.username = "roman"
    ws.StreamHandler.password = "s3cr3t"

    server = ThreadingHTTPServer(("127.0.0.1", PORT), ws.StreamHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)

    try:
        try:
            request("/")
            check("no credentials rejected", False, "got 200")
        except urllib.error.HTTPError as exc:
            check("no credentials rejected", exc.code == 401, f"code {exc.code}")
            check("browser is asked for a password",
                  "Basic" in (exc.headers.get("WWW-Authenticate") or ""))

        for user, pwd, label in [("roman", "wrong", "wrong password"),
                                 ("hacker", "s3cr3t", "wrong login")]:
            try:
                request("/", user, pwd)
                check(f"{label} rejected", False, "got 200")
            except urllib.error.HTTPError as exc:
                check(f"{label} rejected", exc.code == 401, f"code {exc.code}")

        started = time.time()
        try:
            request("/", "roman", "nope")
        except urllib.error.HTTPError:
            pass
        check("brute force is slowed down", time.time() - started >= 0.9,
              f"{time.time() - started:.2f}s")

        resp = request("/", "roman", "s3cr3t")
        html = resp.read().decode()
        check("viewer page served", resp.status == 200 and "stream.mjpg" in html,
              f"{len(html)} bytes")

        resp = request("/snapshot.jpg", "roman", "s3cr3t")
        data = resp.read()
        check("snapshot served", resp.headers.get("Content-Type") == "image/jpeg"
              and data.startswith(b"\xff\xd8"))

        resp = request("/stream.mjpg", "roman", "s3cr3t")
        check("stream content type",
              "multipart/x-mixed-replace" in resp.headers.get("Content-Type", ""))
        chunk = resp.read(60000)
        check("stream delivers consecutive frames", chunk.count(b"--frameboundary") >= 2,
              f"{chunk.count(b'--frameboundary')} boundaries in 60 KB")
        resp.close()

        try:
            request("/../secret", "roman", "s3cr3t")
            check("unknown paths are 404", False)
        except urllib.error.HTTPError as exc:
            check("unknown paths are 404", exc.code == 404, f"code {exc.code}")
    finally:
        server.shutdown()
        server.server_close()
        camera.stop()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        video = make_video(os.path.join(tmp, "fake.mp4"))
        test_capture(video)
        test_backends(video)
        test_http(video)

    print()
    print("RESULT:", "all checks passed" if not FAILURES else f"failed: {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
