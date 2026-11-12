# Meshy Automation Studio

A Windows desktop app that takes reference images and produces Unity-ready 3D models through the official Meshy Image-to-3D API. It submits the images, tracks each generation, downloads the models and textures, and files them into your Unity project. It does this without browser automation and without spending credits behind your back.

```
Image(s) ──► validate ──► confirm cost ──► Meshy Image-to-3D ──► poll status ──► download FBX + textures
                                                                                        │
              YourProject/Assets/Art/Models/Panda/Panda.fbx  ◄── atomic copy ◄─────────┘
                                                 /Textures/Panda_BaseColor.png
```

---

## 1. Setup (Windows 11)

1. **Python.** Either option works:
   - **uv**, if installed: `setup.bat` uses it automatically and downloads a private Python 3.12.
   - **Python 3.11+** from python.org, with "Add python.exe to PATH" ticked during install.
2. Double-click **`setup.bat`**. It creates `.venv` and installs the dependencies (PySide6, requests, keyring, Pillow, pytest).
3. Double-click **`run.bat`** to start the app.
   To see log output in a console instead, run `.venv\Scripts\python app.py`.
4. In **Settings → Meshy API key**, paste your key (from meshy.ai → API, which needs a paid plan) and click **Save key**.
   The app then runs a connection test. The test is a free balance check, so it uses no credits.
5. In **Settings → Unity**, pick your Unity project folder (the one that contains `Assets/` and `ProjectSettings/`). Click **Save settings**.

## 2. Daily use

