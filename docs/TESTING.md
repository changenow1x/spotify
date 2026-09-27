# Testing

What is tested, how to run it, and — just as importantly — what is not
covered.

---

## Running the tests

```bat
python -m app test
```

or

```bash
.venv/bin/python -m pytest tests/ -q
```

Useful variations:

```bash
.venv/bin/python -m pytest tests/ -v                 # per-test names
.venv/bin/python -m pytest tests/ -k publish         # by keyword
.venv/bin/python -m pytest tests/test_e2e_workflow.py  # one file
.venv/bin/python -m pytest tests/ --coverage         # with coverage
```

The full suite takes about sixteen minutes. The slow part is the end-to-end
tests, which drive a real worker against a real HTTP server.

---

## What exists

Counts below are what `pytest --collect-only -q` actually reports — the
number of collected test items, not the number of `def test_` lines. Several
files use `parametrize`, so the two differ.

| File | Collected | Covers |
|---|---|---|
| `test_state_machine.py` | 71 | Every legal and illegal transition |
| `test_spotify_adapter.py` | 71 | The publisher against the simulated site |
| `test_media.py` | 69 | Probing, validation, truncation, spec limits |
| `test_windows.py` | 66 | Windows file handling, locks, cross-directory |
| `test_database.py` | 59 | Schema, transitions, claiming, concurrency |
| `test_watcher.py` | 55 | Stability, pairing, restarts, temporary files |
| `test_metadata_parser.py` | 52 | The `.txt` format, rejections |
| `test_api.py` | 52 | Every dashboard endpoint, auth, path safety, XSS |
| `test_cli.py` | 51 | Every command as a subprocess, plus first-run setup |
| `test_chapters.py` | 48 | Every chapter rule from Spotify's documentation |
| `test_windows_launchers.py` | 40 | Static validation of the `.bat` scripts |
| `test_security_paths.py` | 30 | Traversal, reserved names, long paths |
| `test_e2e_recovery.py` | 30 | Failure, crash, retry, duplicates, concurrency |
| `test_security_redaction.py` | 26 | Secrets never reach a log, a page or a diagnostic |
| `test_release_matrix.py` | 21 | The twenty-case failure matrix, A to T |
| `test_idempotency.py` | 20 | Duplicate cases A-K, end to end |
| `test_archive.py` | 20 | Move semantics, failure, never lose a file |
| `test_performance.py` | 19 | Large files, memory, throughput |
| `test_documentation.py` | 16 | The docs describe the app that exists |
| `test_api_retry_guard.py` | 7 | A finished episode cannot be re-queued |
| `test_e2e_workflow.py` | 11 | The happy paths, folder to archived |
| `test_release_recovery.py` | 6 | Hard kill, reopen, claim release, continuation |
| `test_release_workflow.py` | 5 | The exact folder workflow, and a restart after it |
| `test_config.py` | 3 | Environment overrides |
| **Total** | **848** | |

Regenerate this table with:

```bash
.venv/bin/python -m pytest tests/ --collect-only -q
for f in tests/test_*.py; do
  echo "$(.venv/bin/python -m pytest "$f" --collect-only -q | grep -c '::') $f"
done
```

---

## The three levels

It matters which of these a claim belongs to, so they are kept apart
throughout.

### 1. Real code, real files — fully covered

The watcher, the metadata parser, the media validator, the database, the
state machine, the worker, the archive manager, the API and the CLI all run
for real in the tests.

The end-to-end tests are not mocked at the application boundary. They write
real MP3 and MP4 files into a real folder, let the real `FilesystemWatcher`
find them, and follow them to `ARCHIVED`. Only the Spotify **website** is
substituted.

### 2. Simulated Spotify — covered in logic, not in appearance

`tests/mock_spotify/server.py` is a local HTTP server that serves pages shaped
like Spotify for Creators: a login page, an upload form with progress, the
episode form, a publish action, and an episode list.

It is deliberately hostile where the real site is awkward, because that is
where bugs live:

- uploads can be made to fail, to be slow, or to return a response that is
  lost after the upload actually succeeded;
- the server can reject a file, or report it as still processing forever;
- episodes can pre-exist, so reconciliation has something to find;
- **drafts and form errors are scoped per session**, so two workers using
  two sessions cannot corrupt each other's state — a real bug found this way.

This proves the logic end to end. It does **not** prove the real site behaves
the same way.

### 3. Real Spotify — not yet tested

**The browser has never been run against the live Spotify for Creators
site.** The Chromium download is blocked in the build environment, so no
browser could be launched.

That means these are written from Spotify's current documentation and from
the shape of the simulated pages, and are **unconfirmed against the real
interface**:

- the selector list in `app/publishers/spotify/selectors.py`;
- the upload step;
- the episode form field mapping;
- the publish and schedule controls.

Everything around them — auth handling, timeouts, unknown states,
reconciliation, retry, archive, recovery, the dashboard — is fully covered.

**Do not treat this project as verified against real Spotify until
`CHECK_SPOTIFY.bat` has been run successfully on a real account.**

---

## The integration matrix

`test_e2e_workflow.py` and `test_e2e_recovery.py` cover the acceptance cases.
Each one drops real files and asserts the real end state.

