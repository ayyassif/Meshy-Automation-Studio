import os
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.fake_meshy import FakeMeshyServer  # noqa: E402


@pytest.fixture
def fake_server():
    srv = FakeMeshyServer().start()
    yield srv
    srv.stop()


def make_image(path: Path, size=(64, 64), color=(200, 80, 40), fmt=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


def wait_for(cond, timeout=15.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError("Timed out waiting for condition")
