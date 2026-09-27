# Installation

Windows first. Linux and macOS work the same way from a terminal.

---

## 1. What you need

- **Windows 10 or 11** (or Linux / macOS)
- **Python 3.10 or newer** — check with `python --version`
- **A Spotify for Creators account** with at least one show, verified

You do **not** need FFmpeg, a database server, Node.js, Docker, or any
account other than Spotify.

If Python is not installed: download it from
<https://www.python.org/downloads/>, and **tick "Add python.exe to PATH"**
during setup. That tick is the step people miss.

---

## 2. Install

### Windows

Double-click **`SETUP.bat`**.

It will:

1. find a suitable Python,
2. create a private environment in `.venv\` inside the project,
3. install the packages from `requirements.txt`,
4. download the Chromium browser Playwright drives (~150 MB, once),
5. create `config\config.yaml` from the example,
6. run the full environment check and print the result.

It is safe to run more than once.

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
mkdir -p config && cp config/config.example.yaml config/config.yaml
.venv/bin/python -m app --check
```

---

## 3. Check

```bat
CHECK.bat
```

or

```bash
.venv/bin/python -m app --check
```

Every line is `[OK]`, `[WARN]` or `[FAIL]`.

- `[FAIL]` — something must be fixed before publishing. The last lines list
  each one with the command that fixes it.
- `[WARN]` — the app works without it. FFmpeg and a saved Spotify session
  both start as `[WARN]`.

The exit code is `0` only when there are no `[FAIL]` lines, so this is safe to
use in a script.

---

## 4. Sign in to Spotify

```bat
SIGN_IN.bat
```

or `python -m app --auth`.

A **real browser window** opens on Spotify's own login page. You type your
password there.

- The app never sees, asks for, logs or stores your password.
- The app never types your password for you.
- Spotify's own page handles any two-factor check.
- The app never tries to solve a CAPTCHA or get around any security control.
  If Spotify shows one, it waits for you to do it.

After a successful sign-in, the session is kept in
`data\browser-profile\` and reused for every episode. This normally means you
do this **once**. When Spotify expires it, you do it again.

You do not have to sign in before configuring the app, but nothing can be
published until you have.

---

## 5. Find your show id

Open your show on <https://spotify.com/creators>. The address bar looks
like:

```
https://spotify.com/creators/pod/dashboard/1234567890abcdef
                                         ^^^^^^^^^^^^^^
```

Copy that last part into `config\config.yaml`:

```yaml
shows:
  - name: "Antike Spiritualitaet"
    spotify_show_id: "1234567890abcdef"
```

You can leave it empty; the app will offer to find the show when you first
connect.

---

## 6. Set up your shows

Edit `config\config.yaml`. At minimum, give each show a `name` and set
`enabled: true` for the ones you actually use.

```yaml
shows:
  - name: "Antike Spiritualitaet"
    enabled: true
    timezone: "Europe/Berlin"
    spotify_show_id: "1234567890abcdef"
    defaults:
      language: "de"
      publish: "NOW"
```

Then run `python -m app setup` to create the folders, or let `START.bat`
create them.

---

## 7. Start

```bat
START.bat
```

The app watches the incoming folders and opens the dashboard at
<http://127.0.0.1:8765>.

Leave the window open. Closing it stops the app.

### Make it start with Windows

```bat
python -m app install-autostart
```

```bat
python -m app remove-autostart
```

---

## 8. Try it before you trust it

Do **not** start with a real episode. Make a test one:

```bat
python -m app make-test-media "shows\Antike Spiritualitaet\incoming" --name "Testlauf" --media mp3
```

That writes `Testlauf.mp3` and `Testlauf.txt` into the incoming folder. The
app picks them up, publishes a short public episode on your show, and archives
them.

If you would rather not publish anything, run the read-only checks first:

```bat
CHECK_SPOTIFY.bat
python -m app spotify-smoke-test
```

Neither creates an episode.

---

## What each thing is for

| Folder | Purpose |
|---|---|
| `.venv\` | The private Python environment. Do not delete it. |
| `config\config.yaml` | Your settings. Back this up. |
| `shows\<name>\incoming\` | **You put files here.** |
| `shows\<name>\archive\` | Finished episodes, kept. Do not empty casually. |
| `shows\<name>\failed\` | Files that could not be used. |
| `data\podcast.db` | The queue and the full history. |
| `data\browser-profile\` | The saved Spotify sign-in. Treat as a password. |
| `logs\` | Rotating log files. |
| `diagnostics\` | Screenshots and reports when something looked wrong. |

**Back up** `config\`, `shows\*\archive\` and `data\podcast.db`.

**Do not back up to a cloud folder** `data\browser-profile\` — a sync client
can corrupt a live browser profile. Back it up only when the app is closed.

---

## Updating

```bat
STOP.bat
git pull
SETUP.bat
START.bat
```

The database is migrated on start. Jobs that were mid-upload when you stopped
are recovered: the app checks Spotify for the episode before doing anything,
so nothing is published twice.

---

## Uninstalling

1. `STOP.bat`
2. Delete the folder.

The saved Spotify sign-in goes with it. Sign in again on the next install.
