# Architecture

How the app is built, and why it is built that way.

About 12,700 lines of application code, no framework, no ORM, no message
broker. Python's standard library plus four small dependencies.

---

## The shape of it

```
  incoming\                     the only folder the user writes to
      |
      v
  FilesystemWatcher             waits for files to stop changing
      |                         pairs media with its .txt
      v
  Database                      one row per episode, one per state change
      |
      v
  WorkerPool -> JobWorker       one thread per concurrent job
      |
      |   validate  ->  auth  ->  upload  ->  process  ->  fill  ->  publish
      v
  SpotifyPublisher              drives the real browser
      |
      v
  ArchiveManager                moves the files, only after confirmation
```

The watcher and the workers are **separate threads with a database between
them**. That is the central design decision, and everything else follows from
it.

---

## Why the database sits in the middle

The alternative — watcher calls worker directly — is simpler and breaks in
three specific ways:

1. **A crash loses the job.** With a queue row, a job that was in flight when
   the process died is still there, in a state the app knows how to recover.
2. **Two workers fight over one file.** A claim query makes that impossible.
3. **The dashboard cannot show real state.** It reads the same rows the
   workers write, so what it shows is what is true.

SQLite in WAL mode, one connection per thread, every state change in one
transaction with its event row. The database is a few megabytes after years
of use.

### The job row

```sql
CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    show_id          TEXT NOT NULL,
    state            TEXT NOT NULL,
    job_type         TEXT NOT NULL,   -- 'paired' | 'media_only'
    media_path       TEXT,
    metadata_path    TEXT,
    media_hash       TEXT,            -- durable identity
    content_hash     TEXT,            -- durable identity
    remote_episode_id TEXT,           -- what Spotify calls it
    remote_title     TEXT,
    attempt_count    INTEGER DEFAULT 0,
    status_message   TEXT,
    last_error       TEXT,
    next_attempt_at  TEXT,
    published_at     TEXT,
    archived_media_path TEXT,
    ...
)
```

`media_hash` and `content_hash` are the **durable identity** — not the file
name. `remote_episode_id` is the **remote identity** — what Spotify calls the
episode. Both are written at the moment they become known and are never
guessed.

---

## The state machine

Every state change goes through `app/core/state_machine.py`. The legal
transitions are declared in one table, and an illegal move raises rather than
being written.

```
DISCOVERED → QUEUED → VALIDATING → VALIDATED → AUTHENTICATING
           → UPLOADING → PROCESSING → READY_TO_PUBLISH → PUBLISHING
           → PUBLISHED → ARCHIVING → ARCHIVED
```

with exits to `FAILED`, `RETRY_WAIT`, `CANCELLED`, `NEEDS_USER_ACTION` and
`NEEDS_RECONCILIATION` from wherever they make sense.

Two properties matter:

- **`UPLOADING` is not `PUBLISHED`.** The file reaching Spotify means
  nothing. Only a confirmed episode is published.
- **`PUBLISHED` is terminal for publication.** From there the only forward
  move is archiving. An archive failure goes back to `PUBLISHED` with an
  error — it never re-enters `PUBLISHING`, because that is how you publish
  twice.

Transitions that recovery needs to make — re-opening a job that the database
says is in a state the code cannot legally enter — go through
`Database.force_state()`, which is used **only** by recovery code and is
covered by its own tests.

---

## The watcher

`app/watcher/filesystem_watcher.py`

Uses `watchdog` where available and falls back to polling. The polling path is
not a second-class citizen: it is the one the tests drive, so the production
behaviour is the tested behaviour.

Three responsibilities, in order:

**1. Stability.** `stability.py` tracks each file's size and modification
time. A file is offered to the rest of the system only when both have been
unchanged for `stable_seconds`, confirmed `stable_checks` times. Temporary
extensions (`.part`, `.crdownload`, `.partial`, `.tmp`, …) are ignored
entirely, so a download in progress is never picked up.

**2. Pairing.** `pairing.py` matches a media file to its `.txt` by base name,
case-insensitively. Two `.txt` files matching one media file is **ambiguous,
and produces no match plus a message** — the app never guesses which one you
meant. A media file with no `.txt` waits, and the status says so, until
`media.allow_media_only` allows it through.

**3. Emission.** Only new pairs are emitted; a file already emitted is not
emitted again. This is what makes a restart harmless.

---

## Media handling

`app/media/`

`inspector.py` asks FFmpeg first and falls back to `builtin_probe.py`, a
dependency-free reader that parses MP3 (including Xing/Info and VBR headers),
WAV, MP4 and MOV boxes directly.

This matters more than it looks. FFmpeg is a large optional dependency that
many users will not have, and an app that only works with it installed is not
"works on Windows". The built-in reader is what the test suite exercises, so
it is real code, not a stub.

`validator.py` separates three outcomes, and the distinction is taken from
Spotify's own wording — **recommended** is not the same as **supported**:

- **error** — something Spotify documents as unsupported or requires. The job
  stops and the message names the problem.
- **warning** — something Spotify lists as *recommended* but still accepts. The
  episode publishes, and the deviation is reported on the job.