| Case | Where | What it proves |
|---|---|---|
| Video episode | workflow | MP4 publishes and archives |
| Audio episode | workflow | MP3 publishes and archives |
| Metadata first | workflow | Media arriving second is picked up |
| Media first | workflow | The `.txt` is waited for, then picked up |
| Slow copy | recovery | A file still growing is not touched |
| Truncated file | recovery | A cut-off file is refused, with a reason |
| Duplicate re-ingest | recovery | The same bytes twice → one episode |
| Renamed file | recovery | Identity survives a rename |
| Crash during upload | recovery | Restart adopts, does not republish |
| Network failure | recovery | Retry with backoff, then success |
| Uploaded but not published | recovery | `UPLOADING` is not `PUBLISHED` |
| Remote rejection | recovery | A rejected file fails visibly and retries |
| Cancelled job | recovery | Cancellation is honoured at every stage |
| Retired job | recovery | A retired show is not published |
| Several jobs | recovery | All publish, none is lost |
| Two workers | recovery | Concurrent publication does not collide |
| Scheduled episode | workflow | A future date is scheduled, not published now |
| Chapters | workflow | Timestamps are placed in the description |
| Publish failure | recovery | Publication is idempotent across retries |

---

## Bugs these tests actually found

Recorded because a test that has never failed is not evidence of much.

| Found by | Bug |
|---|---|
| Workflow | Jobs stranded in `DISCOVERED` — `claim_next_job()` did not select them |
| Workflow | MP4 misclassified — the `ftyp` parser was not bounded |
| Workflow | The watcher swept but never emitted: watches were never registered |
| Workflow | Upload completion was being treated as publication |
| Recovery | Byte-identical fixtures collapsed under content hashing — correct behaviour, wrong fixture |
| Recovery | The simulated site used one global draft, so two sessions clobbered each other |
| Recovery | A `NameError` in the simulated server hid a rejection test behind retries |
| Recovery | A 24-byte MP3 fragment validated as a valid 0.0015-second episode |
| Recovery | **Two workers shared one browser session**, producing a duplicate title and a job needing reconciliation |
| Performance | `make_mp3()` discarded all but one frame — every MP3 fixture was 877 bytes regardless of the requested duration |
| Windows | `open-url` accepted `https://open.spotify.com/\nhttps://elsewhere.example` |
| Windows | `make-test-media --name "../../escape"` wrote outside the target folder |
| CLI | `make-test-media` never wrote a `.txt` at all — `None if x else None` |
| Performance | A truncated CBR MP3 reported its full duration; only the byte count reveals it |
| API | `open-folder` accepted a file and would have opened it in its default program |

---

## Writing a new test

Use the harness — it wires the real service to the simulated site:

```python
from app.core.database import JobState


def test_my_case(harness):
    # Drop real files into a real folder.
    harness.drop_pair("My Episode")

    # Let the real watcher find them.
    job = harness.discover()[0]

    # Run the real worker.
    harness.drain()

    # Assert the real end state.
    assert harness.db.get_job(job.id).state_enum() == JobState.ARCHIVED
```

`harness.drop_media()` writes a byte-distinct file every time. **This
matters:** the app identifies episodes by content hash, so two identical
fixtures are correctly deduplicated, and a test that expects two episodes
will fail for the right reason and look like a bug.

Harness helpers (`tests/harness.py`):

| Helper | Does |
|---|---|
| `drop_media(name, kind=..., seconds=...)` | A byte-distinct MP3 or MP4 |
| `drop_metadata(name, **fields)` | A valid `.txt` |
| `drop_pair(name, **fields)` | Both |
| `drop(name, content)` | Any file at all |
| `discover(timeout=..., expect=N)` | Runs the real watcher until the folder settles |
| `discover_one(name)` | One pair, one job |
| `drain(limit=...)` | Runs the worker until the queue is empty |
| `force_state(id, state, message=...)` | Drives a job into a state directly |
| `settle(seconds=None)` | Waits out the stability window |
| `states_of(job_id)` | The job's state history |
| `episode_titles()` | Titles on the simulated site |
| `events(kind=...)` | Watcher events |
| `show` / `incoming` / `media_files()` | Folders and files |
| `adapter` / `service` / `db` / `config` | The real components |

Failure injection on the simulated site is through its `state` object
(`tests/mock_spotify/server.py`):

| Field | Effect |
|---|---|
| `spotify.state.fail_upload = True` | The next upload fails |
| `spotify.state.form_error = "..."` | The publish click raises this banner |
| `spotify.state.upload_delay` | Makes the upload slow |
| `spotify.state.processing_delay` | Spotify "takes a long time" |
| `spotify.state.signed_in = False` | The session is not signed in |
| `spotify.state.requires_verification = True` | Spotify demands verification |
| `spotify.seed_episode(title, state=...)` | Pre-create a remote episode |
| `spotify.episodes()` | What the remote site currently holds |
| `spotify.reset()` | Clear episodes, drafts and errors |

There is no `fail_next_upload()` or `set_reject()` — those names were
illustrative. The fields above are the real ones.

---

## Before you trust a change

```bash
.venv/bin/python -m pytest tests/ -q
```

All 848 must pass. If you changed a fixture generator, the media validator or
the watcher, the end-to-end tests are the ones that will notice.

---

## Before you trust it against real Spotify

```bat
CHECK_SPOTIFY.bat
```

Read-only. It opens the real site with the saved session and reports which of
the 37 registered controls it can find.

If something is missing, that is a selector problem, not a logic problem.
The app is built so that a missing selector produces a **diagnostic with a
screenshot**, never a silent success — so a failure here tells you exactly
which part of the page changed. See
[SPOTIFY_INTEGRATION.md](SPOTIFY_INTEGRATION.md) for how to correct it.
