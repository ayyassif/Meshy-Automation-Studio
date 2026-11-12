"""Offscreen GUI smoke test: drop images -> confirm -> generated -> listed; settings round-trip."""
import time

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from models.job import JobStatus
from tests.conftest import make_image
from tests.test_services import make_unity_project


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_gui_end_to_end(qapp, tmp_path, fake_server, monkeypatch):
    monkeypatch.setenv("MESHY_API_BASE", fake_server.base_url)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

    from app import AppContext
    from ui.main_window import MainWindow

    ctx = AppContext(home=tmp_path / "home")
    ctx.settings.input_dir = str(tmp_path / "Input")
    ctx.settings.output_dir = str(tmp_path / "Output")
    ctx.settings.unity_project = str(make_unity_project(tmp_path / "Game"))
    ctx.qm.tick, ctx.qm.min_poll_interval = 0.05, 0.05
    ctx.start()
    win = MainWindow(ctx)
    win.show()
    try:
        from ui.widgets import expand_paths
        (tmp_path / "imgs").mkdir()
        (tmp_path / "imgs" / "notes.txt").write_text("x")
        make_image(tmp_path / "imgs" / "panda.png")
        paths = expand_paths([tmp_path / "imgs"])  # dropping a folder keeps only supported images
        assert [p.name for p in paths] == ["panda.png"]
        win.dashboard.add_files(paths)
        assert asked and "Estimated cost: 15 credits" in asked[0]
        deadline = time.time() + 20
        while time.time() < deadline:
            qapp.processEvents()
            jobs = ctx.db.list_jobs()
            if jobs and jobs[0].status == JobStatus.COMPLETED:
                break
            time.sleep(0.05)
        for _ in range(30):
            qapp.processEvents()
            time.sleep(0.03)
        win._tick()
        assert ctx.db.list_jobs()[0].status == JobStatus.COMPLETED
        t = win.dashboard.table
        assert t.rowCount() == 1 and t.item(0, 1).text() == "Panda" and t.item(0, 2).text() == "Completed"
        assert t.item(0, 5).text() == "15"
        assert win.dashboard.card_completed.value.text() == "1"
        assert (tmp_path / "Game/Assets/Art/Models/Panda/Panda.fbx").exists()

        # Settings page round-trip
        sp = win.settings_page
        sp._select(sp.model_type, "standard")
        sp._select(sp.ai_model, "meshy-6")
        sp.polycount.setValue(50000)
        sp.save()
        assert ctx.settings.generation.model_type == "standard"
        assert ctx.settings.generation.target_polycount == 50000
        assert (ctx.home / "settings.json").exists()
        assert "test-key" not in (ctx.home / "settings.json").read_text()
    finally:
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
        win.close()
    assert "test-key" not in (ctx.home / "logs" / "meshy_studio.log").read_text()
