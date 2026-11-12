"""Unit tests: image validation, settings/payloads/pricing, naming, logging redaction, DB."""
import base64
import logging

import pytest

from database.db import Database
from models.generation import GenerationSettings
from models.job import JobStatus
from tests.conftest import make_image
from utils.images import ImageValidationError, to_data_uri, validate_image
from utils.logging_setup import RedactingFilter, redact, register_secret
from utils.naming import asset_name_from_path, unique_asset_name


# ---------------------------------------------------------------- images
@pytest.mark.parametrize("name,fmt,mime", [("a.png", "PNG", "image/png"), ("b.jpg", "JPEG", "image/jpeg"),
                                           ("c.jpeg", "JPEG", "image/jpeg"), ("d.webp", "WEBP", "image/webp")])
def test_valid_images(tmp_path, name, fmt, mime):
    p = make_image(tmp_path / name, fmt=fmt)
    info = validate_image(p)
    assert info.format == fmt and info.mime == mime
    uri = to_data_uri(p)
    assert uri.startswith(f"data:{mime};base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == p.read_bytes()


def test_mime_from_content_not_extension(tmp_path):
    p = make_image(tmp_path / "x.png", fmt="JPEG")  # JPEG bytes with .png name
    assert to_data_uri(p).startswith("data:image/jpeg;base64,")


def test_image_rejections(tmp_path):
    with pytest.raises(ImageValidationError, match="Unsupported"):
        validate_image(make_image(tmp_path / "a.bmp", fmt="BMP"))
    with pytest.raises(ImageValidationError, match="at least 32"):
        validate_image(make_image(tmp_path / "tiny.png", size=(20, 64)))
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image at all")
    with pytest.raises(ImageValidationError, match="Not a readable"):
        validate_image(bad)
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ImageValidationError, match="empty"):
        validate_image(empty)
    with pytest.raises(ImageValidationError, match="not found"):
        validate_image(tmp_path / "missing.png")


# -------------------------------------------------------------- settings
def test_default_settings_payload():
    g = GenerationSettings()
    assert g.validate() == []
    p = g.to_payload("data:image/png;base64,AAA")
    assert p == {"image_url": "data:image/png;base64,AAA", "model_type": "smart-topology", "ai_model": "meshy-t2.5",
                 "should_texture": True, "target_formats": ["fbx"], "target_polycount": 10000,
                 "texture_resolution": "2k", "enable_pbr": False}
    assert g.estimate_credits() == 15


def test_smart_topology_never_sends_remesh_or_topology():
    g = GenerationSettings(should_remesh=True, topology="quad")
    p = g.to_payload("x")
    assert "should_remesh" not in p and "topology" not in p


def test_polycount_ranges():
    assert GenerationSettings(ai_model="meshy-t2", target_polycount=15000).validate() == []
    assert GenerationSettings(ai_model="meshy-t2", target_polycount=20000).validate()
    assert GenerationSettings(ai_model="meshy-t2.5", target_polycount=30000).validate() == []
    assert GenerationSettings(ai_model="meshy-t2.5", target_polycount=30001).validate()
    assert GenerationSettings(ai_model="meshy-t2.5", target_polycount=50).validate()


def test_standard_model_payload_and_validation():
    g = GenerationSettings(model_type="standard", ai_model="meshy-6", should_remesh=True, topology="quad",
                           target_polycount=50000, should_texture=False, export_formats=["fbx", "glb"])
    assert g.validate() == []
    p = g.to_payload("x")
    assert p["should_remesh"] is True and p["topology"] == "quad" and p["target_polycount"] == 50000
    assert "texture_resolution" not in p and "enable_pbr" not in p
    assert g.estimate_credits() == 20
    g.should_remesh = False
    assert "target_polycount" not in g.to_payload("x")
    assert GenerationSettings(model_type="standard", ai_model="meshy-t2.5").validate()  # mismatch
    lite4k = GenerationSettings(model_type="standard", ai_model="meshy-6-lite", texture_resolution="4k")
    assert any("not supported" in e for e in lite4k.validate())


def test_pricing_table():
    assert GenerationSettings(texture_resolution="8k").estimate_credits() == 20
    assert GenerationSettings(should_texture=False).estimate_credits() == 5
    assert GenerationSettings(model_type="standard", ai_model="latest").estimate_credits() == 30


def test_settings_roundtrip_ignores_unknown():
    g = GenerationSettings.from_dict({"ai_model": "meshy-t2", "bogus": 1, "target_polycount": "4000"})
    assert g.ai_model == "meshy-t2" and g.target_polycount == 4000


# ---------------------------------------------------------------- naming
def test_asset_names(tmp_path):
    assert asset_name_from_path("panda.png") == "Panda"
    assert asset_name_from_path("my tree_01.jpg") == "MyTree01"
    assert asset_name_from_path("3d-rock.png") == "Model3dRock"
    assert asset_name_from_path("con.png") == "ConModel"
    assert asset_name_from_path("日本.png") == "Model"
    (tmp_path / "Panda").mkdir()
    assert unique_asset_name("Panda", [tmp_path]) == "Panda_2"


# --------------------------------------------------------------- logging
def test_redaction(caplog):
    register_secret("supersecretvalue123")
    text = ("key msy_ABCdef123 Authorization: Bearer abc.def supersecretvalue123 "
            "data:image/png;base64,iVBORw0KGgo= https://assets.meshy.ai/t/model.fbx?Expires=1&Signature=zz")
    out = redact(text)
    for leaked in ("msy_ABCdef123", "abc.def", "supersecretvalue123", "iVBORw0KGgo", "Signature=zz"):
        assert leaked not in out
    rec = logging.LogRecord("x", logging.INFO, "", 0, "token %s", ("msy_XYZ987",), None)
    RedactingFilter().filter(rec)
    assert "msy_XYZ987" not in rec.getMessage()


# -------------------------------------------------------------------- db
def test_db_transition_and_duplicates(tmp_path):
    db = Database(tmp_path / "t.db")
    j = db.add_job(name="A", source_path="a.png", image_hash="h1")
    assert db.find_duplicate("h1").id == j
    assert db.transition(j, JobStatus.QUEUED, JobStatus.APPROVED)
    assert not db.transition(j, JobStatus.QUEUED, JobStatus.APPROVED)  # atomic: only once
    db.update_job(j, status=JobStatus.FAILED)
    assert db.find_duplicate("h1") is None  # failed jobs don't block a retry
    db.update_job(j, status=JobStatus.SUBMITTING)
    assert db.recover_after_restart() == 1
    assert db.get_job(j).status == JobStatus.UNCERTAIN
    db.record_task_id("task-1", j)
    assert "task-1" in db.known_task_ids()
    db.close()


def test_batch_committed_credits(tmp_path):
    db = Database(tmp_path / "t.db")
    b = db.create_batch("manual", 40)
    a = db.add_job(name="A", source_path="a", image_hash="1", batch_id=b, est_credits=15, status=JobStatus.PENDING)
    db.update_job(a, task_id="t1")
    c = db.add_job(name="C", source_path="c", image_hash="3", batch_id=b, est_credits=15, status=JobStatus.FAILED)
    db.update_job(c, task_id="t3")  # failed -> refunded -> 0
    db.add_job(name="D", source_path="d", image_hash="4", batch_id=b, est_credits=15, status=JobStatus.APPROVED)
    assert db.batch_committed_credits(b) == 15
    db.update_job(a, consumed_credits=10)
    assert db.batch_committed_credits(b) == 10
    db.close()
