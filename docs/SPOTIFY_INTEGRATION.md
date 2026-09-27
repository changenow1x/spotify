# Spotify integration, stage by stage

How an episode actually gets onto Spotify, what each stage checks, and where
it can fail.

Everything here runs against a **simulated** Spotify for Creators site in the
test suite. The stages are modelled on Spotify's published documentation
(links at the bottom). The real site has not been driven by this browser
session — see [the honesty section](#what-is-and-is-not-verified).

---

## Why a browser at all

**Spotify publishes no API for uploading episodes.** There is no endpoint, no
SDK, no OAuth scope that adds a file to a show. This was raised directly on
Spotify's developer forum and confirmed: externally hosted shows are
distributed through **RSS**, and only Spotify's own uploader writes episodes.

The app targets a normal show published through Spotify for Creators, so the
only route is a browser driving the real pages a person would use. That is why
it uses Playwright with the normal UI and no scraping of private data.

RSS was rejected deliberately: it changes what the user has (they would need
their own host and their own feed) and it is a different product from "drop a
file in a folder".

---

## The stages

Each stage below is a method in the publisher. Every one of them either
succeeds, fails with a specific reason, or reports **unknown** — never "it
probably worked".

### 1. Session — `ensure_session()`

Opens the saved browser profile and checks whether Spotify considers us signed
in.

- Signed in → returns a usable session.
- Not signed in → raises `NeedsUserAction`, and the job parks in
  `NEEDS_USER_ACTION` with a message telling you to run `SIGN_IN.bat`.
- Profile missing or unreadable → a specific error naming the folder, not a
  generic failure.

A CAPTCHA, a two-factor prompt or any other challenge is **not** handled. It
ends the stage with `NEEDS_USER_ACTION` and names what Spotify is asking for.
This is deliberate and is not a bug to work around.

### 2. Show selection — `_select_show()`

Finds the configured show among the shows on the account, and refuses to guess
if more than one matches.

- Exactly one match → use it.
- No match → the error names the show that was looked for and what is there.
- Several → the error lists them. The app does not pick the first one.

The show id is then written to the job, so subsequent stages go straight there.

### 3. Pre-publish duplicate check — `_pre_publish_check()`

Reads the show's episode list and decides whether this episode already exists.
This is the stage that prevents double-publishing; the rules are in
[ARCHITECTURE.md](ARCHITECTURE.md#idempotency-and-recovery).

- Decisive match → the existing episode is adopted, nothing is uploaded, and
  the job goes on to archive.
- A same-titled episode that is not provably the same recording → the episode
  is published, and a warning event is recorded so you can delete the
  duplicate in Spotify's dashboard.
- The list cannot be read → `unknown`, never "no duplicates".

### 4. Upload — `_upload()`

Uploads the media file on Spotify's upload form and watches the progress
element.

- The stream is **chunked**, not read into memory. A 2 GB video does not
  become a 2 GB Python string.
- A stalled upload raises a timeout that is explicitly retryable, with the
  last observed percentage in the message.
- A network drop mid-upload is a retryable failure, not a silent success.

**An upload that finishes is not a publication.** It only means the bytes
arrived. Stage 7 is what decides that.

### 5. Details — `_fill_details()`

Fills Spotify's own episode form from the validated metadata: title, season,
episode number, explicit flag, description, chapters.

- Every field is filled from a parsed, validated value. Nothing is invented,
  and a field the metadata file did not specify is left empty rather than
  guessed.
- Chapters go into the description as `00:00 Intro` lines, which is how
  Spotify represents them.
- A field Spotify rejects produces an error naming **the field and the value
  that was rejected**, plus the suggestion to fix the `.txt`.

### 6. Publish — `_submit()`

Clicks publish-now, or the scheduling control when the metadata says
`PUBLISH=at`.

- Publish now → waits for the confirmation.
- Scheduled → the date is written in the format Spotify's field expects, and
  the target is echoed back so a wrong date is visible immediately.

### 7. Confirmation — `_confirm_publication()`

The most important stage, because it is where a false success would do real
damage.

- Re-reads the episode list and looks for **this specific episode**, matched
  by the id of the row that appeared — not the first row with a matching
  title, which would be wrong whenever a show has two episodes with the same
  name.
- The three outcomes are kept apart:

| Outcome | Meaning | What happens |
|---|---|---|
| found, state `published` | Spotify confirms it | `PUBLISHED` |
| found, state `scheduled` | it will go out later | `SCHEDULED` |
| not found | cannot confirm | `NEEDS_RECONCILIATION` — never a guess |

- If the list cannot be read, the result is `unknown`, and the job waits for
  reconciliation. It is never reported as published.

### 8. Details read-back — `_verify_details()`

Reads the saved episode back and compares season, episode number, explicit
flag and chapters against what was asked for. A mismatch is reported as a
warning event on the job, visible in the dashboard, because a quietly wrong
episode number is worth knowing about.

### 9. Archive — `app/archive/manager.py`

Only reached after a confirmed publication. Moves the media and metadata files
into `archive/YYYY/MM/`, unchanged, and only ever *moves* them.

- A name collision is disambiguated (`Episode (2).mp3`), never overwritten.
- An archive failure leaves the job `PUBLISHED` and the files where they are.
  It never republishes.

---

## Selectors

All selectors live in `app/publishers/spotify/selectors.py`, as ordered
alternatives. Playwright uses the first that matches, so a Spotify redesign
that keeps one of them keeps the app working.

The app never clicks "somewhere near the button" and never coordinates clicks
by position. If no selector matches, the stage fails with a diagnostic that
names what was looked for, and — crucially — the job stops rather than
continuing on the assumption that it worked.

Each list is deliberately wider than the current page, so a small markup
change is absorbed without a code edit.

---

## Per-worker browser sessions

Sessions are held in a `threading.local()` registry, one browser per worker
thread. Two workers sharing one page produce duplicated entries and form
state belonging to the other episode — a real defect that the two-worker test
found. The fix was per-thread sessions, and the test now runs two workers over
concurrent jobs and asserts that each job gets its own episode.

---

## What is and is not verified

**VERIFIED AGAINST MOCK SPOTIFY** — all nine stages, including the failure
paths: upload stalling mid-way, a network drop, Spotify rejecting the form,
a lost response after Spotify accepted, an unreadable episode list, and
scheduling. Duplicate detection, the title-clash warning, and adoption of an
existing episode are all exercised end to end.

**REQUIRES REAL USER ENVIRONMENT** — the Playwright browser was not
downloadable in the build environment, so the browser has never been pointed
at the real Spotify for Creators site. The selectors, the upload control, the
form fields and the confirmation read-back are written from Spotify's
documentation and from the simulated pages. They have **not** been confirmed
against the live site.

If the live interface differs, the app fails at a stage with a diagnostic
rather than publishing something wrong. That is the designed failure mode, and
it is why `--spotify-smoke-test` exists.

---

## The smoke test

```bat
CHECK_SPOTIFY.bat
```

It opens a **read-only** look at the real site: it signs in if needed, opens
the dashboard, reads the show and the episode list, and reports what it sees.
It does not create, edit, upload or publish anything.

There is no way to reach a real publication from the smoke test. A deliberate
end-to-end attempt needs `--live-publish` **and** an explicit confirmation,
because it is the one operation that cannot be undone.

---

## Sources

- [Publishing videos](https://support.spotify.com/us/creators/article/publishing-videos/)
- [Video specifications](https://support.spotify.com/us/creators/article/video-specs/)
- [Publishing audio episodes](https://support.spotify.com/us/creators/article/publishing-audio-episodes/)
- [Episode chapters](https://support.spotify.com/us/creators/article/episode-chapters/)
- [Formatting your show notes](https://support.spotify.com/us/creators/article/formatting-your-show-notes/)
- [Developer forum: no third-party episode-upload API](https://community.spotify.com/t5/Spotify-for-Developers/Request-for-Access-to-Upload-Podcasts-to-Spotify-from-External/td-p/7044523)
