# The metadata file

Every episode needs a `.txt` file next to its media file. Same base name:

```
Folge_08_Der_Schluessel.mp4
Folge_08_Der_Schluessel.txt     <- this file
```

The app pairs them by name and reads the details from the `.txt`. If there is
no `.txt`, the episode is **not published** — see
[Media-only mode](#media-only-mode) at the end.

---

## Format

One `KEY=value` per line:

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

- Keys are case-insensitive: `title`, `Title` and `TITLE` all work.
- Values are taken literally, so `=` and `:` inside a value are fine.
- `#` at the start of a line is a comment.
- A value may run over several lines; the following lines are appended until
  the next `KEY=`. This is how multi-line descriptions are written.
- `\` is an escape inside values: `\n` is a line break, `\\` is a backslash.
- UTF-8, with or without a BOM. German umlauts and Greek letters are fine.

---

## Fields

### `TITLE` — required

The episode title, exactly as it should appear in your feed.

Not allowed to be empty. Not length-checked against a limit we are not sure
of — if Spotify rejects it, the job fails with Spotify's own message and the
title is shown, rather than being silently shortened.

```
TITLE=Der Schlüssel zum Bewusstsein
```

### `DESCRIPTION` — required

The episode notes. This is the text shown under the episode, and it is what
your listeners read.

```
DESCRIPTION=Eine Reise in die verborgenen Zusammenhänge von Bewusstsein,
Herz und antiker Spiritualität.
```

A value running over several lines is one paragraph with line breaks kept.
An empty line starts a new paragraph:

```
DESCRIPTION=First paragraph.

Second paragraph, separated by a blank line.
```

Notes on links, from Spotify's own documentation:

- Links must start with `https://`.
- Bare `http://` and `www.` links are not turned into links.
- Spotify supports a limited set of HTML tags in show notes. The plain text
  above is always safe; the app does not generate HTML for you.

### `SEASON` and `EPISODE` — optional

A whole number, or blank. `EPISODE` on its own is fine and is the usual case
for a show that is not divided into seasons.

```
SEASON=3
EPISODE=8
```

If you leave `EPISODE` out, Spotify numbers it itself.

### `PUBLISH` — optional

One of exactly three words: `NOW` (the default), `SCHEDULE`, or `DRAFT`.

```
PUBLISH=NOW
```

To publish at a chosen time, use `SCHEDULE` together with `PUBLISH_DATE`,
`PUBLISH_TIME` and — optionally — `TIMEZONE`:

```
PUBLISH=SCHEDULE
PUBLISH_DATE=2026-10-01
PUBLISH_TIME=09:00
TIMEZONE=Europe/Berlin
```

`PUBLISH_TIME` accepts `HH:MM` or `HH:MM:SS`. Without a `TIMEZONE`, the
show's `timezone` from the config is used. **A schedule needs the date and the
time** — `PUBLISH=SCHEDULE` on its own is an error, and so is a schedule
missing either half, because Spotify needs both and guessing one would
schedule an episode for the wrong day.

`DRAFT` saves the episode on Spotify without publishing it. You finish and
publish it yourself in the Spotify for Creators dashboard.

Anything else is refused with the line number, for example
`PUBLISH must be NOW, SCHEDULE or DRAFT, not 'tomorrow'. (line 4)`.

**How far ahead you can schedule.** Spotify's own guidance is that episodes
should be available at least a few hours before you want them published. Very
short notice is accepted by the form but Spotify may publish late or
immediately. If an episode is published with a timestamp in the past, it goes
out at once — the app does not pretend a past date is a future one.

### `EXPLICIT` — optional

`true` or `false`. Defaults to the show's `defaults.explicit`.

```
EXPLICIT=false
```

### `LANGUAGE` — optional

An ISO 639-1 code, usually two letters.

```
LANGUAGE=de
```

### `AUTHOR` — optional

Overrides the show author for this episode only.

```
AUTHOR=Antike Spiritualitaet
```

### `TAGS` — optional

Not shown by Spotify for podcasts; recorded in the database and exported to
the diagnostics so you can find an episode later.

```
TAGS=spiritualitaet, antike, philosophie
```

### `CHAPTERS` — optional

See below.

---

## Chapters

Spotify takes chapters as timestamps inside the description. The format
follows Spotify's own documentation:

- The first timestamp must be `00:00`.
- Timestamps must be in chronological order.
- **There must be at least three chapters.** Fewer is an error, not a
  warning: Spotify accepts the episode but will not show the chapters, and
  the app will not quietly publish something other than what you asked for.
- Leave roughly 30 seconds between them. Less than 30 seconds is a
  *warning* — the episode still publishes, and the deviation is reported.

```
CHAPTERS=
00:00 Intro
02:15 Das vergessene Wissen
07:42 Herz und Bewusstsein
14:05 Der Schlüssel
22:30 Fragen und Antworten
```

Every line after `CHAPTERS=` is one chapter. A blank line ends the list.

`[hh:]mm:ss` and `hh:mm:ss` are both accepted, with or without parentheses:
`(02:15) Das vergessene Wissen` and `02:15 - Das vergessene Wissen` are the
same chapter.

When chapters are present, the app adds the timestamps to the description
itself, so you do not repeat them in `DESCRIPTION`. If you have already typed
them there, it leaves your text alone rather than adding them twice.

If chapters are present but break one of the rules above, the app **refuses
the episode** and says which rule and which line — it does not quietly drop
chapters and publish something else. Nothing is invented or repaired.

To publish the episode *without* chapters, delete the `CHAPTERS` block.

---

## A complete example

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
AUTHOR=Antike Spiritualitaet
TAGS=spiritualitaet, antike, philosophie

CHAPTERS=
00:00 Intro
02:15 Das vergessene Wissen
07:42 Herz und Bewusstsein
14:05 Der Schlüssel
22:30 Fragen und Antworten
```

---

## A minimal, publish-now example

Everything optional left out except what Spotify requires, plus chapters:

```
TITLE=THOT – Der Schlüssel zum Bewusstsein

DESCRIPTION=Eine Reise in die Zusammenhänge von Bewusstsein, Herz und Gehirn.

SEASON=1

EPISODE=1

EXPLICIT=false

PUBLISH=NOW

CHAPTERS=
00:00 Einführung
02:15 Das vergessene Wissen
05:30 Herz und Bewusstsein
```

That file is valid exactly as written: three chapters, the first at `00:00`,
in order, and more than 30 seconds apart. The chapters are sent to Spotify as
three lines in the description:

```
(00:00) Einführung
(02:15) Das vergessene Wissen
(05:30) Herz und Bewusstsein
```

Both examples on this page are parsed and validated by
`tests/test_documentation.py`, so neither can drift away from what the app
actually accepts.

---

## Rejected files

The app refuses, with a message naming the file and the problem:

| Problem | Why |
|---|---|
| `TITLE` missing or empty | There is no title to publish. |
| `DESCRIPTION` missing | Spotify requires episode notes. |
| Neither the title nor the description is present | The file is not metadata. |
| Chapters not starting at `00:00` | Spotify's rule. |
| Fewer than three chapters | Spotify will not display them. |
| Chapters out of order | Spotify's rule. |
| Two chapters with the same timestamp | Ambiguous — we do not guess. |
| A chapter with no title | A timestamp on its own is not a chapter. |
| `PUBLISH` is a date that cannot be read | Typo. |
| `SEASON` / `EPISODE` is not a number | Typo. |
| Two `.txt` files match one media file | Ambiguous — we do not guess. |

Nothing is guessed, nothing is filled in, and the original file is never
changed. The episode is parked with a message and the **Retry** button becomes
useful once you fix the file and drop it in again.

---

## Media-only mode

Off by default, because the title and description are the part you care about
most and a file name makes a poor title.

To turn it on:

```yaml
media:
  allow_media_only: true
```

Then a media file with no `.txt` is published using its file name as the title
and a generated description that says so. A `.txt`, when present, always
wins.

We recommend leaving this off.
