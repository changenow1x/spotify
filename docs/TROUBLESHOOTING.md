# Troubleshooting

Every error the app can show you, what it means, and what to do about it.

---

## Start here

```bat
CHECK.bat
```

It is safe to run any time, changes nothing, and reports what is wrong in
plain words. Most problems below are answered by it.

---

## Setup

### "The environment is missing. Run SETUP.bat first."

There is no `.venv\Scripts\python.exe`. Run `SETUP.bat`. If you are on Linux
or macOS, run `.venv/bin/python -m app --check` instead.

### `CHECK.bat` says a Python version is too old

You need Python 3.11 or newer. `SETUP.bat` downloads the right one if it can;
otherwise install it from python.org, tick **Add Python to PATH**, and run
`SETUP.bat` again.

### "Nothing is listening on the dashboard port"

The app is not running. Start it with `START.bat`. If it was running and is
now not, the last lines of its window say why — that output is not
decorative.

### "The dashboard port is already in use"

Another copy of the app is already running, or a stale one is holding the
port. Run `STOP.bat`. If that does not clear it:

```bat
netstat -ano | findstr :8765
taskkill /PID <the number> /F
```

Or change `api.port` in `config\config.yaml` to something free.

---

## Signing in

### "Not signed in to Spotify"

Run `SIGN_IN.bat`. A browser opens on Spotify's own login page; you type your
password there. The app never sees it. It stores only the session Spotify
itself issued.

### "Spotify is asking for a CAPTCHA / a two-factor code"

The app stops and says so. Do the check yourself in the browser window, then
press **Retry now** in the dashboard. This is deliberate: CAPTCHAs and two-factor
prompts are never bypassed.

### "The saved session has expired"

Spotify decided the session ended. Run `SIGN_IN.bat` again. This happens
occasionally and is not a fault.

### Signing in does nothing / the browser never appears

- Check Playwright is installed: `python -m playwright --version`.
- If a browser is missing, the app says exactly which and how to install it.
  It never downloads a browser silently.
- Some corporate networks block browser automation. The app reports the
  connection failure rather than hanging.

### Signing in opened but the window is hidden behind others

Look for a second browser window. The app uses a real, visible browser on
purpose — a hidden one would make a login you cannot see.

---

## The episode itself

### "No matching .txt file"

The safe default: an episode is media **plus** a `.txt` with the same name.
`Episode 12.mp4` needs `Episode 12.txt`.

This is deliberate. Without the metadata file the app would have to invent a
title, guess the season and make something up about the description — and
publishing invented content under your show's name is not a reasonable
default.

