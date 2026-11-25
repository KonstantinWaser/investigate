"""Holds the tray's single-instance lock until stdin closes, like a running tray would."""

import sys
from pathlib import Path

from investigate.instance import InstanceLock

lock = InstanceLock(Path(sys.argv[1]))
if not lock.acquire():
    print("busy", flush=True)
    sys.exit(3)
lock.publish("http://127.0.0.1:9/")
print("held", flush=True)
sys.stdin.read()
lock.release()
