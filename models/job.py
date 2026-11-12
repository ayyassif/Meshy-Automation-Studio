"""Job model and status constants."""
from __future__ import annotations

from dataclasses import dataclass


class JobStatus:
    QUEUED = "QUEUED"                # added, waiting for user approval (no credits spent)
    APPROVED = "APPROVED"            # approved, waiting for a free submission slot
    SUBMITTING = "SUBMITTING"        # create request in flight
    UNCERTAIN = "UNCERTAIN"          # create request outcome unknown; reconciling with Meshy
    PENDING = "PENDING"              # Meshy task queued
    IN_PROGRESS = "IN_PROGRESS"      # Meshy task generating
    DOWNLOADING = "DOWNLOADING"      # task succeeded, downloading/installing
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"  # task succeeded, files could not be fetched (retry = re-download only)
    CANCELED = "CANCELED"


# Jobs occupying a Meshy generation slot.
REMOTE_ACTIVE = (JobStatus.SUBMITTING, JobStatus.UNCERTAIN, JobStatus.PENDING, JobStatus.IN_PROGRESS)
POLLABLE = (JobStatus.PENDING, JobStatus.IN_PROGRESS)
TERMINAL = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED)
# A job in one of these states blocks a new job for the same image (duplicate protection).
NON_DUPLICATE_BLOCKING = (JobStatus.FAILED, JobStatus.CANCELED)

STATUS_LABELS = {
    JobStatus.QUEUED: "Queued (awaiting approval)",
    JobStatus.APPROVED: "Waiting to submit",
    JobStatus.SUBMITTING: "Submitting",
    JobStatus.UNCERTAIN: "Confirming with Meshy",
    JobStatus.PENDING: "Pending on Meshy",
    JobStatus.IN_PROGRESS: "Generating",
    JobStatus.DOWNLOADING: "Downloading",
    JobStatus.COMPLETED: "Completed",
    JobStatus.FAILED: "Failed",
    JobStatus.DOWNLOAD_FAILED: "Download failed",
    JobStatus.CANCELED: "Canceled",
}


@dataclass
class Job:
    id: int
    name: str
    source_path: str
    image_hash: str
    origin: str
    status: str
    batch_id: int | None
    settings_json: str | None
    task_id: str | None
    progress: int
    est_credits: int | None
    consumed_credits: int | None
    message: str
    attempts: int
    created_at: float
    updated_at: float
    submit_started_at: float | None
    submitted_at: float | None
    finished_at: float | None
    next_action_at: float
    expires_at: int | None
    output_dir: str | None
    unity_dir: str | None
    hidden: int

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)
