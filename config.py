"""Application settings (JSON, no secrets) and API key storage (Windows Credential Manager)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from models.generation import GenerationSettings

log = logging.getLogger(__name__)

APP_NAME = "Meshy Automation Studio"
KEYRING_SERVICE = "MeshyAutomationStudio"
KEYRING_USER = "meshy_api_key"
DEFAULT_API_BASE = "https://api.meshy.ai"


def data_dir() -> Path:
    """Where settings.json, the SQLite database and logs live."""
    override = os.environ.get("MESHY_STUDIO_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "MeshyAutomationStudio"


def default_work_dir() -> Path:
    return Path.home() / "Documents" / "MeshyAutomation"


def api_base_url() -> str:
    # Override only for local testing against the fake server (tests/fake_meshy.py).
    return os.environ.get("MESHY_API_BASE", DEFAULT_API_BASE).rstrip("/")


@dataclass
class AppSettings:
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    max_credits_per_batch: int = 100      # 0 = no limit
    concurrency: int = 1
    input_dir: str = str(default_work_dir() / "Input")
    output_dir: str = str(default_work_dir() / "Output")
    unity_project: str = ""
    unity_dest: str = "Assets/Art/Models"
    unity_per_model_folder: bool = True
    archive_processed: bool = True
    process_existing_on_enable: bool = False
    notifications: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        known = {f.name for f in fields(cls)} - {"generation"}
        obj = cls(**{k: v for k, v in data.items() if k in known})
        obj.generation = GenerationSettings.from_dict(data.get("generation"))
        obj.concurrency = max(1, int(obj.concurrency))
        obj.max_credits_per_batch = max(0, int(obj.max_credits_per_batch))
        return obj

    def to_dict(self) -> dict:
        d = asdict(self)
        d["generation"] = self.generation.to_dict()
        return d

    @classmethod
    def load(cls, path: Path) -> "AppSettings":
        if path.exists():
            try:
                return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                log.exception("Could not read settings; using defaults")
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)


class ApiKeyStore:
    """Stores the Meshy API key in the OS keyring (Windows Credential Manager)."""

    def get(self) -> str | None:
        env = os.environ.get("MESHY_API_KEY")
        if env:
            return env.strip()
        try:
            import keyring
            return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:  # noqa: BLE001
            log.exception("Could not read API key from Credential Manager")
            return None

    def set(self, key: str) -> None:
        import keyring
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())

    def delete(self) -> None:
        import keyring
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass
