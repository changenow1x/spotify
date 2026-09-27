# PodcastAutomation

Drop a video and a text file into a folder. An episode appears on your Spotify
show. Nothing else to click.

Built for Windows, works on Linux and macOS too.

---

## The whole workflow

```
SETUP.bat                 install everything (once)
      ↓
SIGN_IN.bat               connect Spotify, in a real browser (once)
      ↓
START.bat                 leave this running
      ↓
  SHOW/incoming/
      Episode.mp4     ┐
      Episode.txt     ┘  drop both in, same base name
      ↓
  AUTOMATION          detect → wait for the copy to finish → pair →
                      read the details → check the file against Spotify's
                      specifications → upload → fill in the details →
                      publish → confirm with Spotify
      ↓
  SPOTIFY             the episode is live
      ↓
  SHOW/archive/YYYY/MM/   the two files, moved there
```

From the moment you drop the files in, you do nothing. Watch it happen on the
dashboard at `http://127.0.0.1:8765`.

If the network drops, the app restarts, or you press Ctrl+C halfway, it picks
up where it left off. **It never publishes the same episode twice.**

Nothing is ever deleted. Files are *moved* to the archive, and only after
Spotify confirms the episode is published.

---

## Getting started

Seven steps, once.

**1. Install.** Double-click **`SETUP.bat`**. It creates a private Python
environment, installs what is needed, downloads the browser, and checks the
result. You never touch Python, pip or a terminal.

**2. Check the installation.**

```
python -m app --check
```

Every line says `OK`, `WARN` or `FAIL`. Fix anything marked `FAIL` before
going on — on Windows, `SETUP.bat` fixes most of them.

**3. Sign in to Spotify.** Double-click **`SIGN_IN.bat`**. A real browser
window opens on Spotify's own login page. You type your password *there*, on
Spotify's site. The app never sees it and never stores it. If Spotify asks for
a CAPTCHA or a two-factor code, you do that yourself in the same window.

Spotify also requires a verified account before it will publish your first
episode. If you have not verified yours, do that from the email Spotify sent,
or from the Spotify for Creators site.

**4. Check the connection.** Double-click **`CHECK_SPOTIFY.bat`**. It is
read-only: it signs in, finds your show, reads the episode list, and reports
what it sees. It creates and publishes nothing.

**5. Point the app at your show.** Edit `config\config.yaml` and set the
show's name and Spotify id. The dashboard's *Shows* page shows the id for each
show, or run:

```
python -m app status
```

**6. Start it.** Double-click **`START.bat`**. The dashboard opens at
`http://127.0.0.1:8765`. Leave the window open.

**7. Put the files in.** Drop two files with the same name into the show's
`incoming` folder:

```
shows\Antike Spiritualitaet\incoming\
    Folgen_08_Der_Schluessel.mp4
    Folgen_08_Der_Schluessel.txt
```

The names must match exactly apart from the extension. That is the entire
daily workflow — from here on the app runs by itself.

Watch it happen on the dashboard, or in the window you started. The job walks
through `DISCOVERED → VALIDATING → QUEUED → UPLOADING → PROCESSING →
PUBLISHING → PUBLISHED → ARCHIVING → ARCHIVED`. If something needs you, it
says so and waits.

The finished files are in:

```
shows\Antike Spiritualitaet\archive\2026\09\
```

Nothing is deleted, ever. The files are **moved** there once Spotify confirms
the episode is published.

[docs/QUICKSTART.md](docs/QUICKSTART.md) is this same walkthrough with the
states and the failure cases spelled out.

| File | What it does |
|---|---|
| `SETUP.bat` | First-time install. Run once. |
| `SIGN_IN.bat` | Sign in to Spotify once, in a real browser. |
| `START.bat` | Start watching and publishing. |
| `CHECK.bat` | Is everything ready? Safe to run any time. |
| `CHECK_SPOTIFY.bat` | Can we reach Spotify? **Read-only.** |
| `STOP.bat` | Stop the running automation. |
| `RESTART.bat` | Stop and start again - after changing the config, or if it is stuck. |

Every one of these is a thin wrapper around a command you can also type
yourself:

