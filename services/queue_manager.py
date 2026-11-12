"""Job queue engine.

One engine thread submits approved jobs, polls Meshy tasks, and reconciles
uncertain submissions. One download thread fetches finished assets.

Credit-safety rules enforced here:
* Jobs are only submitted after explicit approval (manual confirmation, or the
  user enabling auto-submit for the watched folder).
* Every submission checks the batch budget and the live credit balance first.
* A job moves APPROVED -> SUBMITTING atomically, so it can never be sent twice.
* If a create request's outcome is unknown, the job becomes UNCERTAIN and we
  look for the task on Meshy instead of resubmitting.
* Polling and downloading never cost credits; they resume after a restart.
"""
from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from config import AppSettings
from database.db import Database
from models.generation import GenerationSettings
from models.job import POLLABLE, REMOTE_ACTIVE, Job, JobStatus
from services.download_manager import DownloadError, UrlExpiredError, download_task_assets
from services.meshy_client import (AmbiguousCreateError, AuthError, InsufficientCreditsError, MeshyClient, MeshyError,
                                   NotFoundError, RateLimitError, TransientError)
from services.unity_manager import UnityError, install_to_unity, resolve_destination
from utils.images import ImageValidationError, to_data_uri, validate_image
from utils.naming import asset_name_from_path, unique_asset_name, unique_path

log = logging.getLogger(__name__)

EventCallback = Callable[[str, int, str], None]


@dataclass
class AddResult:
    path: Path
    job_id: int | None
    error: str | None = None
    duplicate_of: int | None = None