- **information** — nothing to say.

A recommendation must never become a refusal. Spotify lists 16:9 as the
recommended aspect ratio and *any* aspect ratio as compatible, so a
square video produces a warning and publishes, letterboxed if need be. It
lists any audio bitrate as acceptable, so 96 kbps is a warning, not a
rejection. It lists five frame rates as recommended and does not exclude the
others, so 15 fps is a warning.

### What the validator enforces {#media}

Transcribed from Spotify's current documentation and re-checked at each
release. "Hard" means Spotify documents it as unsupported; the rest are
recommendations.

| | Value | Verdict |
|---|---|---|
| **Video container** | MP4, MOV, MPG | anything else is a hard error |
| **Audio container** | MP3, M4A, WAV | anything else (AIFF included) is a hard error |
| **Duration** | up to 12 h; under 4 h recommended | over 12 h is a hard error |
| **File size** | up to 60 GB; under 10 GB recommended | over 60 GB is a hard error |
| **Video codec** | H.264 / H.265 recommended | anything else warns |
| **Audio codec** | AAC-LC / PCM / FLAC recommended | anything else warns |
| **Aspect ratio** | 16:9 recommended, any accepted | 16:9 preferred, never required |
| **Frame rate** | 24/25/30/50/60 recommended, native rate | anything else warns |
| **Audio bitrate** | 128 kbps recommended; 192 kbps for a video's audio track | below warns |
| **Audio channels** | mono or stereo | more than two is a hard error |
| **Tracks** | one video, one audio, equal duration | more than one of either, or a length mismatch, is a hard error |
| **Resolution** | 1080p recommended, 180p acceptable | below 720p warns |
| **Scan** | progressive | interlaced warns |

Two checks exist purely to stop a half-copied file being published:

- `duration_implausibly_short` — under one second is always a fragment.
- `media_appears_truncated` — a constant-bitrate file states its full length
  in the header, so a cut-off file still claims its original duration. Only
  comparing the bytes on disk against the bytes the declared bitrate implies
  catches it.

Neither guesses. A file that trips them is parked with an explanation.

---

## The publisher

`app/publishers/`

`base.py` defines `PublisherAdapter`. Everything above it — the worker, the
database, the dashboard — knows only that interface. The Spotify
implementation is reached through a registry, so a future platform is a new
adapter and nothing else.

That registry also records the honest answer for the others:

| Adapter | Status |
|---|---|
| `spotify` | **implemented** — the only platform in this release |
| `apple_podcasts` | future adapter, not built |
| `amazon_music` | future adapter, not built |
| `youtube` | future adapter, not built |

**Spotify is the only platform in v1.** The others are listed so the boundary
is visible, not because they are half-built. There is no code path that
publishes anywhere else.

### What RSS does and does not do

Spotify publishes no API for uploading episodes. Externally hosted shows reach
Spotify through an **RSS feed**, and Spotify's own documentation is explicit
that a show hosted with Spotify is made available on Spotify automatically,
while every *other* platform is a separate submission **by you**:

> To reach platforms beyond Spotify — like Apple Podcasts or Amazon Music —
> you need to submit your show to each service yourself. You'll need your RSS
> feed, found in Settings → Availability.

That is two separate things, and conflating them would be a lie:

- **RSS is not an upload API.** Nothing in this project feeds an episode into
  Spotify; the file goes through Spotify for Creators' own web uploader, which
  is why a browser is involved at all.
- **RSS is not automatic first-time submission elsewhere.** Once you have
  submitted your show to another service, new episodes usually appear there
  within a few hours. The first submission is a manual step on that service,
  done once, and this project does not and will not do it for you.

So a second platform would not be "add an RSS feed here". It would be a new
adapter for that service's own interface, and it is only worth building after
someone has verified that interface behaves as documented — which is exactly
why none of them are in this release.

### Browser sessions are per thread

`SpotifyPublisher` keeps its sessions in a `threading.local()` registry, not
one shared session. Two workers publishing at once would otherwise drive a
single page concurrently, which produces duplicated entries and confused
form state — a real bug found by the two-worker test.

---

## Idempotency and recovery {#idempotency-and-recovery}

The rule the whole design serves: **never publish the same episode twice.**

Three mechanisms:

**1. Durable identity.** Content hash, not file name. The same bytes are the
same episode whatever the file is called.

**2. Remote identity.** Once Spotify assigns an episode an id, it is stored.
Re-running picks it up instead of creating a second one.

**3. Reconciliation.** When the app does not know whether Spotify already has
the episode, the state is `NEEDS_RECONCILIATION` — explicit, not optimistic.
`reconciliation.py` then reads the real episode list and matches on title,
season and episode number. A match is adopted; a miss is published once.

**4. Pre-publish check for every job.** Even a job that has never been sent
is compared against the show first. A file the user just dropped in may be a
re-ingest of something already published — by an older install, a previous
retry, or by hand. The only way to know is to look. This costs one page read
per job and buys the guarantee that re-dropping a file does not create a
second copy of an episode.

The match has to be *decisive*, and what counts as decisive depends on how
much the app knows:

