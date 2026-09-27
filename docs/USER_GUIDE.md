# User guide

How to use the app day to day.

---

## The whole idea

Put a video and a text file in a folder. An episode appears on your Spotify
show. Everything else is automatic.

There is no queue to manage, no "publish" button, no account to connect per
episode. The app watches the folder and reacts to what it finds.

---

## Your folders

```
PodcastAutomation\
    shows\
        Antike Spiritualitaet\
            incoming\     <- put files here
            archive\      <- finished episodes
            failed\       <- files that could not be used
        Another Podcast\
            incoming\
            archive\
            failed\
    data\
        podcast.db
        browser-profile\    <- your saved Spotify sign-in
    logs\
    diagnostics\
    config\config.yaml
```

One folder per show. The folder name is what you called the show in
`config\config.yaml`, so renaming the show means the folder is renamed too.

**`incoming\`** is the only folder you put things in.
**`archive\`** keeps everything that was published. Do not empty it — it is
your copy of what went out.
**`failed\`** holds files that could not be paired or used, so nothing is
silently lost.

---

## Publishing an episode

### 1. Drop the files in

```
shows\Antike Spiritualitaet\incoming\
    Folgen_08_Der_Schluessel.mp4
    Folgen_08_Der_Schluessel.txt
```

The two files must have the **same base name** and differ only in the
extension. Copy them in any order — the app waits for the `.txt` if the media
arrives first, and picks the episode up as soon as both are there.

### 2. Wait for the copy

The app does not touch a file that is still changing. It waits until the size
and the modification time have stopped moving for `watcher.stable_seconds`
(5 seconds by default), checked `stable_checks` times.

Copy a 4 GB video over a slow network? It waits as long as it takes. A file
being written to is never uploaded.

**If you copy with a tool that writes a temporary name first** — Chrome,
Firefox, and most download managers use `.part`, `.crdownload`, `.download`,
`.partial`, `.tmp` and similar — those names are ignored. The episode starts
when the final name appears.

### 3. The app takes over

You can watch it happen in the dashboard. The episode moves through:

| State | What is happening |
|---|---|
| `DISCOVERED` | The pair was found |
| `QUEUED` | Waiting for a free worker |
| `VALIDATING` | Checking the file against Spotify's specs |
| `VALIDATED` | The file is usable |
| `AUTHENTICATING` | Checking the Spotify session |
| `UPLOADING` | Sending the file to Spotify |
| `PROCESSING` | Spotify is converting it |
| `READY_TO_PUBLISH` | Spotify says the file is usable |
| `PUBLISHING` | Filling in title, description, date |
| `PUBLISHED` | Spotify confirms the episode exists |
| `ARCHIVING` | Moving your files to the archive |
| `ARCHIVED` | Done |

**`UPLOADING` is not `PUBLISHED`.** The file arriving at Spotify means
nothing until the episode exists. Only `PUBLISHED` means Spotify has
confirmed it.

### 4. Archived

Both files are moved to `archive\`, keeping their names. Your originals are
never deleted — they are moved, and they are never touched before publication
is confirmed.

---

## The dashboard

While the app runs: <http://127.0.0.1:8765>

### What you see

- **Top bar** — whether Spotify is connected, and Connect / Disconnect /
  Pause.
- **Summary cards** — how many episodes are in progress, published, need
  attention, and how many files are still being copied.
- **Your shows** — each show, its incoming folder, and a button to open that
  folder in Explorer.
- **Workers** — each worker thread and exactly what it is doing right now.
  If an episode is stuck, this is where you see it.
- **Episodes** — every job with its state, filtered by show or state.
- **Activity and log** — the last events, and the tail of the log file.

### What you can do

| Button | What it does |
|---|---|
| **Retry now** | Try a failed episode immediately, without waiting. Only offered for episodes that failed or need your attention. A published, scheduled or archived episode is finished, so the button is hidden and the API refuses with `409`. |
| **Cancel** | Stop trying. The files stay in `incoming\`. |
| **Archive** | Archive a published episode whose files are still there. |
| **Pause / Resume** | Hold everything without stopping the app. |
| **Connect / Disconnect** | Sign in to Spotify, or forget the session. |
| **Open folder** | Show a folder in Explorer. |

### Filtering

The **Episodes** list filters by show and by state, including the states that
mean trouble: `FAILED`, `NEEDS_USER_ACTION`, `NEEDS_RECONCILIATION`.

---

## When an episode needs you

### Needs attention (`FAILED`)

The attempt failed. The message says what went wrong and what to do.

The app retries on its own with growing waits — 20 seconds, then 40, then 80,
up to `retry.max_attempts` tries, with a little randomness so a batch of
episodes does not all retry at the same second.

Press **Retry now** to skip the wait. Press **Cancel** to stop.

### Needs your input (`NEEDS_USER_ACTION`)

Spotify wants something only you can do — a CAPTCHA, a confirmation mail, a
security check.

The app stops and tells you. **It does not try to get around it.** Complete
the check in the browser window, then press **Retry now**.

### Needs checking (`NEEDS_RECONCILIATION`)

The app lost track of whether Spotify already has this episode — the
connection dropped during the final confirmation, or the app was stopped
mid-publication.

Before doing anything it **looks at the real Spotify episode list** and
adopts what is already there. If the episode exists, it is recorded as
published and never uploaded again. If it does not, it is uploaded once.

This state is a safety net, not a failure. It usually resolves itself on the
next attempt.

---

## Scheduling

```
PUBLISH=2026-10-01 09:00
```

Times are read in the show's `timezone`. The app prepares the episode in
advance; Spotify publishes it at that time.

The files are archived once Spotify confirms the episode **exists** — a
scheduled episode is a real episode, it is just not public yet. That way a
failure is still noticed and still retried.

Spotify's guidance is to give it at least a few hours. Very short notice may
be published late or immediately. A date in the past is published at once;
the app does not pretend otherwise.

---

## Publishing several episodes

Drop them all in. Two or more worker threads can run at once
(`max_concurrent_jobs` in the config, 1 by default).

Each episode is identified by the **content** of the file, not its name. So:

- Copying the same file again under a different name is recognised as the
  same episode and not published twice.
- Two different files that happen to share a name are two different episodes.
- Renaming a file after it was discovered does not confuse the app.

If you copy the same episode again on purpose, delete the archive copy first
or the app will correctly treat it as a duplicate.

---

## Stopping and starting safely

Close the window, or run `STOP.bat`.

A job that was mid-upload is left in a state the app recovers on the next
start. Before doing anything it checks Spotify for the episode. **Nothing is
ever published twice because of a restart.**

To start with Windows automatically: `python -m app install-autostart`.

---

## Housekeeping

| Folder | Safe to delete? |
|---|---|
| `logs\` | Yes, any time. |
| `diagnostics\` | Yes — old screenshots. |
| `data\podcast.db` | **No.** That is the queue and the history. |
| `data\browser-profile\` | **No.** That is your sign-in. |
| `shows\*\archive\` | Your published episodes. Keep them. |
| `shows\*\failed\` | Yes, once you know you do not need the files. |

### Growing too large

The database keeps a full history of every job, which is small. The browser
profile and the `logs\` folder are the two that grow; both are safe to trim
while the app is stopped.
