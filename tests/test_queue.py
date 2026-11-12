"""End-to-end queue tests against the fake Meshy server (no real credits)."""
import pytest

from config import AppSettings
from database.db import Database
from models.generation import GenerationSettings
from models.job import JobStatus
from services.meshy_client import MeshyClient
from services.queue_manager import QueueManager
from tests.conftest import make_image, wait_for
from tests.test_services import make_unity_project


@pytest.fixture
def env(tmp_path, fake_server):
    settings = AppSettings(input_dir=str(tmp_path / "Input"), output_dir=str(tmp_path / "Output"),
                           unity_project=str(make_unity_project(tmp_path / "Game")), max_credits_per_batch=100)
    db = Database(tmp_path / "jobs.db")
    events = []
    state = {"key": "test-key", "post_timeout": 60.0}

    def make_qm():
        return QueueManager(
            db, lambda: settings,
            lambda: MeshyClient(state["key"], base_url=fake_server.base_url, min_interval=0,
                                post_timeout=state["post_timeout"]),
            on_event=lambda k, j, m: events.append((k, j, m)), tick=0.05, min_poll_interval=0.05,
            reconcile_delay=0.1, balance_refresh_interval=3600)

    env = type("Env", (), dict(settings=settings, db=db, events=events, srv=fake_server, make_qm=make_qm,
                               tmp=tmp_path, state=state))
    yield env
    db.close()


def status(env, job_id):
    return env.db.get_job(job_id).status


def test_full_flow_manual_batch(env):
    qm = env.make_qm()
    qm.start()
    try:
        res = qm.add_images([make_image(env.tmp / "imgs" / "panda.png")])
        jid = res[0].job_id
        assert status(env, jid) == JobStatus.QUEUED
        import time
        time.sleep(0.3)
        assert env.srv.state.create_count == 0  # nothing submitted without approval
        assert qm.estimate() == (1, 15)
        batch, approved = qm.approve()
        assert approved == [jid]
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED)
        wait_for(lambda: qm.balance and qm.balance["balance"] == 985)  # refreshed after completion
    finally:
        qm.stop()
    job = env.db.get_job(jid)
    assert env.srv.state.create_count == 1
    assert job.consumed_credits == 15 and job.task_id
    unity = env.tmp / "Game" / "Assets" / "Art" / "Models" / "Panda"
    assert (unity / "Panda.fbx").exists() and (unity / "Textures" / "Panda_BaseColor.png").exists()
    assert job.unity_dir == str(unity)
    assert any(k == "completed" and j == jid for k, j, _ in env.events)


def test_duplicates_rejected(env):
    qm = env.make_qm()
    img = make_image(env.tmp / "a.png")
    copy = env.tmp / "b.png"
    copy.write_bytes(img.read_bytes())
    r = qm.add_images([img, copy, img])
    assert r[0].job_id and r[1].duplicate_of == r[0].job_id and r[2].duplicate_of == r[0].job_id


def test_budget_limits_batch(env):
    env.settings.max_credits_per_batch = 30  # two jobs at 15 credits
    qm = env.make_qm()
    qm.start()
    try:
        ids = [qm.add_images([make_image(env.tmp / f"i{i}.png", color=(i * 40, 10, 10))])[0].job_id
               for i in range(3)]
        qm.approve()
        wait_for(lambda: sum(status(env, i) == JobStatus.COMPLETED for i in ids) == 2
                 and status(env, ids[2]) == JobStatus.QUEUED)
    finally:
        qm.stop()
    assert env.srv.state.create_count == 2
    assert "budget" in env.db.get_job(ids[2]).message
    assert any(k == "budget" for k, _, _ in env.events)


def test_insufficient_balance_pauses_without_submitting(env):
    env.srv.state.balance = 10
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: qm.paused)
    finally:
        qm.stop()
    assert env.srv.state.create_count == 0
    assert status(env, jid) == JobStatus.APPROVED and "Insufficient" in qm.pause_reason


def test_ambiguous_create_is_reconciled_not_resubmitted(env):
    env.srv.state.create_delay = 1.0
    env.state["post_timeout"] = 0.3
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED, timeout=20)
    finally:
        qm.stop()
    assert env.srv.state.create_count == 1  # recovered the existing task; no second create
    job = env.db.get_job(jid)
    assert job.task_id == next(iter(env.srv.state.tasks))  # adopted the task Meshy really created


def test_ambiguous_with_no_task_fails_without_resubmitting(env):
    env.srv.state.create_error = (502, "bad gateway")  # 5xx -> ambiguous, but no task exists
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: status(env, jid) == JobStatus.FAILED, timeout=20)
    finally:
        qm.stop()
    assert env.srv.state.create_count == 0
    assert "Not resubmitted" in env.db.get_job(jid).message
    assert qm.retry(jid).startswith("Re-queued")
    assert status(env, jid) == JobStatus.QUEUED  # must be approved again explicitly