### Manual: drag and drop
1. Drop images (or a folder of images) onto the dashboard, or click **Browse images…**.
2. A confirmation dialog shows:
   - the number of images
   - the settings that will be used
   - the **estimated cost** (from Meshy's published pricing)
   - your current balance
   - the batch budget
3. Click **Yes**. The jobs then run on their own. Each one goes through:
   *Waiting to submit → Pending on Meshy → Generating % → Downloading → Completed*.
4. When a model is ready you get a Windows notification. The files are already in Unity, so switch back to the editor and it imports them.

If you click **No**, the images stay in the queue as *Queued*. Select them later and click **Submit queued…**.

### Automatic: folder monitoring
Default folders:
- Input: `Documents\MeshyAutomation\Input`
- Output: `Documents\MeshyAutomation\Output`

You can change both in Settings.

| Option (Dashboard → Folder monitoring) | Effect |
|---|---|
| **Monitor input folder** | New images dropped into Input are detected once they have finished copying. Without auto-submit they only go into the queue; nothing is spent. |
| **Auto-submit detected images** | Detected images are submitted without asking. Turning this on shows a confirmation with the per-image cost. Each monitoring session gets its own budget (*Max credits per batch*). |
| **Include images already in the folder** | When monitoring starts, images already sitting in Input are processed as well. Off by default, so existing files are ignored. |

After processing, source images are moved to:
- `Input\Processed` when the model was generated
- `Input\Failed` when generation failed or the file is not a valid image
- `Input\Skipped` when the image is a duplicate

Monitoring and auto-submit are **always off when the app starts**. You must turn them on yourself each session.

### Dashboard buttons
| Button | What it does |
|---|---|
| Submit queued… | Confirms and submits the queued jobs you selected (or all of them if none are selected). |
| Pause / Resume queue | Pausing stops *new* submissions only. Tasks already running are still tracked and downloaded. |
| Retry | For a failed generation, puts the job back in the queue. You confirm again, because a retry costs credits. For a *Download failed* job, only the files are downloaded again, which is free. |
| Cancel | Cancels a job that has not been submitted yet, or a Meshy task that is still *Pending* (Meshy refunds those). A task that is already generating cannot be cancelled. |
| Remove | Hides finished, failed or unsubmitted jobs from the list. Files are kept, and task IDs stay in the database. |
| Open files | Opens the job's Unity folder, or its Output folder. Double-clicking a row does the same. |

## 3. Settings reference

### Generation
All values were checked against docs.meshy.ai on 2026-11-12.

| Setting | Default | API parameter / notes |
|---|---|---|
| Model type | Smart Topology | `model_type: "smart-topology"`: a clean, game-ready triangle mesh. *Standard* is the high-detail mesh. |
| AI model | meshy-t2.5 | Smart Topology accepts `meshy-t2.5` or `meshy-t2`. `meshy-t2` is cleaner and produces natively separated parts. Standard accepts `latest` (Meshy 7.1), `meshy-7.1`, `meshy-6` or `meshy-6-lite`. |
| Target polycount | 10,000 | `target_polycount`. Allowed ranges: t2.5 is 100–30,000, t2 is 100–15,000, Standard with remesh is 100–300,000. |
| Remesh / Topology | – | Standard only (`should_remesh`, `topology`). Hidden for Smart Topology, because Meshy ignores them there. |
| Generate textures | on | `should_texture` |
| Texture resolution | 2K | `texture_resolution`: `2k`, `4k` or `8k`. Meshy 6 Lite supports 2K only. |
| PBR maps | off | `enable_pbr`: adds metallic, roughness and normal maps. |
| Texture prompt | – | `texture_prompt`, up to 800 characters. |
| Export formats | FBX | `target_formats`. Unity uses FBX, OBJ (with its MTL) or GLB. Other formats are saved to Output only. |
| Pose / moderation / auto-size | off | `pose_mode`, `moderation`, `auto_size` + `origin_at` |

Retired or deprecated identifiers are deliberately not offered:
- `lowpoly`
- `meshy-t1`
- `meshy-5`
- `meshy-7` as the geometry model

**Estimated credits.** These come from Meshy's published pricing table:

| Model | Mesh only | 2K textures | 4K textures | 8K textures |
|---|---|---|---|---|
| meshy-t2 / meshy-t2.5 | 5 | 15 | 15 | 20 |
| meshy-7.1 / latest / meshy-6 | 20 | 30 | 30 | 35 |
| meshy-6-lite | 5 | 15 | – | – |

The *actual* charge is Meshy's `consumed_credits`, shown in the **Used** column when a job finishes. Failed tasks are refunded by Meshy, so they show 0.

### Credits and queue
- **Max credits per batch** (default 100; 0 means no limit). Applies to each confirmed manual batch and to each auto-submit monitoring session. A job that would push the batch over budget is not submitted; it stays *Queued* with an explanation.
- **Simultaneous generations** (default 1).

### Unity
- **Unity project.** If empty, models are only downloaded to Output.
- **Destination.** Default `Assets/Art/Models`. Must be inside `Assets`.
- **Own folder per model** (on by default). Produces `Models/Panda/Panda.fbx` with a `Textures/` subfolder. If that name is taken, the folder becomes `Panda_2`.

Files are first assembled in a hidden `.Panda.incoming` folder, which Unity ignores, and then renamed into place. Unity therefore never imports a half-copied model. The app does not touch scenes, prefabs or `.meta` files.

### Environment variables
None of these are needed for normal use. `demo_mode.bat` sets all three.

| Variable | Effect |
|---|---|
| `MESHY_API_KEY` | Used instead of the key in Credential Manager. |
| `MESHY_STUDIO_HOME` | Changes the data folder (settings, database, logs). The default is `%LOCALAPPDATA%\MeshyAutomationStudio`. |
| `MESHY_API_BASE` | Changes the API address. The default is `https://api.meshy.ai`. Use this only to point the app at the local fake server. |

## 4. Credit safety: how it is enforced

- **Nothing is submitted without your approval.** Two things count as approval: the confirmation dialog, or turning on auto-submit (which has its own confirmation).
- **Startup never spends credits.** If approved jobs were still waiting when the app closed, the queue starts **paused** and asks you to click Resume.
- **The balance is checked before every submission** (the balance call is free). If the balance is lower than the estimate, the queue pauses.
- **Duplicates are blocked.** Images are identified by a SHA-256 of their content, so the same image under a different name is still a duplicate.
- **A job cannot be submitted twice.** Its state changes from *approved* to *submitting* in one atomic database update.
- **Timeouts are never resubmitted blindly.** If the create request times out after it was sent, or Meshy returns 5xx, the job becomes *Confirming with Meshy*. The app then lists your recent Meshy tasks and adopts the one that was created.
  - If no task is found after several checks, the job fails with "click Retry". It is never resubmitted automatically.
  - Only errors that happen *before* the request leaves your PC are retried automatically: connection refused, DNS failure, connect timeout.
- **Rate limits are respected.** On HTTP 429 the app waits for the time in `Retry-After`. Polling follows the `Retry-After` header Meshy sends for running tasks (5 seconds if the header is missing). Requests are throttled to at most 10 per second, well under Meshy's limit of 20.

## 5. Reliability

- **Persistent state.** Jobs and an append-only list of every Meshy task ID are stored in SQLite: `%LOCALAPPDATA%\MeshyAutomationStudio\jobs.db`.
- **Restarts.** On restart, tasks that are still running are tracked again and downloaded.
  - A crash in the middle of a submission is reconciled: the app checks whether Meshy created the task.
  - Meshy keeps generated files for **3 days**, so reopen the app within that time.
- **Interrupted downloads.** Downloads go to `.part` files and resume with HTTP Range requests.
  - Expired signed URLs are refreshed by fetching the task again.
  - Downloads retry with backoff (6 attempts), then show *Download failed*. Retry downloads again without generating again.
- **Network trouble while polling** never marks a task as failed. Only a Meshy `FAILED` status does, and the error code is shown.
- **Logs.** Written to `%LOCALAPPDATA%\MeshyAutomationStudio\logs\` and shown on the Log tab. API keys, `Bearer` headers, image base64 data and signed URL query strings are all redacted.
- **API key storage.** The key is stored only in **Windows Credential Manager**, under the generic credential *MeshyAutomationStudio*. It is never written to `settings.json`, the database or the logs.

## 6. Output layout

```
Documents\MeshyAutomation\Output\Panda\      (complete download, kept outside Unity)
    Panda.fbx  Panda_Preview.png  meshy_task.json
    Textures\Panda_BaseColor.png  [Panda_Metallic/Roughness/Normal.png with PBR]

YourProject\Assets\Art\Models\Panda\         (what Unity imports)
    Panda.fbx
    Textures\Panda_BaseColor.png ...
```

File names come from the image name. For example, `oak tree_01.png` becomes `OakTree01`.

## 7. Testing

```
run_tests.bat            (or: .venv\Scripts\python -m pytest -q)
```

There are 45 tests, and they all run against a **local fake Meshy server** (`tests/fake_meshy.py`), so they cost no credits. They cover:
- image validation
- payloads and pricing
- authentication (401)
- task creation, Retry-After and 429 handling
- ambiguous timeouts and their reconciliation
- 402 handling and the budget limit
- duplicate blocking
- task failure
- restart and crash recovery
- interrupted and resumed downloads
- expired URLs
- Unity organisation
- the folder watcher with auto-submit
- cancel and refund
- log redaction
- an offscreen GUI end-to-end run

**Demo mode.** Run `demo_mode.bat` to try the full UI against the fake server, with no real API and no credits. It keeps its data in `demo_data\`.

**First real test.** Start with a single image and keep the default settings. That costs about 15 credits.

## 8. Limitations and notes

- **Meshy has no free test mode any more.** The shared test key was retired in September 2026. The only free real-API check is the balance call, which *Test connection* uses. Real generations always cost credits.
- **Cost estimates** come from the published pricing table (default texture model for each AI model). Whether options such as PBR change the price is not documented, so the **Used** column, filled from Meshy's `consumed_credits`, is the authoritative cost.
- **Textures in FBX.** Meshy returns the model file and texture maps as separate downloads. The textures are placed in `Textures/` with clear names.
  - If the FBX embeds or references its own texture names, Unity may not link the separate maps automatically. In that case, assign `*_BaseColor.png` (and the PBR maps) to the material, or use Model Import Settings → Materials → *Extract Textures*.
  - For OBJ files, the `.mtl` is rewritten to point at the renamed base-color texture.
- **GLB in Unity** needs the glTFast package. FBX works out of the box.
- **Confirming with Meshy** (after an ambiguous timeout) matches tasks by creation time. If tasks are being created from another tool on the same account at the same moment, the match could be wrong. The job message says so if there was more than one candidate.
- **Folder monitoring** checks the folder every 2 seconds and is not recursive. Subfolders such as `Processed` are ignored.
- **Notifications** use the Windows tray. Focus Assist or Do Not Disturb can hide them.
- **Meshy 3-day retention.** If the app is closed for more than 3 days while a task is unfinished or not yet downloaded, Meshy deletes the files, and the job is marked failed with that explanation.

## 9. Project structure

```
app.py                       entry point + AppContext wiring
config.py                    settings.json (no secrets), Credential Manager key store
models/generation.py         verified Meshy parameters, validation, payload, pricing
models/job.py                job states
database/db.py               SQLite (jobs, batches, append-only task IDs)
services/meshy_client.py     REST client, error classification (safe vs ambiguous)
services/queue_manager.py    engine: approve → submit → poll → reconcile → download → Unity
services/download_manager.py resumable downloads, URL refresh, file naming
services/unity_manager.py    atomic copy into Assets/
services/folder_watcher.py   polling watcher with file-stability check
ui/                          PySide6 dashboard, settings, log, tray notifications
utils/                       image validation, naming, redacting logger
tests/                       pytest suite + fake Meshy server
```