| Evidence available | Adopted on | A bare title match |
|---|---|---|
| A Spotify episode id already recorded for **this** job | the id | — |
| The remote episode id is recorded against a **different** local job | never — that episode is provably another file's | published, with a warning |
| The job has reached Spotify (upload started, an advanced state, a retry) | title, with season and episode number checked whenever both sides have them | adopted — the job demonstrably sent something |
| The job has **never** reached Spotify | title **and** season **and** episode number | published, with a warning |

The third row is the lost-response case: the bytes went out, the reply did not
come back, and the only evidence is the title. Adopting is right there, and
the fourth row is a file the user has just put in the folder, where a title
alone proves nothing.

The second row is the rule that was missing, and its absence cost a recording.
Two different files shared a title and both were interrupted mid-upload. The
first was retried and published; the second then reconciled against the
episode the *first* had just created, adopted it, and was archived as though
it had been published — so the second file was gone. `reconciliation.py` now
asks, before accepting any title match, whether another local job already
records that episode id. If one does, this file is not that episode, and it
publishes instead.

### What reconciliation decides

| Situation | Outcome |
|---|---|
| This job's episode id is already recorded | **ADOPT** — finish and archive |
| Another job owns that remote episode id | **PUBLISH** this file, and record a warning naming the clash |
| Decisive match, nothing contradicts it | **ADOPT** |
| Title matches, numbering is missing or different, job never sent | **PUBLISH**, and record a warning the user can act on |
| The episode list cannot be read, and this job has reached Spotify before | **FLAG NEEDS_RECONCILIATION** and wait — publishing could duplicate |
| The episode list cannot be read, and this job is new | **PUBLISH** — a first publish has nothing to duplicate |
| Spotify asks for a sign-in, a CAPTCHA or a two-factor code | **FLAG NEEDS_USER_ACTION** and stop; never automated |
| A network or browser error | **RETRY** with bounded backoff |
| The retries are exhausted | park in **FAILED** with the last error, and offer retry / cancel / move-to-failed |

In no case is a newly created episode dropped because another episode has a
similar title. The worst outcome is a visible duplicate the user can delete in
Spotify's own dashboard; the best is a silent, correct publication.

A title on its own is ambiguous. `Season 2, Episode 7` on Spotify might be the
recording in the folder, or a different episode that happens to share a title.
Adopting it would silently discard the file the user just put there and watch
it vanish into the archive — losing a recording is the worse failure. So the
episode is published as its own episode, the user gets a visible warning event
saying `Spotify has an episode titled 'X', but the season or episode number
does not match, so this is treated as a different recording`, and the
duplicate is right there to be deleted in the Spotify dashboard. A number
that matches exactly is proof enough, and is adopted silently.

`recover_interrupted_jobs()` runs at startup and finds every job left in a
non-terminal state, moves it to the safe state, stamps `publish_attempted_at`
on the ones that had reached Spotify, and lets reconciliation decide.

### Retries

Bounded exponential backoff: `base × 2^(attempt-1)`, capped at
`max_delay_seconds`, with jitter so a batch does not stampede. The number of
attempts is finite, after which the job is parked with the last error. Every
failure offers the same three things: **Retry now**, **Cancel**, **Move to
failed**.

---

## The API and dashboard

`app/api/` is a small FastAPI application bound to `127.0.0.1`.

Mutating endpoints require the local token. Read endpoints do not, because the
dashboard is a single-user local page and requiring a token on every poll
would break it for no gain.

`app/dashboard/app.js` is plain JavaScript, no build step, no framework. It
renders with `textContent` exclusively — there is no `innerHTML` assignment
anywhere, so stored text (a file name, an error from Spotify) can never become
markup. There is a test that fails if that ever changes.

The dashboard reads the same database rows the workers write, so "what the
dashboard shows" and "what is happening" cannot drift apart.

---

## Logging and diagnostics

`app/core/logging_setup.py` writes rotating files. `app/security/redaction.py`
scrubs anything that looks like a token, cookie, auth header or URL
credential — on the way to the log and to the dashboard, not only on the way
to disk.

When a browser step fails, `diagnostics/` gets a screenshot and a structured
report: which stage, which selectors were tried, what the page contained. UI
uncertainty produces a diagnostic, never a silent success.

---

## Dependencies

| Package | Why |
|---|---|
| `pydantic` | Config validation with real error messages |
| `PyYAML` | The config file |
| `watchdog` | Native folder watching on Windows |
| `playwright` | The browser |
| `fastapi` + `uvicorn` | The local dashboard |

FFmpeg is optional and discovered at runtime. Everything else is standard
library.

---

## Testing strategy

Unit tests for the parser, validator, state machine and paths. Integration
tests that use the **real** components — real watcher, real database, real
worker, real publisher — against a local HTTP server that mimics the Spotify
pages.

That server is not a mock of our own code; it is a stand-in for the only
thing we cannot test here, which is Spotify itself. So the end-to-end tests
cover the logic completely, and the browser selectors remain the one part
that needs a real account to confirm.

See [TESTING.md](TESTING.md).
