"""Service tests against the local fake Meshy server: client, downloads, Unity, watcher."""
import socket
import time

import pytest
import requests

from models.generation import GenerationSettings
from services.download_manager import download_file, download_task_assets
from services.folder_watcher import FolderWatcher
from services.meshy_client import (AmbiguousCreateError, AuthError, InsufficientCreditsError, MeshyClient,
                                   RateLimitError, TransientError, parse_retry_after)
from services.unity_manager import UnityError, install_to_unity, validate_unity_project
from tests.conftest import make_image
from utils.images import to_data_uri


def client_for(srv, key="test-key", **kw):
    return MeshyClient(key, base_url=srv.base_url, min_interval=0, **kw)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ client
def test_auth_and_balance(fake_server):
    assert client_for(fake_server).get_balance()["balance"] == 1000
    with pytest.raises(AuthError):
        client_for(fake_server, key="wrong").get_balance()
    with pytest.raises(AuthError):
        MeshyClient("")


def test_create_and_track(fake_server, tmp_path):
    fake_server.state.retry_after = "7"
    c = client_for(fake_server)
    payload = GenerationSettings().to_payload(to_data_uri(make_image(tmp_path / "p.png")))
    tid = c.create_image_to_3d(payload)
    task, ra = c.get_task(tid)
    assert task["status"] == "IN_PROGRESS" and ra == 7.0  # Retry-After parsed
    c.get_task(tid)
    task, ra = c.get_task(tid)
    assert task["status"] == "SUCCEEDED" and ra is None
    assert set(task["model_urls"]) == {"fbx"} and task["texture_urls"][0]["base_color"]
    assert task["consumed_credits"] == 15
    assert [t["id"] for t in c.list_tasks()] == [tid]


def test_client_error_classification(fake_server):
    c = client_for(fake_server)
    fake_server.state.rate_limit_creates = 1
    with pytest.raises(RateLimitError) as e:
        c.create_image_to_3d({"image_url": "data:image/png;base64,AA"})
    assert e.value.retry_after == 1.0 and not e.value.concurrency_limited
    fake_server.state.create_error = (402, "Insufficient credits")
    with pytest.raises(InsufficientCreditsError):
        c.create_image_to_3d({"image_url": "data:image/png;base64,AA"})
    fake_server.state.create_error = (503, "down")
    with pytest.raises(AmbiguousCreateError):  # 5xx on create: outcome unknown
        c.create_image_to_3d({"image_url": "data:image/png;base64,AA"})
    with pytest.raises(TransientError):  # 5xx would be transient on GET; here use not-found server
        MeshyClient("k", base_url=f"http://127.0.0.1:{free_port()}", min_interval=0).get_balance()


def test_timeout_after_send_is_ambiguous(fake_server):
    fake_server.state.create_delay = 1.5
    c = client_for(fake_server, post_timeout=0.4)
    with pytest.raises(AmbiguousCreateError):
        c.create_image_to_3d({"image_url": "data:image/png;base64,AA"})
    assert fake_server.state.create_count == 1  # the task WAS created server-side


def test_connection_refused_is_not_ambiguous():
    c = MeshyClient("k", base_url=f"http://127.0.0.1:{free_port()}", min_interval=0)
    with pytest.raises(TransientError):
        c.create_image_to_3d({"image_url": "x"})


def test_parse_retry_after():
    assert parse_retry_after("5") == 5
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert 0 <= parse_retry_after(time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))) <= 31


# --------------------------------------------------------------- downloads
def _succeeded_task(fake_server, tmp_path, **settings):
    c = client_for(fake_server)
    g = GenerationSettings(**settings)
    tid = c.create_image_to_3d(g.to_payload(to_data_uri(make_image(tmp_path / "src.png"))))
    for _ in range(fake_server.state.polls_to_finish):
        task, _ = c.get_task(tid)
    assert task["status"] == "SUCCEEDED"
    return c, tid, task


def test_download_resumes_after_interruption(fake_server, tmp_path):
    _, tid, task = _succeeded_task(fake_server, tmp_path)
    fake_server.state.truncate_once.add("model.fbx")
    dest = tmp_path / "out" / "m.fbx"
    with requests.Session() as s:
        size = download_file(s, task["model_urls"]["fbx"], dest, sleep=lambda _: None)
    assert size == fake_server.state.asset_size
    assert dest.read_bytes().startswith(f"model.fbx:{tid}:".encode())
    assert not dest.with_name("m.fbx.part").exists()