If you genuinely have media with no metadata, see
[media-only mode](USER_GUIDE.md#media-only-mode).

### "The metadata file is missing a title"

`TITLE=` is required. Nothing else is.

### "The metadata file could not be parsed"

The parser is strict on purpose. The message names the **line number** and the
problem. Common causes: a BOM at the start, tabs where spaces are expected,
a duplicate field, or an `=` inside a value that was not meant to be one.

### "Chapters must be at least three"

Spotify will not show fewer than three. Either write three or more, or delete
the whole `CHAPTERS` block. The app will not drop chapters for you.

### "The first chapter must start at 00:00"

Spotify's rule. The first line is the start of the episode.

### "The description is empty"

Spotify requires a description. It is shown under the episode, so there is
nothing sensible to put there but your own words. Write a sentence.

### "The media is not a valid MP4/MOV/MPG/MP3/M4A/WAV"

The file extension does not match the contents, or the file is damaged. Check
it opens and plays normally on your machine.

Those are the formats Spotify lists: **MP4, MOV and MPG** for video, and
**MP3, M4A and WAV** for audio. Anything else — AIFF, for instance — is
refused, because Spotify does not accept it. See
[ARCHITECTURE.md](ARCHITECTURE.md#media) for the full list and for what is a
hard error versus a warning.

### "The media file looks truncated"

A file with a constant bitrate states its full length in its header, so a
half-copied file still claims its original length. The app compares the bytes
present against the bytes that length implies.

Usually the copy was still running. Wait for it to finish and the file will
appear; if it is genuinely truncated, copy it again.

### "The video is the wrong size / duration / aspect ratio"

Spotify's specifications are enforced. `docs/METADATA_FORMAT.md` lists the
limits. The message names the limit and what your file is.

### "The audio and video lengths differ"

Spotify requires them to match. This usually means the file was re-encoded
badly. Re-export it.

---

## Publishing

### "Spotify could not be reached"

A network problem. The job retries on its own with a growing wait, and you
can press **Retry now** once the network is back. Nothing is lost.

### "Spotify rejected the episode"

The message names the field Spotify objected to. Fix the `.txt`, then
**Retry now**. The files are still in `incoming`; nothing was archived.

### "Needs reconciliation" / "the state of this episode is unknown"

The app could not determine whether Spotify already has this episode, so it
did not guess and did not publish. This is the safe state.

Normally a **Retry now** resolves it. If it persists, open
`spotify.com/podcaster` and look: if the episode is there, the app will adopt
it on the next pass; if it is not, the app will publish it.

### "An episode with this title already exists on the show"

Spotify refuses two episodes with the same title and number. The app records
this and does not publish. Change the title or the episode number in the
`.txt`.

### "Spotify has an episode titled 'X', but the season or episode number does not match"

A visible warning, not a failure. The app found an episode with that title on
your show, could not prove it is this recording, and published yours anyway
rather than silently dropping your file.

Check the two episodes in Spotify's dashboard and delete the one you did not
mean to create.

### The episode is on Spotify but the dashboard says it is not

Spotify takes a little while to process. The app reports **processing** while
that is happening, which is different from published. Give it a few minutes,
then **Retry now**.

If it never resolves, open the episode in Spotify and check it is really
published. If it is, press **Retry now** — the app will find and adopt it.

### "This job has been retried too many times"

The job is parked in **Failed** with the last error. It is not lost. Fix the
cause, then **Retry now** — the attempt counter resets.

---

## Files and folders

### The files disappeared from incoming

That is what should happen: they were published and archived to
`archive\YYYY\MM\`. See [the user guide](USER_GUIDE.md#where-files-go).

### "Archiving failed"

The episode **is** published; only the move failed, usually because the file
is open in another program or the disk is full.

The job stays `PUBLISHED`. Close whatever has the file open and press **Retry
now** — it retries the archive, and it will not publish again.

### The app will not pick up a file

- Still being copied? The app waits until the size and modification time stop
  changing. A file being written is never uploaded.
- A temporary extension (`.part`, `.crdownload`, `.tmp`) is ignored
  entirely.
- Check the show folder really is `shows\<SHOW NAME>\incoming`.

### A file with a strange name was renamed

Windows-illegal characters are replaced, and names like `CON` or `nul` are
disambiguated (`nul.txt` → `nul_.txt`) because they are device names on
Windows. Nothing is lost; only unsafe characters change.

### Very long paths

Paths beyond Windows' limit are reported with the length and the limit. Move
the installation closer to the drive root, or shorten the show name. The app
shortens individual file names itself but cannot shorten a path you chose.

---

## The dashboard

### It will not load

Check the app is running (`START.bat`) and that you are on the same
computer — the dashboard is bound to `127.0.0.1` and is deliberately not
reachable from another machine.

### An action does nothing

Actions need the local token. If the database was recreated, open the app's
window and use the token it printed at startup. If the page was left open
across a restart, refresh it.

### The dashboard shows a stale state

The dashboard reads live worker state, not a cached copy. If it looks wrong,
press **Refresh**. If a worker genuinely died, its jobs are recovered and
rescheduled at the next start.

---

## Windows-specific

### "The file is in use by another process"

Something has the file open — a player, an editor, a sync client, or File
Explorer previewing it. Close it and press **Retry now**.

A file is never archived while it is open, so nothing is lost and nothing is
half-moved.

### Antivirus or "Controlled folder access" is interfering

Some security software blocks a process that moves files. Add an exclusion for
the installation folder, or allow the app in that software's settings.

### The command window closes immediately on an error

Run it from a terminal instead so the output stays visible:

```bat
.venv\Scripts\python -m app
```

### `--live-publish` is refused

It requires an explicit confirmation, and it is the only route to a real
publication. If you were expecting a safe check, use `CHECK_SPOTIFY.bat`
instead, which is read-only.

---

## Getting more detail

```bat
.venv\Scripts\python -m app --check
.venv\Scripts\python -m app --doctor
```

Both are read-only. `--check` verifies the installation; `--doctor` reports
configuration, database, watcher and folder state in more depth.

Logs are in `logs\`, with credentials and tokens redacted. If you report a
problem, that log is the useful part — check it still makes sense without your
Spotify account in it.
