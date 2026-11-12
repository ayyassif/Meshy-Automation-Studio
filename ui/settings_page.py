"""Settings page: Meshy API key + generation parameters, credits, folders, Unity."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from config import api_base_url
from models.generation import (AI_MODEL_LABELS, AI_MODELS, EXPORT_FORMATS, PRICING_SOURCE, GenerationSettings)
from services.meshy_client import MeshyClient, MeshyError
from services.unity_manager import UnityError, resolve_destination, validate_unity_project


class _TestSignals(QObject):
    done = Signal(bool, str)


class SettingsPage(QWidget):
    saved = Signal()
    key_changed = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 12, 16, 16)

        # ---------------------------------------------------------- API key
        g = QGroupBox("Meshy API key")
        f = QFormLayout(g)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        row = QHBoxLayout()
        row.addWidget(self.key_edit, 1)
        for text, slot in (("Save key", self.save_key), ("Test connection", self.test_key),
                           ("Remove key", self.delete_key)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        f.addRow("API key", row)
        self.key_status = QLabel()
        self.key_status.setObjectName("hint")
        self.key_status.setWordWrap(True)
        f.addRow("", self.key_status)
        lay.addWidget(g)
        self._test_signals = _TestSignals()
        self._test_signals.done.connect(self._test_done)

        # ------------------------------------------------------- generation
        g = QGroupBox("Generation (Meshy Image-to-3D)")
        f = QFormLayout(g)
        self.model_type = QComboBox()
        self.model_type.addItem("Smart Topology - clean game-ready triangle mesh", "smart-topology")
        self.model_type.addItem("Standard - high-detail mesh", "standard")
        self.ai_model = QComboBox()
        self.polycount = QSpinBox()
        self.polycount.setSingleStep(500)
        self.poly_hint = QLabel()
        self.poly_hint.setObjectName("hint")
        poly_row = QHBoxLayout()
        poly_row.addWidget(self.polycount)
        poly_row.addWidget(self.poly_hint, 1)
        self.remesh = QCheckBox("Remesh to target polycount")
        self.topology = QComboBox()
        self.topology.addItem("Triangle", "triangle")
        self.topology.addItem("Quad-dominant", "quad")
        self.texture = QCheckBox("Generate textures")
        self.pbr = QCheckBox("PBR maps (metallic, roughness, normal)")
        self.tex_res = QComboBox()
        for r in ("2k", "4k", "8k"):
            self.tex_res.addItem(r.upper(), r)
        self.tex_prompt = QPlainTextEdit()
        self.tex_prompt.setPlaceholderText("Optional texture prompt, e.g. 'stylized hand-painted, soft colors'")
        self.tex_prompt.setFixedHeight(60)
        fmt_row = QHBoxLayout()
        self.formats: dict[str, QCheckBox] = {}
        for fmt in EXPORT_FORMATS:
            cb = QCheckBox(fmt.upper())
            self.formats[fmt] = cb
            fmt_row.addWidget(cb)
        fmt_row.addStretch()
        self.pose = QComboBox()
        self.pose.addItem("None", "")
        self.pose.addItem("A-pose", "a-pose")
        self.pose.addItem("T-pose", "t-pose")
        self.moderation = QCheckBox("Content moderation screening")
        self.auto_size = QCheckBox("Auto-size to real-world height")
        self.origin = QComboBox()
        self.origin.addItem("Bottom", "bottom")
        self.origin.addItem("Center", "center")
        self.estimate = QLabel()
        self.estimate.setStyleSheet("font-weight: 600;")
        self.estimate.setWordWrap(True)
        f.addRow("Model type", self.model_type)
        f.addRow("AI model", self.ai_model)
        f.addRow("", self.remesh)
        f.addRow("Topology", self.topology)
        f.addRow("Target polycount", poly_row)
        f.addRow("", self.texture)
        f.addRow("Texture resolution", self.tex_res)
        f.addRow("", self.pbr)
        f.addRow("Texture prompt", self.tex_prompt)
        f.addRow("Export formats", fmt_row)
        f.addRow("Pose", self.pose)
        f.addRow("", self.moderation)
        f.addRow("", self.auto_size)
        f.addRow("Origin", self.origin)
        f.addRow("Estimated cost", self.estimate)
        self.gen_form = f
        lay.addWidget(g)

        # ---------------------------------------------------------- credits
        g = QGroupBox("Credits and queue")
        f = QFormLayout(g)
        self.budget = QSpinBox()
        self.budget.setRange(0, 1_000_000)
        self.budget.setSuffix(" credits")
        self.budget.setSpecialValueText("No limit")
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 10)
        self.notify = QCheckBox("Windows notifications when jobs finish or fail")
        f.addRow("Max credits per batch", self.budget)
        f.addRow("Simultaneous generations", self.concurrency)
        f.addRow("", self.notify)
        lay.addWidget(g)

        # ---------------------------------------------------------- folders
        g = QGroupBox("Folders")
        f = QFormLayout(g)
        self.input_dir = QLineEdit()
        self.output_dir = QLineEdit()
        f.addRow("Input (watched)", self._with_browse(self.input_dir))
        f.addRow("Output (all downloads)", self._with_browse(self.output_dir))
        self.archive = QCheckBox("Move processed images to Input/Processed (failed -> Input/Failed)")
        f.addRow("", self.archive)
        lay.addWidget(g)

        # ------------------------------------------------------------ unity
        g = QGroupBox("Unity")
        f = QFormLayout(g)
        self.unity_project = QLineEdit()
        self.unity_project.setPlaceholderText("Leave empty to only download to the Output folder")
        self.unity_dest = QLineEdit()
        self.per_model = QCheckBox("Put each model in its own folder (Models/Panda/Panda.fbx + Textures/)")
        self.unity_status = QLabel()
        self.unity_status.setObjectName("hint")
        f.addRow("Unity project", self._with_browse(self.unity_project))
        f.addRow("Destination in project", self.unity_dest)
        f.addRow("", self.per_model)
        f.addRow("", self.unity_status)
        lay.addWidget(g)

        btns = QHBoxLayout()
        btns.addStretch()
        reset = QPushButton("Restore recommended generation defaults")
        reset.clicked.connect(self.reset_generation)
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        btns.addWidget(reset)
        btns.addWidget(save)
        lay.addLayout(btns)
        lay.addStretch()

        # dynamic behaviour
        self.model_type.currentIndexChanged.connect(self._model_type_changed)
        for w in (self.ai_model, self.tex_res, self.topology):
            w.currentIndexChanged.connect(self._update_dynamic)
        for w in (self.remesh, self.texture, self.auto_size, self.pbr):
            w.toggled.connect(self._update_dynamic)
        self.unity_project.textChanged.connect(self._update_unity_status)
        self.unity_dest.textChanged.connect(self._update_unity_status)
        self.load()

    # ================================================================ helpers
    def _with_browse(self, edit: QLineEdit) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(lambda: self._pick_dir(edit))
        h.addWidget(b)
        return w

    def _pick_dir(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select folder", edit.text())
        if d:
            edit.setText(d)

    @staticmethod
    def _select(combo: QComboBox, value):
        i = combo.findData(value)
        combo.setCurrentIndex(max(0, i))

    def _fill_ai_models(self, keep: str | None = None):
        mt = self.model_type.currentData()
        self.ai_model.blockSignals(True)
        self.ai_model.clear()
        for m in AI_MODELS[mt]:
            self.ai_model.addItem(AI_MODEL_LABELS.get(m, m), m)
        self._select(self.ai_model, keep)
        self.ai_model.blockSignals(False)

    def _model_type_changed(self):
        self._fill_ai_models()
        self._update_dynamic()

    def _update_dynamic(self):
        g = self._collect_generation()
        standard = g.model_type == "standard"
        # Only show options that apply to the selected model (smart-topology ignores remesh/topology).
        self.gen_form.setRowVisible(self.remesh, standard)
        self.gen_form.setRowVisible(self.topology, standard and g.should_remesh)
        self.gen_form.setRowVisible(self.origin, g.auto_size)
        rng = g.polycount_range()
        self.polycount.setEnabled(rng is not None)
        if rng:
            self.polycount.setRange(*rng)
            self.poly_hint.setText(f"{rng[0]:,} - {rng[1]:,}")
        else:
            self.poly_hint.setText("not used without remeshing")
        self.tex_res.setEnabled(g.should_texture)
        self.pbr.setEnabled(g.should_texture)
        self.tex_prompt.setEnabled(g.should_texture)
        self.origin.setEnabled(g.auto_size)
        errors = g.validate()
        est = g.estimate_credits()
        if errors:
            self.estimate.setText("Invalid: " + " ".join(errors))
            self.estimate.setStyleSheet("font-weight: 600; color: #ff8da1;")
        else:
            self.estimate.setText(f"~{est} credits per model" if est is not None else "Unknown")
            self.estimate.setStyleSheet("font-weight: 600; color: #3ecf8e;")
        self.estimate.setToolTip(PRICING_SOURCE)

    def _update_unity_status(self):
        proj = self.unity_project.text().strip()
        if not proj:
            self.unity_status.setText("Unity copy disabled; models stay in the Output folder.")
            return
        err = validate_unity_project(proj)
        if not err:
            try:
                dest = resolve_destination(proj, self.unity_dest.text().strip() or "Assets/Art/Models")
                self.unity_status.setText(f"OK - models go to {dest}")
                return
            except UnityError as exc:
                err = str(exc)
        self.unity_status.setText("Warning: " + err)

    def _collect_generation(self) -> GenerationSettings:
        return GenerationSettings(
            model_type=self.model_type.currentData(), ai_model=self.ai_model.currentData() or "",
            target_polycount=self.polycount.value(), should_texture=self.texture.isChecked(),
            enable_pbr=self.pbr.isChecked(), texture_resolution=self.tex_res.currentData(),
            texture_prompt=self.tex_prompt.toPlainText().strip(),
            export_formats=[f for f, cb in self.formats.items() if cb.isChecked()],
            should_remesh=self.remesh.isChecked(), topology=self.topology.currentData(),
            pose_mode=self.pose.currentData(), moderation=self.moderation.isChecked(),
            auto_size=self.auto_size.isChecked(), origin_at=self.origin.currentData())

    # ============================================================== load/save
    def _load_generation(self, g: GenerationSettings):
        self._select(self.model_type, g.model_type)
        self._fill_ai_models(g.ai_model)
        rng = g.polycount_range() or (100, 300_000)
        self.polycount.setRange(*rng)
        self.polycount.setValue(g.target_polycount)
        self.remesh.setChecked(g.should_remesh)
        self._select(self.topology, g.topology)
        self.texture.setChecked(g.should_texture)
        self.pbr.setChecked(g.enable_pbr)
        self._select(self.tex_res, g.texture_resolution)
        self.tex_prompt.setPlainText(g.texture_prompt)
        for fmt, cb in self.formats.items():
            cb.setChecked(fmt in g.export_formats)
        self._select(self.pose, g.pose_mode)
        self.moderation.setChecked(g.moderation)
        self.auto_size.setChecked(g.auto_size)
        self._select(self.origin, g.origin_at)
        self._update_dynamic()

    def load(self):
        s = self.ctx.settings
        self._load_generation(s.generation)
        self.budget.setValue(s.max_credits_per_batch)
        self.concurrency.setValue(s.concurrency)
        self.notify.setChecked(s.notifications)
        self.input_dir.setText(s.input_dir)
        self.output_dir.setText(s.output_dir)
        self.archive.setChecked(s.archive_processed)
        self.unity_project.setText(s.unity_project)
        self.unity_dest.setText(s.unity_dest)
        self.per_model.setChecked(s.unity_per_model_folder)
        self._update_unity_status()
        self._refresh_key_status()

    def reset_generation(self):
        self._load_generation(GenerationSettings())

    def save(self):
        g = self._collect_generation()
        errors = g.validate()
        if errors:
            QMessageBox.warning(self, "Invalid generation settings", "\n".join(errors))
            return
        s = self.ctx.settings
        s.generation = g
        s.max_credits_per_batch = self.budget.value()
        s.concurrency = self.concurrency.value()
        s.notifications = self.notify.isChecked()
        s.input_dir = self.input_dir.text().strip() or s.input_dir
        s.output_dir = self.output_dir.text().strip() or s.output_dir
        s.archive_processed = self.archive.isChecked()
        s.unity_project = self.unity_project.text().strip()
        s.unity_dest = self.unity_dest.text().strip() or "Assets/Art/Models"
        s.unity_per_model_folder = self.per_model.isChecked()
        self.ctx.save_settings()
        self.saved.emit()
        msg = "Settings saved. Jobs already approved keep the settings they were approved with."
        if s.unity_project and validate_unity_project(s.unity_project):
            msg += f"\n\nWarning: {validate_unity_project(s.unity_project)}"
        QMessageBox.information(self, "Saved", msg)

    # ================================================================ API key
    def _refresh_key_status(self):
        if self.ctx.api_key:
            self.key_edit.setPlaceholderText("Stored securely in Windows Credential Manager")
            self.key_status.setText("A key is stored in Windows Credential Manager. It is never written to "
                                    "logs, the database or settings files.")
        else:
            self.key_edit.setPlaceholderText("Paste your Meshy API key (msy_...)")
            self.key_status.setText("No key stored. Create one at meshy.ai -> API (paid plan required).")
        if api_base_url() != "https://api.meshy.ai":
            self.key_status.setText(self.key_status.text() + f"\nTEST MODE: using {api_base_url()}")

    def save_key(self):
        key = self.key_edit.text().strip()
        if not key:
            return
        try:
            self.ctx.set_api_key(key)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Could not store key", f"Windows Credential Manager error: {exc}")
            return
        self.key_edit.clear()
        self._refresh_key_status()
        self.key_changed.emit()
        self.test_key()

    def delete_key(self):
        if QMessageBox.question(self, "Remove key", "Remove the stored Meshy API key?") \
                == QMessageBox.StandardButton.Yes:
            self.ctx.delete_api_key()
            self._refresh_key_status()
            self.key_changed.emit()

    def test_key(self):
        key = self.key_edit.text().strip() or self.ctx.api_key
        if not key:
            self.key_status.setText("Enter a key first.")
            return
        self.key_status.setText("Testing (free balance check, no credits used)...")

        def run():
            try:
                bal = MeshyClient(key, base_url=api_base_url()).get_balance()
                self._test_signals.done.emit(True, f"Connected. Balance: {bal.get('balance', 0):,} credits.")
            except MeshyError as exc:
                self._test_signals.done.emit(False, exc.message)
            except Exception as exc:  # noqa: BLE001
                self._test_signals.done.emit(False, str(exc))

        threading.Thread(target=run, daemon=True).start()

    def _test_done(self, ok: bool, msg: str):
        self.key_status.setText(("OK: " if ok else "Failed: ") + msg)
        if ok:
            self.ctx.qm.request_balance_refresh()