def test_rate_limit_then_success(env):
    env.srv.state.rate_limit_creates = 1
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED, timeout=20)
    finally:
        qm.stop()
    assert env.srv.state.create_count == 1


def test_meshy_task_failure(env):
    env.srv.state.fail_tasks = True
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: status(env, jid) == JobStatus.FAILED)
    finally:
        qm.stop()
    job = env.db.get_job(jid)
    assert "too complex" in job.message and job.consumed_credits == 0
    assert any(k == "failed" for k, _, _ in env.events)


def test_restart_resumes_tracking(env):
    env.srv.state.polls_to_finish = 1000  # stays in progress
    qm = env.make_qm()
    qm.start()
    jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
    qm.approve()
    wait_for(lambda: status(env, jid) == JobStatus.IN_PROGRESS)
    qm.stop()
    task_id = env.db.get_job(jid).task_id
    # "Restart": brand-new manager with the same database.
    env.srv.state.tasks[task_id]["polls"] = 0
    env.srv.state.polls_to_finish = 2
    qm2 = env.make_qm()
    qm2.start()
    try:
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED)
    finally:
        qm2.stop()
    assert env.srv.state.create_count == 1 and env.db.get_job(jid).task_id == task_id


def test_restart_does_not_auto_submit_approved_jobs(env):
    qm = env.make_qm()
    jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
    qm.approve()  # approved but engine never ran
    qm2 = env.make_qm()
    qm2.start()
    try:
        import time
        time.sleep(0.5)
        assert qm2.paused and env.srv.state.create_count == 0
        qm2.resume()
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED)
    finally:
        qm2.stop()


def test_crash_during_submit_is_reconciled(env):
    qm = env.make_qm()
    jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
    qm.approve()
    # Simulate: request reached Meshy, then the app died before saving the task ID.
    import time
    client = MeshyClient("test-key", base_url=env.srv.base_url, min_interval=0)
    env.db.update_job(jid, status=JobStatus.SUBMITTING, submit_started_at=time.time())
    client.create_image_to_3d(GenerationSettings().to_payload("data:image/png;base64,AA"))
    qm2 = env.make_qm()
    qm2.start()
    try:
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED)
    finally:
        qm2.stop()
    assert env.srv.state.create_count == 1


def test_download_interruption_and_url_expiry(env):
    env.srv.state.truncate_once.add("model.fbx")
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: status(env, jid) == JobStatus.COMPLETED, timeout=20)
    finally:
        qm.stop()
    out = env.tmp / "Output" / "A" / "A.fbx"
    assert out.stat().st_size == env.srv.state.asset_size


def test_watcher_auto_submit_and_archive(env):
    from services.folder_watcher import FolderWatcher
    inbox = env.tmp / "Input"
    make_image(inbox / "existing.png", color=(1, 2, 3))
    qm = env.make_qm()
    qm.start()
    qm.set_watch_auto_submit(True)
    w = FolderWatcher(inbox, qm.handle_watched_file, interval=0.05, process_existing=False)
    w.start()
    try:
        make_image(inbox / "tree.png", color=(9, 9, 9))
        wait_for(lambda: (inbox / "Processed" / "tree.png").exists(), timeout=20)
        dup = inbox / "tree_copy.png"
        dup.write_bytes((inbox / "Processed" / "tree.png").read_bytes())
        wait_for(lambda: (inbox / "Skipped" / "tree_copy.png").exists())
    finally:
        w.stop()
        qm.stop()
    assert (inbox / "existing.png").exists()  # pre-existing image was NOT submitted
    assert env.srv.state.create_count == 1
    assert (env.tmp / "Game" / "Assets" / "Art" / "Models" / "Tree" / "Tree.fbx").exists()


def test_watcher_without_auto_submit_only_queues(env):
    qm = env.make_qm()
    res = qm.handle_watched_file(make_image(env.tmp / "Input" / "rock.png"))
    assert status(env, res.job_id) == JobStatus.QUEUED and env.srv.state.create_count == 0


def test_cancel_pending(env):
    env.srv.state.hold_pending = True
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: status(env, jid) == JobStatus.PENDING and env.db.get_job(jid).task_id)
        assert env.srv.state.balance == 985
        qm.cancel(jid)
        wait_for(lambda: status(env, jid) == JobStatus.CANCELED)
    finally:
        qm.stop()
    assert env.srv.state.balance == 1000  # Meshy refunds PENDING tasks


def test_invalid_key_pauses(env):
    env.state["key"] = "wrong"
    qm = env.make_qm()
    qm.start()
    try:
        jid = qm.add_images([make_image(env.tmp / "a.png")])[0].job_id
        qm.approve()
        wait_for(lambda: qm.balance_error is not None)
    finally:
        qm.stop()
    assert env.srv.state.create_count == 0 and status(env, jid) == JobStatus.APPROVED
