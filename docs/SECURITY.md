# Security

What is protected, how, and what is deliberately not done.

The threat model is a **local desktop tool on one user's own Windows machine**.
It is not a web service. The dashboard is bound to `127.0.0.1` and is not
exposed to the network.

---

## Your Spotify password

**The app never sees it, never asks for it, and never stores it.**

Signing in opens Spotify's own login page in a real, visible browser. You
type your password there, into Spotify's form, on Spotify's site. The app
watches for the result. There is no code path that types a password, reads a
password field, or asks you for credentials.

## Two-factor, CAPTCHA and other checks

**Never bypassed.** Not attempted, not detected-and-worked-around, not
automated in any way.

If Spotify shows a CAPTCHA, asks for a two-factor code, or wants a
confirmation by mail, the episode stops in `NEEDS_USER_ACTION` with a message
saying what Spotify is asking for. You do it. You press **Retry now**.

There is no CAPTCHA-solving service, no third-party solver, no proxy rotation
and no rate-limit evasion anywhere in this project. That is a deliberate
design decision, not a missing feature.

## The saved session

The legitimate session — the cookies Spotify itself issued after you signed in
— is stored in:

```
data\browser-profile\
```

Treat that folder exactly like a password:

- Anyone who can read it is signed in as you on Spotify.
- It is **not** committed to version control (`.gitignore` excludes
  `browser-profile/` and `data/`).
- **Do not put it in a cloud-synced folder** like Dropbox or OneDrive while
  the app is running. A sync client can copy a live browser profile and
  corrupt it. Back it up only with the app stopped.
- To sign out and destroy it: `python -m app --disconnect`, or delete the
  folder.

Sessions expire the way Spotify decides they expire. When that happens the app
says so and asks you to sign in again. It does not try to renew it
automatically behind your back.

## The dashboard

- Bound to `127.0.0.1` by default, so only this computer can reach it.
- Every action that changes something (retry, cancel, pause, resume, connect,
  disconnect) requires a per-installation token stored in the database and
  compared with `hmac.compare_digest`, so it is not vulnerable to a timing
  comparison.
- Read-only endpoints need no token, because the dashboard is a single-user
  local page.
- `require_token: true` is the default in the example config.

Changing `api.host` to `0.0.0.0` exposes your queue to the network. The
config comment says so. Do not.

## Browser input safety

**The dashboard has no HTML injection surface.**

Every value that comes from the API — a file name, an error message from
Spotify, a title — is written with `textContent`. There is no `innerHTML`,
`outerHTML`, `insertAdjacentHTML` or `document.write` assignment anywhere in
`app/dashboard/app.js`.

This matters: a file name is attacker-influenced input. A file called
`<img src=x onerror=alert(1)>.mp4` is a perfectly legal file name. Because
the dashboard never interprets text as markup, that file name is displayed,
not executed.

`tests/test_api.py` fails the build if an `innerHTML` assignment is ever
added.

## File paths

A media file, a metadata file and a show name are all user-controlled and
all end up in paths.

- `app/security/paths.py` is the single place paths are validated.
- `safe_join()` refuses any component that is absolute, contains a drive
  letter, or escapes the installation root.
- `sanitize_filename()` reduces a name to one harmless component: it keeps
  the last path component only, replaces Windows-illegal characters,
  normalises Unicode to NFC, disambiguates Windows **reserved device names**
  (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9` — so
  `nul.txt` becomes `nul_.txt` rather than writing to the null device),
  strips trailing dots and spaces, and truncates to fit Windows' path limit.
- Archive destinations are checked to be inside the configured archive
  folder.

A traversal attempt is refused, not sanitised into something surprising. Two
real defects of this kind were found and fixed by the test suite:

- `make-test-media --name "../../escape"` wrote outside the target folder.
- `make-test-media` never wrote a `.txt` at all, so it produced a file the
  app's own safe default would reject.

## Subprocesses

**No `shell=True` anywhere in the project.** Every subprocess is invoked with
an argument list, so a file name containing `;`, `&&`, `|` or quotes is passed
as one literal argument and cannot become a command:

```python
subprocess.run([tool, str(path)], shell=False, check=True)
```

FFmpeg is invoked the same way, with its arguments built as a list. The only
shell-adjacent command is `schtasks`, used for the optional Windows
autostart, and it too uses an argument list.

## Secrets in logs

`app/security/redaction.py` scrubs anything that looks like a token, cookie,
`Authorization` header, or a credential embedded in a URL — on the way to the
log file *and* on the way to the dashboard, not only to disk.

Exception messages are passed through `scrub_exception()` before being stored
on a job or shown to the user, so a stack trace cannot leak a session value
into the UI or into `podcast.db`.

`tests/test_security_redaction.py` covers this, and
`tests/test_api.py` asserts the dashboard never echoes the token.

## File handling safety

- **Originals are never deleted.** Files are *moved* to the archive, only
  after Spotify has confirmed publication. A failed archive leaves the job
  `PUBLISHED` and the files in place; it never republishes.
- **A file being written is never uploaded.** The watcher waits until size
  and modification time have stopped changing, and ignores temporary
  extensions (`.part`, `.crdownload`, `.partial`, `.tmp`, …) entirely.
- **Truncated files are refused.** A constant-bitrate file states its full
  length in its header, so a cut-off file still claims its original duration.
  The validator compares the bytes on disk against the bytes the declared
  bitrate implies and refuses a file that is far smaller.
- **A failed archive is retried, not repeated as a publication.**

## Network and the dashboard

The app talks to Spotify over HTTPS through a real browser, and serves the
dashboard locally. It makes no other outbound connections, phones home, or
telemetry.

## Verified by the suite

| Property | Test |
|---|---|
| No `innerHTML` in the dashboard | `test_api.py::TestOutputEscaping` |
| Dashboard actions require a token | `test_api.py::TestTokenProtection` |
| Folder opening cannot escape the root | `test_api.py::TestOpenHelpersAreSafe` |
| Only Spotify links are opened | `test_api.py::TestOpenHelpersAreSafe` |
| Newlines/credentials in URLs are rejected | `app/security/paths.py::is_spotify_url` |
| Traversal and reserved names | `test_security_paths.py` |
| Secrets are redacted everywhere | `test_security_redaction.py` |
| Generators cannot escape their folder | `test_cli.py::TestReadOnlyCommands` |
| Large files are streamed, not loaded | `test_performance.py` |

The search for `shell=True`, `os.system`, `eval`, `exec`, `pickle.load` and
`innerHTML` across the project returns nothing outside docstrings and
comments.

---

## What this is not

- Not hardened for exposure to the internet. Do not bind the dashboard to a
  public address.
- Not multi-user. One installation, one user, one Spotify account.
- Not a content-security system. It refuses to guess at metadata; it cannot
  verify that a description is legally or factually correct.
- No password storage, no credential vault, no MFA handling — because the app
  does not handle credentials at all.
