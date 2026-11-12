"""Resumable downloads of Meshy task assets into a staging folder."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

TEXTURE_KEY_NAMES = {"base_color": "BaseColor", "metallic": "Metallic", "roughness": "Roughness",
                     "normal": "Normal", "emission": "Emission"}


class DownloadError(Exception):
    pass


class UrlExpiredError(DownloadError):
    """The signed asset URL is no longer valid (expired or revoked)."""


def _ext_from_url(url: str, default: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{2,5}", ext or "") else default


def download_file(session: requests.Session, url: str, dest: Path, *, attempts: int = 4,
                  timeout=(15, 120), backoff: float = 2.0, sleep=time.sleep) -> int:
    """Download url to dest via dest.part, resuming with HTTP Range after interruptions."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with session.get(url, headers=headers, stream=True, timeout=timeout) as r:
                if r.status_code in (401, 403, 404, 410):
                    raise UrlExpiredError(f"Download URL rejected (HTTP {r.status_code}) - it has probably expired.")
                if r.status_code == 416 and offset:
                    break  # we already have the whole file
                if r.status_code == 206 and offset:
                    mode = "ab"
                elif r.status_code == 200:
                    mode, offset = "wb", 0
                else:
                    raise DownloadError(f"HTTP {r.status_code}")
                length = r.headers.get("Content-Length")
                expected = offset + int(length) if length and length.isdigit() else None
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
                if expected is not None and part.stat().st_size != expected:
                    raise DownloadError(f"Incomplete download ({part.stat().st_size} of {expected} bytes)")
            break
        except UrlExpiredError:
            raise
        except (requests.RequestException, DownloadError, OSError) as exc:
            if attempt == attempts:
                raise DownloadError(f"Download failed after {attempts} attempts: {exc}") from None
            log.warning("Download interrupted (%s); resuming, attempt %d/%d", type(exc).__name__, attempt + 1,
                        attempts)
            sleep(backoff * attempt)
    os.replace(part, dest)
    return dest.stat().st_size


def plan_files(task: dict, name: str, formats: list[str]) -> list[tuple[str, str]]:
    """List of (url, relative path) to download for a succeeded task."""
    files: list[tuple[str, str]] = []
    model_urls = task.get("model_urls") or {}
    for fmt in formats:
        url = model_urls.get(fmt)
        if url:
            files.append((url, f"{name}.{fmt}"))
            if fmt == "obj" and model_urls.get("mtl"):
                files.append((model_urls["mtl"], f"{name}.mtl"))
    if not files:  # requested formats missing: fall back to anything Unity-usable
        for fmt in ("fbx", "obj", "glb"):
            if model_urls.get(fmt):
                files.append((model_urls[fmt], f"{name}.{fmt}"))
                break
    textures = task.get("texture_urls") or []
    if isinstance(textures, dict):
        textures = [textures]
    for i, tex in enumerate(textures):
        suffix = "" if i == 0 else f"_{i + 1}"
        for key, url in (tex or {}).items():
            if isinstance(url, str) and url:
                label = TEXTURE_KEY_NAMES.get(key, "".join(p.title() for p in key.split("_")))
                files.append((url, f"Textures/{name}{suffix}_{label}{_ext_from_url(url, '.png')}"))
    if task.get("thumbnail_url"):
        files.append((task["thumbnail_url"], f"{name}_Preview{_ext_from_url(task['thumbnail_url'], '.png')}"))
    return files


def _patch_mtl(mtl: Path, name: str, staging: Path) -> None:
    """Point the OBJ material at our renamed base-color texture."""
    base = next(iter(sorted((staging / "Textures").glob(f"{name}_BaseColor.*"))), None)
    if not base or not mtl.exists():
        return
    lines = mtl.read_text(encoding="utf-8", errors="replace").splitlines()
    out = [f"map_Kd Textures/{base.name}" if ln.strip().startswith("map_Kd") else ln for ln in lines]
    mtl.write_text("\n".join(out) + "\n", encoding="utf-8")


def download_task_assets(task: dict, name: str, staging: Path, formats: list[str],
                         session: requests.Session, refresh_task: Callable[[], dict],
                         sleep=time.sleep) -> dict:
    """Download all requested model formats, textures and the preview into staging.

    Already-downloaded files are skipped, so this can be re-run after a crash.
    If a signed URL has expired, the task is re-fetched once for fresh URLs.
    """
    staging.mkdir(parents=True, exist_ok=True)
    plan = plan_files(task, name, formats)
    if not any(rel.rsplit(".", 1)[-1] in ("fbx", "obj", "glb", "usdz", "stl", "3mf") for _, rel in plan):
        raise DownloadError("Meshy returned no model files for this task.")
    urls = {rel: url for url, rel in plan}
    refreshed = False
    done: list[str] = []
    for rel in list(urls):
        dest = staging / rel
        if dest.exists():
            done.append(rel)
            continue
        try:
            download_file(session, urls[rel], dest, sleep=sleep)
        except UrlExpiredError:
            if refreshed:
                raise
            log.info("Asset URL expired; fetching fresh URLs from Meshy")
            task = refresh_task()
            refreshed = True
            fresh = {r: u for u, r in plan_files(task, name, formats)}
            if rel not in fresh:
                raise
            urls.update({r: u for r, u in fresh.items() if r in urls})  # fresh URLs for remaining files
            download_file(session, urls[rel], dest, sleep=sleep)
        done.append(rel)
    if (staging / f"{name}.mtl").exists():
        _patch_mtl(staging / f"{name}.mtl", name, staging)
    meta = {k: task.get(k) for k in ("id", "status", "consumed_credits", "created_at", "started_at",
                                     "finished_at", "expires_at", "texture_prompt")}
    meta["files"] = done
    (staging / "meshy_task.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta
