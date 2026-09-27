# Quick start

The shortest path from nothing to a published episode.

---

## Where do I put the files?

```
PodcastAutomation\
    shows\
        Antike Spiritualitaet\      <- your show's name from config.yaml
            incoming\
                MyEpisode.mp4        <- the video
                MyEpisode.txt        <- the details
```

**Both files, same base name, in `incoming\`.** `MyEpisode.mp4` and
`MyEpisode.txt` go together. The media extension can be `.mp4`, `.mov`, `.mpg`,
`.mp3`, `.m4a` or `.wav`.

That is the whole workflow. You never touch Python, a database, a browser or
a command line.

---

## What goes in the .txt?

One complete, valid example:

```
TITLE=Folge 8: Der Schlüssel zum Bewusstsein
DESCRIPTION=Eine Reise in die verborgenen Zusammenhänge von Bewusstsein,
Herz und antiker Spiritualität.

In dieser Folge sprechen wir über die Rolle des Herzens im Verstehen.

SEASON=3
EPISODE=8
PUBLISH=SCHEDULE
PUBLISH_DATE=2026-10-01
PUBLISH_TIME=09:00
EXPLICIT=false
LANGUAGE=de
CHAPTERS=
00:00 Intro
02:15 Das vergessene Wissen
07:42 Herz und Bewusstsein
14:05 Der Schlüssel
22:30 Fragen und Antworten
```

`TITLE` and `DESCRIPTION` are required. Everything else is optional. Save it
as **UTF-8**.

A minimal version, if you do not need chapters or scheduling:

```
TITLE=Meine erste Folge
DESCRIPTION=Was in dieser Folge besprochen wird.
```

Full field reference: [METADATA_FORMAT.md](METADATA_FORMAT.md).

---

## How do I start the app?

**Windows, after `SETUP.bat` and `SIGN_IN.bat` have been run once:**

```
START.bat
```

That is it. The app starts watching and publishes anything you drop in.

**From a terminal, anywhere:**

```
python -m app
```

Use the same Python that `SETUP.bat` created, i.e. `.venv\Scripts\python.exe`
on Windows.

To make it start with Windows automatically:

```
python -m app install-autostart
```

---

## How do I connect Spotify?

**Once, in a real browser:**

```
SIGN_IN.bat
```

or

```
python -m app --auth
```

A browser window opens on Spotify's own login page. **You** type your
password, in the browser, on Spotify's page. The app never sees it, never
asks for it, and never stores it.

If Spotify asks for two-factor confirmation or a CAPTCHA, you do that in the
browser. The app waits; it does not try to get around it. If Spotify needs
something and you are not there, the episode stops in `NEEDS_USER_ACTION` and
says so, rather than failing silently.

The legitimate session is then kept in `data\browser-profile\` and reused for
every episode. This normally means signing in once, not once per episode.

To sign out:

```
python -m app --disconnect
```

---

## How do I know it worked?

**Three independent confirmations.**

1. **The dashboard** — <http://127.0.0.1:8765>, open while the app runs. The
   episode moves through the states below and ends at **Archived**. Each
   worker shows what it is doing right now.

   | State | Meaning |
   |---|---|
   | Discovered | The pair was found |
   | Queued | Waiting for a worker |
   | Validating | Checking the file against Spotify's specs |
   | Authenticated | Checking the Spotify session |
   | Uploading | Sending the file |
   | Processing | Spotify is converting it |
   | Ready to publish | Spotify says the file is usable |
   | Publishing | Filling in title, description, date |
   | **Published** | **Spotify confirmed the episode exists** |
   | Archiving | Moving your files |
   | **Archived** | **Done** |

   Uploading is not publishing. Only "Published" means Spotify has confirmed
   the episode exists.

2. **Your files moved.** From `incoming\` to:

   ```
   shows\Antike Spiritualitaet\archive\2026\09\MyEpisode.mp4
   shows\Antike Spiritualitaet\archive\2026\09\MyEpisode.txt
   ```

   They are **moved, never deleted**, and only after publication is
   confirmed.

3. **Spotify itself.** Open your show on
   <https://spotify.com/creators> — the episode is in the list.

If something goes wrong the episode stops in a state you can act on
(**Needs attention**, **Needs your input**, or **Needs checking**) with a
message saying what happened. Details:
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## What happens when it fails?

- **It retries by itself**, with growing waits between attempts, a limited
  number of times.
- **You can retry immediately** with the **Retry now** button.
- **You can cancel** with the **Cancel** button; your files stay in
  `incoming\`.
- **It never publishes the same episode twice.** If the app loses track of
  whether Spotify already has it — a crash, a dropped connection — it checks
  Spotify's real episode list before doing anything, and adopts what is
  already there.
- **An archive failure never republishes.** The episode stays Published, and
  only the filing away is retried.

---

## Try it without publishing anything real

Make a short test episode:

```
python -m app make-test-media "shows\Antike Spiritualitaet\incoming" --name "Testlauf" --media mp3
```

Or check everything without touching Spotify at all:

```
CHECK.bat
```

```
CHECK_SPOTIFY.bat
```

`CHECK_SPOTIFY.bat` is **read-only**: it signs in with the saved session,
opens Spotify, and reports what it can see. It creates and publishes nothing.

---

## The one thing that has not been verified

**The browser has never been run against the live Spotify for Creators site.**
Playwright is installed, but the Chromium download is blocked in the build
environment and there is no Spotify account there, so the selectors and the
form-filling are written from Spotify's current documentation and have **not**
been confirmed against the real interface.

You can find out in one command, without publishing anything:

```bat
CHECK_SPOTIFY.bat
```

Everything else — the folder watcher, the metadata parser, the validator, the
queue, retries, recovery, duplicate prevention, archiving, the dashboard, the
commands — is exercised by the test suite against the real code, and the
Spotify side is exercised against a faithful stand-in site.

Before you rely on this for a real episode, run `CHECK_SPOTIFY.bat` and read
[SPOTIFY_INTEGRATION.md](SPOTIFY_INTEGRATION.md) for what each stage does and
what to do if Spotify's pages change.
