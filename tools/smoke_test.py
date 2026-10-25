"""Smoke-test a built (frozen) MirrorBridge: start it, mirror a test stream into it, check the log.

Usage: python tools/smoke_test.py path/to/MirrorBridge[.exe]
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mirrorbridge.testing.fake_sender import run as run_sender  # noqa: E402

PORT = 7140


def main():
    exe = sys.argv[1]
    home = Path(tempfile.mkdtemp(prefix="mirrorbridge-smoke-"))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", HOME=str(home), APPDATA=str(home),
               XDG_CONFIG_HOME=str(home))
    if sys.platform == "darwin":
        log_file = home / "Library" / "Application Support" / "MirrorBridge" / "mirrorbridge.log"
    elif sys.platform == "win32":
        log_file = home / "MirrorBridge" / "mirrorbridge.log"
    else:
        log_file = home / "mirrorbridge" / "mirrorbridge.log"

    proc = subprocess.Popen([exe, "--port", str(PORT), "--name", "Smoke Test"], env=env)
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if log_file.exists() and "listening on port" in log_file.read_text(errors="replace"):
                break
            if proc.poll() is not None:
                raise SystemExit("app exited early with code %s" % proc.returncode)
            time.sleep(0.5)
        else:
            raise SystemExit("app did not start")
        asyncio.run(run_sender("127.0.0.1", PORT, 4, 640, 360, 30, name="Smoke iPhone"))
        time.sleep(3)
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = log_file.read_text(errors="replace") if log_file.exists() else ""
    print(log)
    required = ["device connected: Smoke iPhone", "mirror video: h264 640x360", "audio stream: aac",
                "device disconnected: Smoke iPhone"]
    missing = [r for r in required if r not in log]
    if missing or "Traceback" in log:
        raise SystemExit("smoke test failed; missing: %s" % missing)
    print("smoke test passed")


if __name__ == "__main__":
    main()
