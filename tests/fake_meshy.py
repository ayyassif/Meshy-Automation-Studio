"""A local fake of the Meshy API used for tests and credit-free demos.

Mirrors the documented endpoints/shapes: POST/GET/LIST/DELETE /openapi/v1/image-to-3d,
GET /openapi/v1/balance, Retry-After headers, 401/402/429 errors, signed asset URLs.

Run standalone for a GUI demo:
    python -m tests.fake_meshy            (listens on http://127.0.0.1:8765, key "test-key")
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PRICES = {"meshy-t2.5": (5, 15, 15, 20), "meshy-t2": (5, 15, 15, 20), "latest": (20, 30, 30, 35),
          "meshy-7.1": (20, 30, 30, 35), "meshy-6": (20, 30, 30, 35), "meshy-6-lite": (5, 15, 15, 15)}


class FakeState:
    def __init__(self):
        self.api_key = "test-key"
        self.balance = 1000
        self.tasks: dict[str, dict] = {}
        self.create_count = 0
        self.polls_to_finish = 3
        self.retry_after = "0"
        self.create_delay = 0.0          # sleep AFTER creating the task (simulates lost response)
        self.create_error: tuple[int, str] | None = None
        self.rate_limit_creates = 0
        self.fail_tasks = False
        self.hold_pending = False       # keep tasks PENDING forever
        self.truncate_once: set[str] = set()   # asset names to cut off mid-transfer once
        self.asset_size = 300_000
        self.lock = threading.Lock()

    def expire_urls(self, task_id: str) -> None:
        self.tasks[task_id]["sig"] += 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "FakeMeshyServer"

    def log_message(self, *args):  # silence
        pass

    @property
    def st(self) -> FakeState:
        return self.server.state

    def _json(self, code: int, body, headers: dict | None = None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.st.api_key}":
            self._json(401, {"message": "Invalid API key"})
            return False
        return True

    def _task_view(self, t: dict) -> dict:
        base = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        view = {k: v for k, v in t.items() if k not in ("sig", "polls", "payload")}
        if t["status"] == "SUCCEEDED":
            q = f"?Expires=9999&Signature={t['sig']}"
            fmts = t["payload"].get("target_formats") or ["glb", "fbx", "obj", "usdz"]
            view["model_urls"] = {f: f"{base}/assets/{t['id']}/model.{f}{q}" for f in fmts}
            if "obj" in fmts:
                view["model_urls"]["mtl"] = f"{base}/assets/{t['id']}/model.mtl{q}"
            if t["payload"].get("should_texture", True):
                tex = {"base_color": f"{base}/assets/{t['id']}/texture_0.png{q}"}
                if t["payload"].get("enable_pbr"):
                    for k in ("metallic", "roughness", "normal"):
                        tex[k] = f"{base}/assets/{t['id']}/texture_0_{k}.png{q}"
                view["texture_urls"] = [tex]
            view["thumbnail_url"] = f"{base}/assets/{t['id']}/preview.png{q}"
        return view

    # --------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/assets/"):
            return self._asset(url)
        if not self._authed():
            return
        if url.path == "/openapi/v1/balance":
            return self._json(200, {"balance": self.st.balance, "monthly_credits": self.st.balance,
                                    "permanent_credits": 0, "monthly_resets_at": None})
        if url.path == "/openapi/v1/image-to-3d":
            qs = parse_qs(url.query)
            size = int(qs.get("page_size", ["10"])[0])
            tasks = sorted(self.st.tasks.values(), key=lambda t: -t["created_at"])[:size]
            return self._json(200, [self._task_view(t) for t in tasks])
        if url.path.startswith("/openapi/v1/image-to-3d/"):
            tid = url.path.rsplit("/", 1)[1]
            with self.st.lock:
                t = self.st.tasks.get(tid)
                if not t:
                    return self._json(404, {"message": "Task not found"})
                if t["status"] in ("PENDING", "IN_PROGRESS") and not self.st.hold_pending:
                    t["polls"] += 1
                    if t["polls"] >= self.st.polls_to_finish:
                        if self.st.fail_tasks:
                            t.update(status="FAILED", consumed_credits=0, finished_at=int(time.time() * 1000),
                                     task_error={"type": "invalid_input", "code": "image_too_complex",
                                                 "message": "The image is too complex."})
                        else:
                            t.update(status="SUCCEEDED", progress=100, finished_at=int(time.time() * 1000))
                    elif t["polls"] >= 1:
                        t.update(status="IN_PROGRESS", progress=int(100 * t["polls"] / self.st.polls_to_finish),
                                 preceding_tasks=0, started_at=t["started_at"] or int(time.time() * 1000))
                view = self._task_view(t)
            headers = {"Retry-After": self.st.retry_after} if view["status"] in ("PENDING", "IN_PROGRESS") else {}
            return self._json(200, view, headers)
        self._json(404, {"message": "Not found"})

    def _asset(self, url):
        _, _, tid, name = url.path.split("/", 3)
        t = self.st.tasks.get(tid)
        sig = parse_qs(url.query).get("Signature", [""])[0]
        if not t or str(t["sig"]) != sig:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = (f"{name}:{tid}:".encode() * (self.st.asset_size // 20 + 1))[: self.st.asset_size]
        if name.endswith(".mtl"):
            body = b"newmtl material_0\nKd 1 1 1\nmap_Kd texture_0.png\n"
        start = 0
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            self.send_response(200)
        chunk = body[start:]
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if name in self.st.truncate_once and start == 0:
            self.st.truncate_once.discard(name)
            self.wfile.write(chunk[: len(chunk) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(chunk)

    # -------------------------------------------------------------- POST
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if not self._authed():
            return
        if urlparse(self.path).path != "/openapi/v1/image-to-3d":
            return self._json(404, {"message": "Not found"})
        payload = json.loads(raw or b"{}")
        st = self.st
        if st.rate_limit_creates > 0:
            st.rate_limit_creates -= 1
            return self._json(429, {"message": "Rate limit exceeded"}, {"Retry-After": "1"})
        if st.create_error:
            code, msg = st.create_error
            return self._json(code, {"message": msg})
        if not str(payload.get("image_url", "")).startswith(("data:image/", "http")):
            return self._json(400, {"message": "Invalid image_url"})
        prices = PRICES.get(payload.get("ai_model", "latest"), (20, 30, 30, 35))
        cost = prices[{"2k": 1, "4k": 2, "8k": 3}[payload.get("texture_resolution", "2k")]] \
            if payload.get("should_texture", True) else prices[0]
        if st.balance < cost:
            return self._json(402, {"message": "Insufficient credits"})
        tid = str(uuid.uuid4())
        now = int(time.time() * 1000)
        with st.lock:
            st.balance -= cost
            st.create_count += 1
            st.tasks[tid] = {"id": tid, "type": "image-to-3d", "status": "PENDING", "progress": 0,
                             "preceding_tasks": 1, "consumed_credits": cost, "task_error": None,
                             "created_at": now, "started_at": 0, "finished_at": 0,
                             "expires_at": now + 3 * 86400_000, "sig": 1, "polls": 0, "payload": payload}
        if st.create_delay:
            time.sleep(st.create_delay)
        self._json(202, {"result": tid})

    def do_DELETE(self):
        if not self._authed():
            return
        tid = urlparse(self.path).path.rsplit("/", 1)[1]
        t = self.st.tasks.get(tid)
        if not t:
            return self._json(404, {"message": "Task not found"})
        if t["status"] == "IN_PROGRESS":
            return self._json(409, {"message": "Task is IN_PROGRESS and cannot be deleted."})
        if t["status"] == "PENDING":
            self.st.balance += t["consumed_credits"]
        del self.st.tasks[tid]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeMeshyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.state = FakeState()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "FakeMeshyServer":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


if __name__ == "__main__":
    srv = FakeMeshyServer(8765)
    srv.state.retry_after = "2"
    srv.state.polls_to_finish = 6
    print(f"Fake Meshy API at {srv.base_url} (API key: test-key). Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
