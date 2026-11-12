"""Minimal client for the official Meshy REST API (https://docs.meshy.ai/en/api).

Design rules:
* No automatic retries inside the client. Callers decide what is safe to retry.
* A failed create request is classified as either *not sent* (safe to retry) or
  *ambiguous* (Meshy may have created the task) - never silently resubmitted.
* The API key is only placed in the Authorization header; it is never logged.
"""
from __future__ import annotations

import email.utils
import logging
import threading
import time

import requests
import urllib3

from config import DEFAULT_API_BASE

log = logging.getLogger(__name__)

IMAGE_TO_3D = "/openapi/v1/image-to-3d"
BALANCE = "/openapi/v1/balance"


class MeshyError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


class AuthError(MeshyError):
    """401 - missing or invalid API key."""


class InsufficientCreditsError(MeshyError):
    """402 - not enough credits (or plan does not allow API task creation)."""


class NotFoundError(MeshyError):
    """404."""


class RateLimitError(MeshyError):
    """429 - too many requests per second, or too many concurrent tasks."""

    @property
    def concurrency_limited(self) -> bool:
        return self.message.startswith(("NoMorePendingTasks", "NoMoreConcurrentTasks"))


class TransientError(MeshyError):
    """Network problem or 5xx where retrying is safe (request was a GET, or never sent)."""


class AmbiguousCreateError(MeshyError):
    """The create request may or may not have created a task on Meshy."""


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _request_not_sent(exc: BaseException) -> bool:
    """True when the failure happened before any bytes reached Meshy (connect/DNS failure)."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    for arg in getattr(exc, "args", ()):
        reason = getattr(arg, "reason", arg)
        if isinstance(reason, urllib3.exceptions.NewConnectionError):  # includes NameResolutionError
            return True
    return False


def _error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except ValueError:
        pass
    return (resp.text or resp.reason or f"HTTP {resp.status_code}")[:300]


class MeshyClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_API_BASE, session: requests.Session | None = None,
                 get_timeout: float = 30.0, post_timeout: float = 60.0, connect_timeout: float = 10.0,
                 min_interval: float = 0.1):
        if not api_key:
            raise AuthError("No Meshy API key configured.")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self.get_timeout = get_timeout
        self.post_timeout = post_timeout
        self.connect_timeout = connect_timeout
        self.min_interval = min_interval  # stay well below the documented 20 requests/second
        self._lock = threading.Lock()
        self._last_request = 0.0

    def close(self) -> None:
        self._session.close()

    # --------------------------------------------------------------- core
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _send(self, method: str, path: str, *, json=None, params=None, ambiguous_on_failure: bool = False):
        self._throttle()
        timeout = (self.connect_timeout, self.post_timeout if method == "POST" else self.get_timeout)
        headers = {"Authorization": f"Bearer {self._key}"}
        try:
            resp = self._session.request(method, self.base_url + path, json=json, params=params,
                                         headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            kind = type(exc).__name__
            if _request_not_sent(exc):
                raise TransientError(f"Could not connect to Meshy ({kind}).") from None
            if ambiguous_on_failure:
                raise AmbiguousCreateError(f"No response from Meshy ({kind}); the task may have been created.") from None
            raise TransientError(f"Network error talking to Meshy ({kind}).") from None

        retry_after = parse_retry_after(resp.headers.get("Retry-After"))
        code = resp.status_code
        if 200 <= code < 300:
            return resp, retry_after
        msg = _error_message(resp)
        if code == 429:
            raise RateLimitError(msg, code, retry_after)
        if code == 401:
            raise AuthError(f"Meshy rejected the API key (401): {msg}", code)
        if code == 402:
            raise InsufficientCreditsError(f"Insufficient Meshy credits or plan (402): {msg}", code)
        if code == 404:
            raise NotFoundError(f"Not found (404): {msg}", code)
        if code >= 500:
            if ambiguous_on_failure:
                raise AmbiguousCreateError(f"Meshy server error {code}; the task may have been created.", code,
                                           retry_after)
            raise TransientError(f"Meshy server error {code}: {msg}", code, retry_after)
        raise MeshyError(f"Meshy error {code}: {msg}", code, retry_after)

    # ---------------------------------------------------------- endpoints
    def get_balance(self) -> dict:
        resp, _ = self._send("GET", BALANCE)
        return resp.json()

    def create_image_to_3d(self, payload: dict) -> str:
        """Returns the new task ID. Raises AmbiguousCreateError when the outcome is unknown."""
        resp, _ = self._send("POST", IMAGE_TO_3D, json=payload, ambiguous_on_failure=True)
        try:
            task_id = resp.json().get("result")
        except ValueError:
            task_id = None
        if not task_id:
            raise AmbiguousCreateError("Meshy accepted the request but returned no task ID.", resp.status_code)
        return str(task_id)

    def get_task(self, task_id: str) -> tuple[dict, float | None]:
        resp, retry_after = self._send("GET", f"{IMAGE_TO_3D}/{task_id}")
        return resp.json(), retry_after

    def list_tasks(self, page_size: int = 50, page_num: int = 1) -> list[dict]:
        resp, _ = self._send("GET", IMAGE_TO_3D,
                             params={"page_size": page_size, "page_num": page_num, "sort_by": "-created_at"})
        data = resp.json()
        return data if isinstance(data, list) else []

    def delete_task(self, task_id: str) -> None:
        self._send("DELETE", f"{IMAGE_TO_3D}/{task_id}")