class QueueManager:
    def __init__(self, db: Database, settings_provider: Callable[[], AppSettings],
                 client_factory: Callable[[], MeshyClient | None], *, on_event: EventCallback | None = None,
                 download_session_factory: Callable[[], requests.Session] = requests.Session,
                 tick: float = 0.5, min_poll_interval: float = 2.0, default_poll_interval: float = 5.0,
                 reconcile_delay: float = 5.0, reconcile_attempts: int = 4, download_attempts: int = 6,
                 balance_refresh_interval: float = 300.0):
        self.db = db
        self.settings_provider = settings_provider
        self.client_factory = client_factory
        self.on_event = on_event
        self._dl_session_factory = download_session_factory
        self.tick = tick
        self.min_poll_interval = min_poll_interval
        self.default_poll_interval = default_poll_interval
        self.reconcile_delay = reconcile_delay
        self.reconcile_attempts = reconcile_attempts
        self.download_attempts = download_attempts
        self.balance_refresh_interval = balance_refresh_interval

        self.paused = False
        self.pause_reason = ""
        self.balance: dict | None = None
        self.balance_error: str | None = None
        self.watch_batch_id: int | None = None
        self.watch_auto_submit = False

        self._client: MeshyClient | None = None
        self._submit_blocked_until = 0.0
        self._next_balance_refresh = 0.0
        self._balance_requested = threading.Event()
        self._commands: queue.Queue = queue.Queue()
        self._downloads: queue.Queue = queue.Queue()
        self._in_download: set[int] = set()
        self._dl_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    # ================================================================ lifecycle
    def start(self) -> None:
        recovered = self.db.recover_after_restart()
        if recovered:
            log.warning("%d job(s) were mid-submission when the app closed; verifying with Meshy", recovered)
        waiting = self.db.count_status(JobStatus.APPROVED)
        if waiting:
            # Never spend credits just because the app started.
            self.pause(f"Paused after restart: {waiting} approved job(s) waiting. Click Resume to submit.")
        resumed = self.db.count_status((*POLLABLE, JobStatus.UNCERTAIN, JobStatus.DOWNLOADING))
        if resumed:
            log.info("Resuming tracking of %d Meshy task(s) from the previous session", resumed)
        self._stop.clear()
        self._threads = [threading.Thread(target=self._engine_loop, name="QueueEngine", daemon=True),
                         threading.Thread(target=self._download_loop, name="Downloader", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self._downloads.put(None)
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []
        if self._client:
            self._client.close()
            self._client = None

    def pause(self, reason: str = "Paused by user") -> None:
        self.paused = True
        self.pause_reason = reason
        log.info("Queue paused: %s", reason)
        self._emit("paused", 0, reason)

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""
        self._submit_blocked_until = 0.0
        log.info("Queue resumed")
        self._emit("paused", 0, "")
        self._wake.set()

    def reset_client(self) -> None:
        """Call after the API key changes."""
        self._commands.put(self._drop_client)
        self._wake.set()

    def _drop_client(self) -> None:
        if self._client:
            self._client.close()
        self._client = None

    def request_balance_refresh(self) -> None:
        self._balance_requested.set()
        self._wake.set()

    # ================================================================== events
    def _emit(self, kind: str, job_id: int = 0, message: str = "") -> None:
        if self.on_event:
            try:
                self.on_event(kind, job_id, message)
            except Exception:  # noqa: BLE001
                log.exception("Event handler failed")

    def _update(self, job_id: int, **fields) -> None:
        self.db.update_job(job_id, **fields)
        self._emit("changed", job_id)

    # ============================================================ adding jobs
    def add_images(self, paths, origin: str = "manual", auto_approve: bool = False,
                   batch_id: int | None = None) -> list[AddResult]:
        settings = self.settings_provider()
        gen = settings.generation
        results: list[AddResult] = []
        for raw in paths:
            path = Path(raw)
            try:
                info = validate_image(path)
            except ImageValidationError as exc:
                results.append(AddResult(path, None, str(exc)))
                log.warning("Rejected %s: %s", path.name, exc)
                continue
            dup = self.db.find_duplicate(info.sha256)
            if dup:
                msg = f"Same image already queued/generated as job #{dup.id} ({dup.name}, {dup.status_label})."
                results.append(AddResult(path, None, msg, duplicate_of=dup.id))
                log.info("Skipped duplicate %s: %s", path.name, msg)
                continue
            fields = dict(name=asset_name_from_path(path), source_path=str(path), image_hash=info.sha256,
                          origin=origin, est_credits=gen.estimate_credits())
            if auto_approve and batch_id is not None:
                fields.update(status=JobStatus.APPROVED, batch_id=batch_id, settings_json=json.dumps(gen.to_dict()))
            job_id = self.db.add_job(**fields)
            log.info("Added job #%d %s (%s)", job_id, fields["name"], fields.get("status", JobStatus.QUEUED))
            results.append(AddResult(path, job_id))
            self._emit("changed", job_id)
        self._wake.set()
        return results

    def approve(self, job_ids: list[int] | None = None) -> tuple[int | None, list[int]]:
        """Approve QUEUED jobs for submission as one budgeted batch (call after the user confirms)."""
        settings = self.settings_provider()
        gen = settings.generation
        errors = gen.validate()
        if errors:
            raise ValueError("Invalid generation settings:\n" + "\n".join(errors))
        jobs = [j for j in self.db.jobs_by_status(JobStatus.QUEUED)
                if not j.hidden and (job_ids is None or j.id in job_ids)]
        if not jobs:
            return None, []
        batch_id = self.db.create_batch("manual", settings.max_credits_per_batch)
        snapshot = json.dumps(gen.to_dict())
        approved = []
        for j in jobs:
            if self.db.transition(j.id, JobStatus.QUEUED, JobStatus.APPROVED, batch_id=batch_id,
                                  settings_json=snapshot, est_credits=gen.estimate_credits(), message=""):
                approved.append(j.id)
                self._emit("changed", j.id)
        log.info("Approved batch #%d with %d job(s), budget %s", batch_id, len(approved),
                 settings.max_credits_per_batch or "unlimited")
        self._wake.set()
        return batch_id, approved

    def estimate(self, job_ids: list[int] | None = None) -> tuple[int, int | None]:
        """(number of QUEUED jobs, estimated total credits or None if unknown) using current settings."""
        jobs = [j for j in self.db.jobs_by_status(JobStatus.QUEUED)
                if not j.hidden and (job_ids is None or j.id in job_ids)]
        per = self.settings_provider().generation.estimate_credits()
        return len(jobs), (None if per is None else per * len(jobs))

    # ----------------------------------------------------------- folder watch
    def set_watch_auto_submit(self, enabled: bool) -> None:
        self.watch_auto_submit = enabled
        if enabled:
            budget = self.settings_provider().max_credits_per_batch
            self.watch_batch_id = self.db.create_batch("watcher", budget)
            log.info("Auto-submit enabled for watched folder (batch #%d, budget %s)", self.watch_batch_id,
                     budget or "unlimited")
        else:
            self.watch_batch_id = None

    def handle_watched_file(self, path: Path) -> AddResult:
        auto = self.watch_auto_submit and self.watch_batch_id is not None
        res = self.add_images([path], origin="watcher", auto_approve=auto, batch_id=self.watch_batch_id)[0]
        if res.job_id is None and self.settings_provider().archive_processed:
            sub = "Skipped" if res.duplicate_of else "Failed"
            self._move_source(path, sub)
        return res

    def _move_source(self, src: Path, sub: str) -> Path | None:
        try:
            if not src.exists():
                return None
            dest = unique_path(src.parent / sub / src.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            return dest
        except OSError:
            log.exception("Could not move %s", src.name)
            return None

    def _archive(self, job: Job, sub: str) -> None:
        if job.origin == "watcher" and self.settings_provider().archive_processed:
            dest = self._move_source(Path(job.source_path), sub)
            if dest:
                self.db.update_job(job.id, source_path=str(dest))

    # ============================================================ user actions
    def retry(self, job_id: int) -> str:
        job = self.db.get_job(job_id)
        if not job:
            return "Job not found."
        if job.status == JobStatus.DOWNLOAD_FAILED:
            self.db.transition(job_id, JobStatus.DOWNLOAD_FAILED, JobStatus.DOWNLOADING, attempts=0,
                               next_action_at=0, message="Retrying download (no new generation, no credits).")
            self._emit("changed", job_id)
            self._wake.set()
            return "Download will be retried."
        if job.status in (JobStatus.FAILED, JobStatus.CANCELED):
            dup = self.db.find_duplicate(job.image_hash, exclude_job=job_id)
            if dup:
                return f"Not retried: job #{dup.id} already covers this image."
            self.db.transition(job_id, (JobStatus.FAILED, JobStatus.CANCELED), JobStatus.QUEUED, task_id=None,
                               progress=0, consumed_credits=None, attempts=0, batch_id=None,
                               message="Re-queued. Submit to generate again (uses credits).")
            self._emit("changed", job_id)
            return "Re-queued. Use 'Submit queued' to generate again."
        return f"Cannot retry a job that is {job.status_label.lower()}."

    def remove(self, job_id: int) -> str:
        job = self.db.get_job(job_id)
        if not job:
            return "Job not found."
        if job.status in (JobStatus.QUEUED, JobStatus.APPROVED):
            # Never submitted: safe to drop entirely.
            self.db.transition(job_id, (JobStatus.QUEUED, JobStatus.APPROVED), JobStatus.CANCELED, hidden=1,
                               message="Removed before submission.")
        elif job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED, JobStatus.DOWNLOAD_FAILED):
            self.db.update_job(job_id, hidden=1)  # kept in DB so task IDs are never lost
        else:
            return "Cannot remove a job that is still running on Meshy."
        self._emit("changed", job_id)
        return "Removed."

    def cancel(self, job_id: int) -> None:
        """Cancel a PENDING Meshy task (Meshy refunds PENDING tasks)."""
        self._commands.put(lambda: self._do_cancel(job_id))
        self._wake.set()

    def _do_cancel(self, job_id: int) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        if job.status in (JobStatus.QUEUED, JobStatus.APPROVED):
            self.db.transition(job_id, (JobStatus.QUEUED, JobStatus.APPROVED), JobStatus.CANCELED,
                               message="Canceled before submission.")
            self._emit("changed", job_id)
            return
        if job.status != JobStatus.PENDING or not job.task_id:
            self._emit("info", job_id, "Only queued or Meshy-PENDING jobs can be canceled.")
            return
        client = self._get_client()
        if not client:
            return
        try:
            client.delete_task(job.task_id)
        except MeshyError as exc:
            msg = "Meshy already started this task; it can no longer be canceled." if exc.status == 409 else str(exc)
            self._emit("info", job_id, msg)
            return
        self.db.transition(job_id, JobStatus.PENDING, JobStatus.CANCELED, finished_at=time.time(),
                           consumed_credits=0, message="Canceled on Meshy while pending (refunded).")
        self._emit("changed", job_id)
        self.request_balance_refresh()

    # ============================================================ engine loop
    def _get_client(self) -> MeshyClient | None:
        if self._client is None:
            try:
                self._client = self.client_factory()
            except AuthError:
                self._client = None
        return self._client

    def _engine_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._engine_step()
            except Exception:  # noqa: BLE001
                log.exception("Queue engine error")
            self._wake.wait(self.tick)
            self._wake.clear()

    def _engine_step(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                break
            cmd()
        client = self._get_client()
        if client is None:
            return
        now = time.time()
        if self._balance_requested.is_set() or now >= self._next_balance_refresh:
            self._balance_requested.clear()
            self._refresh_balance(client)
        self._reconcile_uncertain(client)
        self._poll_tasks(client)
        self._queue_downloads()
        if not self.paused:
            self._submit_next(client)

    def _refresh_balance(self, client: MeshyClient) -> int | None:
        self._next_balance_refresh = time.time() + self.balance_refresh_interval
        try:
            self.balance = client.get_balance()
            self.balance_error = None
        except MeshyError as exc:
            self.balance_error = exc.message
            if isinstance(exc, AuthError):
                self.balance = None
        self._emit("balance", 0, self.balance_error or "")
        return None if self.balance is None else int(self.balance.get("balance", 0))

    # ------------------------------------------------------------- submitting
    def _submit_next(self, client: MeshyClient) -> None:
        now = time.time()
        if now < self._submit_blocked_until:
            return
        settings = self.settings_provider()
        if self.db.count_status(REMOTE_ACTIVE) >= max(1, settings.concurrency):
            return
        candidates = self.db.jobs_by_status(JobStatus.APPROVED)
        if not candidates:
            return
        job = candidates[0]
        gen = GenerationSettings.from_dict(json.loads(job.settings_json or "{}"))
        errors = gen.validate()
        if errors:
            self._fail(job, "Invalid settings: " + " ".join(errors))
            return
        est = gen.estimate_credits()

        # Budget check.
        batch = self.db.get_batch(job.batch_id) if job.batch_id else None
        budget = batch["budget"] if batch else 0
        if budget:
            spent = self.db.batch_committed_credits(job.batch_id, exclude_job=job.id)
            if est is None or spent + est > budget:
                why = ("cost unknown, budget cannot be enforced" if est is None
                       else f"{spent} + {est} would exceed the batch budget of {budget} credits")
                self.db.transition(job.id, JobStatus.APPROVED, JobStatus.QUEUED, batch_id=None,
                                   message=f"Not submitted: {why}. Submit again to start a new batch.")
                self._emit("changed", job.id)
                self._emit("budget", job.id, f"Batch budget reached ({budget} credits). Remaining jobs stay queued.")
                return

        # Duplicate safety (race-free: also checked at add time).
        dup = self.db.find_duplicate(job.image_hash, exclude_job=job.id)
        if dup and dup.task_id:
            self._fail(job, f"Duplicate of job #{dup.id}, which was already submitted.")
            return

        # Live balance check before spending.
        balance = self._refresh_balance(client)
        if balance is None:
            self._submit_blocked_until = now + 30
            self._update(job.id, message=f"Waiting: could not check balance ({self.balance_error}).")
            return
        if est is not None and balance < est:
            self._update(job.id, message=f"Waiting: balance {balance} is below the estimated {est} credits.")
            self.pause(f"Insufficient credits (balance {balance}, need ~{est}).")
            return

        try:
            data_uri = to_data_uri(job.source_path)
        except ImageValidationError as exc:
            self._fail(job, f"Image problem: {exc}")
            return
        payload = gen.to_payload(data_uri)

        if not self.db.transition(job.id, JobStatus.APPROVED, JobStatus.SUBMITTING, submit_started_at=time.time(),
                                  message="Submitting to Meshy..."):
            return
        self._emit("changed", job.id)
        log.info("Submitting job #%d %s (%s, est. %s credits)", job.id, job.name, gen.describe(), est)
        try:
            task_id = client.create_image_to_3d(payload)
        except RateLimitError as exc:
            wait = exc.retry_after if exc.retry_after is not None else (30 if exc.concurrency_limited else 10)
            self._submit_blocked_until = time.time() + wait
            self._revert(job, f"Meshy rate limit: {exc.message}. Retrying submission in {wait:.0f}s.")
        except TransientError as exc:
            # The request never reached Meshy (connection failed), so retrying is safe.
            self._submit_blocked_until = time.time() + 15
            self._revert(job, f"{exc.message} Will retry submission.")
        except AmbiguousCreateError as exc:
            log.warning("Job #%d: create outcome unknown (%s); will reconcile", job.id, exc.message)
            self.db.transition(job.id, JobStatus.SUBMITTING, JobStatus.UNCERTAIN,
                               next_action_at=time.time() + self.reconcile_delay, attempts=0,
                               message=f"{exc.message} Checking Meshy - it will NOT be resubmitted automatically.")
            self._emit("changed", job.id)
        except InsufficientCreditsError as exc:
            self._fail(job, exc.message, from_status=JobStatus.SUBMITTING)
            self.pause("Meshy reported insufficient credits (402).")
        except AuthError as exc:
            self._revert(job, exc.message)
            self.pause("Invalid API key. Update it in Settings, then Resume.")
        except MeshyError as exc:
            self._fail(job, exc.message, from_status=JobStatus.SUBMITTING)
        else:
            self._accept_task(job.id, task_id, JobStatus.SUBMITTING)

    def _accept_task(self, job_id: int, task_id: str, from_status: str, note: str = "") -> None:
        # Record the task ID before anything else so it is never lost.
        self.db.record_task_id(task_id, job_id)
        self.db.transition(job_id, from_status, JobStatus.PENDING, task_id=task_id, submitted_at=time.time(),
                           next_action_at=time.time() + self.min_poll_interval, attempts=0,
                           message=note or "Submitted. Waiting for Meshy...")
        log.info("Job #%d -> Meshy task %s", job_id, task_id)
        self._emit("changed", job_id)

    def _revert(self, job: Job, message: str) -> None:
        self.db.transition(job.id, JobStatus.SUBMITTING, JobStatus.APPROVED, message=message)
        self._emit("changed", job.id)

    def _fail(self, job: Job, message: str, from_status: str | None = None) -> None:
        self.db.transition(job.id, from_status or job.status, JobStatus.FAILED, finished_at=time.time(),
                           message=message)
        log.error("Job #%d %s failed: %s", job.id, job.name, message)
        self._archive(self.db.get_job(job.id), "Failed")
        self._emit("changed", job.id)
        self._emit("failed", job.id, message)

    # ----------------------------------------------------------- reconciling
    def _reconcile_uncertain(self, client: MeshyClient) -> None:
        jobs = self.db.jobs_by_status(JobStatus.UNCERTAIN, due_before=time.time())
        if not jobs:
            return
        try:
            tasks = client.list_tasks(page_size=50)
        except MeshyError as exc:
            for j in jobs:
                self.db.update_job(j.id, next_action_at=time.time() + 30,
                                   message=f"Still confirming with Meshy ({exc.message}).")
            return
        known = self.db.known_task_ids()
        for job in jobs:
            start_ms = (job.submit_started_at or job.created_at) * 1000
            candidates = [t for t in tasks if t.get("id") and t["id"] not in known
                          and start_ms - 120_000 <= int(t.get("created_at") or 0) <= start_ms + 900_000]
            if candidates:
                best = min(candidates, key=lambda t: abs(int(t.get("created_at") or 0) - start_ms))
                note = "Recovered task created during an interrupted submission."
                if len(candidates) > 1:
                    note += f" ({len(candidates)} possible matches; picked the closest in time - please verify.)"
                log.warning("Job #%d: adopted Meshy task %s after uncertain submission", job.id, best["id"])
                self._accept_task(job.id, best["id"], JobStatus.UNCERTAIN, note)
                known.add(best["id"])
                continue
            attempts = job.attempts + 1
            if attempts >= self.reconcile_attempts:
                self._fail(job, "Meshy did not create a task for this submission (no matching task found). "
                                "Not resubmitted automatically - click Retry to submit again.")
            else:
                self.db.update_job(job.id, attempts=attempts, next_action_at=time.time() + self.reconcile_delay,
                                   message=f"Confirming with Meshy (check {attempts}/{self.reconcile_attempts})...")
                self._emit("changed", job.id)

    # --------------------------------------------------------------- polling
    def _poll_tasks(self, client: MeshyClient) -> None:
        for job in self.db.jobs_by_status(POLLABLE, due_before=time.time()):
            if self._stop.is_set():
                return
            try:
                task, retry_after = client.get_task(job.task_id)
            except RateLimitError as exc:
                self.db.update_job(job.id, next_action_at=time.time() + (exc.retry_after or 10))
                return
            except NotFoundError:
                self._fail(job, "Task no longer exists on Meshy (deleted, or past the 3-day retention).")
                continue
            except AuthError as exc:
                self.db.update_job(job.id, next_action_at=time.time() + 60, message=exc.message)
                self._emit("changed", job.id)
                continue
            except MeshyError as exc:
                # Timeouts/5xx while polling never mean the task failed; just poll again later.
                delay = min(120, 10 * (job.attempts + 1))
                self.db.update_job(job.id, attempts=job.attempts + 1, next_action_at=time.time() + delay,
                                   message=f"Connection issue ({exc.message}); retrying in {delay}s.")
                self._emit("changed", job.id)
                continue
            self._apply_task(job, task, retry_after)

    def _apply_task(self, job: Job, task: dict, retry_after: float | None) -> None:
        status = task.get("status")
        fields = dict(progress=int(task.get("progress") or 0), attempts=0)
        if task.get("consumed_credits") is not None:
            fields["consumed_credits"] = int(task["consumed_credits"])
        if task.get("expires_at"):
            fields["expires_at"] = int(task["expires_at"])
        if status == "SUCCEEDED":
            self.db.transition(job.id, POLLABLE, JobStatus.DOWNLOADING, next_action_at=0,
                               message="Generated. Downloading files...", **{**fields, "progress": 100})
            self._emit("changed", job.id)
            self._wake.set()
        elif status == "FAILED":
            err = task.get("task_error") or {}
            msg = err.get("message") or "Meshy reported the task as failed."
            if err.get("code"):
                msg += f" [{err['code']}]"
            self.db.update_job(job.id, **{**fields, "consumed_credits": int(task.get("consumed_credits") or 0)})
            self._fail(job, f"Meshy: {msg} (failed tasks are refunded)")
            self.request_balance_refresh()
        elif status == "CANCELED":
            self.db.transition(job.id, POLLABLE, JobStatus.CANCELED, finished_at=time.time(),
                               message="Canceled on Meshy.", **fields)
            self._emit("changed", job.id)
        else:
            delay = retry_after if retry_after is not None else self.default_poll_interval
            delay = max(self.min_poll_interval, delay)
            new_status = JobStatus.IN_PROGRESS if status == "IN_PROGRESS" else JobStatus.PENDING
            if new_status == JobStatus.PENDING and task.get("preceding_tasks"):
                msg = f"Waiting in Meshy queue ({task['preceding_tasks']} ahead)."
            elif new_status == JobStatus.PENDING:
                msg = "Waiting in Meshy queue."
            else:
                msg = f"Generating... {fields['progress']}%"
            self.db.transition(job.id, POLLABLE, new_status, next_action_at=time.time() + delay, message=msg,
                               **fields)
            self._emit("changed", job.id)

    # ------------------------------------------------------------- downloads
    def _queue_downloads(self) -> None:
        for job in self.db.jobs_by_status(JobStatus.DOWNLOADING, due_before=time.time()):
            with self._dl_lock:
                if job.id in self._in_download:
                    continue
                self._in_download.add(job.id)
            self._downloads.put(job.id)

    def _download_loop(self) -> None:
        session = self._dl_session_factory()
        try:
            while not self._stop.is_set():
                job_id = self._downloads.get()
                if job_id is None:
                    break
                try:
                    self._download_job(job_id, session)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Unexpected download error for job #%d", job_id)
                    self._download_retry_later(self.db.get_job(job_id), f"Unexpected error: {exc}")
                finally:
                    with self._dl_lock:
                        self._in_download.discard(job_id)
                    self._wake.set()
        finally:
            session.close()

    def _download_retry_later(self, job: Job | None, message: str) -> None:
        if not job:
            return
        attempts = job.attempts + 1
        if attempts >= self.download_attempts:
            self.db.transition(job.id, JobStatus.DOWNLOADING, JobStatus.DOWNLOAD_FAILED, attempts=attempts,
                               message=f"{message} Click Retry to download again (no extra credits).")
            self._emit("changed", job.id)
            self._emit("failed", job.id, f"Download failed: {message}")
        else:
            delay = min(300, 15 * attempts)
            self.db.update_job(job.id, attempts=attempts, next_action_at=time.time() + delay,
                               message=f"{message} Retrying download in {delay}s.")
            self._emit("changed", job.id)

    def _download_job(self, job_id: int, session: requests.Session) -> None:
        job = self.db.get_job(job_id)
        if not job or job.status != JobStatus.DOWNLOADING:
            return
        client = self._get_client()
        if client is None:
            self._download_retry_later(job, "No API key configured.")
            return
        try:
            task, _ = client.get_task(job.task_id)  # fresh signed URLs
        except NotFoundError:
            self._fail(job, "Meshy no longer has this task's files (assets are kept for 3 days).")
            return
        except MeshyError as exc:
            self._download_retry_later(job, exc.message)
            return

        settings = self.settings_provider()
        output_root = Path(settings.output_dir)
        if job.output_dir:
            staging, name = Path(job.output_dir), job.name
        else:
            roots = [output_root]
            if settings.unity_project:
                try:
                    roots.append(resolve_destination(settings.unity_project, settings.unity_dest))
                except UnityError:
                    pass
            name = unique_asset_name(job.name, roots)
            staging = output_root / name
            self.db.update_job(job.id, name=name, output_dir=str(staging))
        gen = GenerationSettings.from_dict(json.loads(job.settings_json or "{}"))
        try:
            meta = download_task_assets(task, name, staging, gen.export_formats, session,
                                        refresh_task=lambda: client.get_task(job.task_id)[0])
        except UrlExpiredError:
            self._fail(job, "Download links have expired and Meshy no longer provides the files.")
            return
        except (DownloadError, OSError) as exc:
            self._download_retry_later(self.db.get_job(job.id), str(exc))
            return

        unity_dir, note = None, ""
        if settings.unity_project:
            try:
                unity_dir = str(install_to_unity(staging, name, settings.unity_project, settings.unity_dest,
                                                 settings.unity_per_model_folder))
            except (UnityError, OSError) as exc:
                note = f" Unity copy failed: {exc}"
                log.error("Job #%d: %s", job.id, note.strip())
        consumed = meta.get("consumed_credits")
        where = f"Unity: {unity_dir}" if unity_dir else f"Output: {staging}"
        self.db.transition(job.id, JobStatus.DOWNLOADING, JobStatus.COMPLETED, finished_at=time.time(),
                           progress=100, unity_dir=unity_dir, attempts=0,
                           consumed_credits=consumed if consumed is not None else job.consumed_credits,
                           message=f"Done. {where}.{note}")
        log.info("Job #%d %s completed (%s credits). %s", job.id, name, consumed, where)
        self._archive(self.db.get_job(job.id), "Processed")
        self._emit("changed", job.id)
        self._emit("completed", job.id, f"{name} is ready ({consumed if consumed is not None else '?'} credits).")
        self.request_balance_refresh()