def test_download_task_assets_refreshes_expired_urls(fake_server, tmp_path):
    c, tid, task = _succeeded_task(fake_server, tmp_path, enable_pbr=True, export_formats=["fbx", "obj"])
    fake_server.state.expire_urls(tid)  # old signatures now 403
    staging = tmp_path / "Output" / "Panda"
    with requests.Session() as s:
        meta = download_task_assets(task, "Panda", staging, ["fbx", "obj"], s,
                                    refresh_task=lambda: c.get_task(tid)[0], sleep=lambda _: None)
    names = sorted(p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file())
    assert names == ["Panda.fbx", "Panda.mtl", "Panda.obj", "Panda_Preview.png",
                     "Textures/Panda_BaseColor.png", "Textures/Panda_Metallic.png",
                     "Textures/Panda_Normal.png", "Textures/Panda_Roughness.png", "meshy_task.json"]
    assert "map_Kd Textures/Panda_BaseColor.png" in (staging / "Panda.mtl").read_text()
    assert meta["consumed_credits"] == 15
    assert "Signature" not in (staging / "meshy_task.json").read_text()  # no signed URLs stored


# ------------------------------------------------------------------- unity
def make_unity_project(root):
    (root / "Assets").mkdir(parents=True)
    (root / "ProjectSettings").mkdir()
    return root


def make_staging(tmp_path, name="Panda"):
    st = tmp_path / "Output" / name
    (st / "Textures").mkdir(parents=True, exist_ok=True)
    (st / f"{name}.fbx").write_bytes(b"fbx")
    (st / f"{name}_Preview.png").write_bytes(b"png")
    (st / "meshy_task.json").write_text("{}")
    (st / "Textures" / f"{name}_BaseColor.png").write_bytes(b"tex")
    return st


def test_unity_per_model_folder(tmp_path):
    proj = make_unity_project(tmp_path / "Game")
    dest = install_to_unity(make_staging(tmp_path), "Panda", proj)
    assert dest == proj / "Assets" / "Art" / "Models" / "Panda"
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == \
        ["Panda.fbx", "Textures/Panda_BaseColor.png"]  # preview/json stay in Output only
    dest2 = install_to_unity(make_staging(tmp_path), "Panda", proj)  # collision -> new folder
    assert dest2.name == "Panda_2"
    assert not any(p.name.startswith(".") for p in dest.parent.iterdir())  # incoming folder cleaned up


def test_unity_flat_and_validation(tmp_path):
    proj = make_unity_project(tmp_path / "Game")
    root = install_to_unity(make_staging(tmp_path, "Tree"), "Tree", proj, "Assets/Props", per_model_folder=False)
    assert (root / "Tree.fbx").exists() and (root / "Textures" / "Tree_BaseColor.png").exists()
    assert validate_unity_project(tmp_path / "nope")
    assert validate_unity_project(tmp_path)  # no Assets/ProjectSettings
    with pytest.raises(UnityError, match="inside 'Assets'"):
        install_to_unity(make_staging(tmp_path, "X"), "X", proj, "../Outside")


# ----------------------------------------------------------------- watcher
def test_watcher_ignores_existing_and_waits_for_stable_files(tmp_path):
    inbox = tmp_path / "Input"
    make_image(inbox / "old.png")
    seen = []
    w = FolderWatcher(inbox, seen.append, interval=0.05)
    w.start()
    w.stop()  # we drive scans manually below
    make_image(inbox / "new.png")
    (inbox / "notes.txt").write_text("x")
    assert w.scan_once() == []          # first sighting: wait for stability
    assert [p.name for p in w.scan_once()] == ["new.png"]
    assert w.scan_once() == []          # reported only once
    assert [p.name for p in seen] == ["new.png"]


def test_watcher_process_existing(tmp_path):
    inbox = tmp_path / "Input"
    make_image(inbox / "old.jpg", fmt="JPEG")
    seen = []
    w = FolderWatcher(inbox, seen.append, interval=0.05, process_existing=True)
    w.start()
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.05)
    w.stop()
    assert [p.name for p in seen] == ["old.jpg"]