```bat
python -m app --check
python -m app --auth
python -m app
```

---

## The folder layout

Everything lives in one folder so you can back it up or move it as a whole.

```
PodcastAutomation\
    SETUP.bat  START.bat  SIGN_IN.bat  CHECK.bat  CHECK_SPOTIFY.bat  STOP.bat
    config\config.yaml          <- your settings
    shows\
        Antike Spiritualitaet\
            incoming\           <- YOU PUT FILES HERE
            archive\            <- finished episodes, kept safely
            failed\             <- files that could not be used
        Another Podcast\
    data\
        podcast.db              <- the queue, the history, the session
        browser-profile\        <- the saved Spotify sign-in
    logs\
    diagnostics\                <- screenshots when something looks wrong
```

You only ever touch `incoming\`.

---

## The metadata file

The `.txt` beside your media file says what the episode is called. It is
required by default, because the app will not invent a title for you.

```
TITLE=Folge 8: Der Schlüssel zum Bewusstsein
DESCRIPTION=Eine Reise in die verborgenen Zusammenhänge von Bewusstsein,
Herz und antiker Spiritualität.

SEASON=3
EPISODE=8
PUBLISH=NOW
EXPLICIT=false
LANGUAGE=de
```

`DESCRIPTION` is the only field without a sensible default. Everything else
is optional and falls back to the show's `defaults` in the config.

| Field | Notes |
|---|---|
| `TITLE` | **Required.** Shown in your feed. |
| `DESCRIPTION` | **Required.** The episode notes. Line breaks are kept. |
| `SEASON` / `EPISODE` | Numbering. `EPISODE` alone is fine. |
| `PUBLISH` | `NOW`, or a date/time to schedule. |
| `EXPLICIT` | `true` / `false` |
| `LANGUAGE` | e.g. `de`, `en` |
| `CHAPTERS` | Timestamps, one per line. See `docs/METADATA_FORMAT.md`. |
| `AUTHOR` | Overrides the show author for this episode. |

To publish **without** a `.txt`, set `media.allow_media_only: true` in the
config. The file name then becomes the title. This is off by default, and we
recommend leaving it off.

---

## Scheduling

In the `.txt`:

```
PUBLISH=2026-10-01 09:00
```

Times without a zone are read in the show's `timezone` from the config
(`Europe/Berlin` above). The episode is prepared in advance and Spotify
publishes it at that time. The files are archived as soon as Spotify confirms
the episode exists — a scheduled episode is a real episode, it is just not
public yet.

---

## The dashboard

While the app runs it serves a local page, usually at
<http://127.0.0.1:8765>.

It shows the real state of everything: the watcher, each worker thread and
what it is doing right now, every episode and where it got to, and the recent
log. You can retry, cancel, pause, resume, and reconnect Spotify from there.

It is bound to `127.0.0.1`, so only your own computer can open it. Changing
the address in the config to expose it to your network is not recommended.

---

## When something goes wrong

The app never fails quietly. An episode that cannot be published ends up in a
state you can see and act on:

- **Needs attention** — the attempt failed. It retries on its own with
  growing waits, up to `retry.max_attempts`. Press **Retry now** to skip the
  wait, or **Cancel** to stop trying.
- **Needs your input** — Spotify wants something only you can do, for
  example a CAPTCHA or a confirmation mail. The app stops and tells you.
  It never tries to get around it.
- **Needs checking** — the app lost track of whether Spotify already has the
  episode, for example the connection dropped during the final confirmation.
  Before doing anything it looks at the real Spotify episode list and adopts
  what is already there. This is the state you will see if you stop the app
  during a publication.

Diagnostics — including a screenshot of the page it was looking at when
something looked wrong — land in `diagnostics\`.

---

## Commands

| Command | What it does |
|---|---|
| `python -m app` | Run the automation. |
| `python -m app --check` | Check the installation. Changes nothing but folders. |
| `python -m app --auth` | Sign in to Spotify, once, in a real browser. |
| `python -m app status` | Print a summary. |
| `python -m app jobs` | List episodes and their state. |
| `python -m app retry <id>` | Retry now. |
| `python -m app cancel <id>` | Stop trying. |
| `python -m app spotify-check` | Read-only Spotify connectivity check. |
| `python -m app spotify-smoke-test` | Read-only check of the real account. |
| `python -m app doctor` | Deeper Spotify diagnostics. |
| `python -m app make-test-media <folder>` | Write a sample episode. |
| `python -m app test` | Run the test suite. |

`--check` and `spotify-check` are safe: they read, they do not publish.
`spotify-smoke-test` publishes nothing unless you also pass `--live-publish`
**and** type `PUBLISH` to confirm.

### Using a different data folder

`--root` points the app at a different folder for `shows/`, `data/`, `logs/`
and `logs/diagnostics/`, leaving `config/` where it is. It is handy for keeping
a second show library, or for trying something out without touching your real
one. It works before or after the command:

```bash
python -m app --root D:\PodcastTwo check
python -m app check --root D:\PodcastTwo
```

`--root` is a flag for a command, not one itself. Like `check`, it creates the
standard empty folder tree in the root you name, and nothing else: it never
copies, moves or deletes anything already there.

---

## Requirements

- Windows 10/11 (Linux and macOS work too)
- Python 3.10 or newer — `SETUP.bat` installs it if you have not
- A Spotify for Creators account with a verified show

FFmpeg is optional. Without it the built-in reader handles MP3, WAV, M4A, MP4
and MOV, which covers normal recordings.

---

## What is tested, and what is not

This matters, so it is stated plainly.

**VERIFIED LOCALLY** — the folder watcher, the metadata parser, the media
validator, the database, the worker, the state machine, the archive manager,
the dashboard API and the command line all run for real in the test suite. The
end-to-end tests drop real files into a real temporary folder and follow them
all the way to `ARCHIVED`, then close and reopen the database and restart to
prove nothing is published twice. The 600 MB large-file gate, the two-worker
concurrency gate, the security scans and the Windows launcher checks all run
here too.

**VERIFIED AGAINST MOCK SPOTIFY** — the browser step is exercised against a
local server that mimics the Spotify for Creators pages: sign-in, upload
progress, the episode form, scheduling, form rejection, the episode list used
for duplicate checks, and every failure path in between. This proves the
*logic* end to end. It does not prove the real site behaves the same.

**REQUIRES REAL USER ENVIRONMENT** — the browser has never been pointed at the
live Spotify for Creators site. The Chromium binary could not be downloaded in
the build environment, and no Spotify account is available there, so the
selector list, the upload step and the form filling are written from Spotify's
current documentation and from the simulated pages, and have **not** been
confirmed against the real interface. Playwright is installed (1.63) and the
app reports the missing browser truthfully rather than hiding it.

Before you rely on this for a real episode, run:

```bat
CHECK_SPOTIFY.bat
```

It is read-only. If the selectors need adjusting, `docs/SPOTIFY_INTEGRATION.md`
lists the stages and `docs/TESTING.md` explains how to find out what changed
and how to correct it without guessing.

[docs/SPOTIFY_INTEGRATION.md](docs/SPOTIFY_INTEGRATION.md) has the per-stage
detail.

---

## Documentation

| Document | About |
|---|---|
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | The ten-minute version: drop two files, get an episode |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Installing, signing in, troubleshooting setup |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Day-to-day use, the folder layout, the dashboard |
| [docs/METADATA_FORMAT.md](docs/METADATA_FORMAT.md) | Every field of the `.txt`, and chapters |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built and why |
| [docs/TESTING.md](docs/TESTING.md) | What is tested, how to run it, what is not covered |
| [docs/SPOTIFY_INTEGRATION.md](docs/SPOTIFY_INTEGRATION.md) | The browser automation, stage by stage |
| [docs/SECURITY.md](docs/SECURITY.md) | What is protected, and how |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Every error message and what to do |

---

## What this will not do

It will not invent a title or a description for you. It will not publish a
file without the metadata it needs. It will not publish an episode twice. It
will not delete your recordings. It will not bypass a CAPTCHA, two-factor
check, or any other security control — if Spotify asks you to prove something,
the app stops and tells you. And it will not publish a partially copied file:
it waits until the file has stopped changing before it touches it.
