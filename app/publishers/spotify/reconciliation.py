"""Reconciliation - the duplicate-prevention brain.

The failure this defends against is specific and real: the app uploaded a
6 GB file, Spotify accepted it, then the power went out before the app heard
back.  On restart the only honest question is *"does Spotify already have
this?"* - and the only safe answer is to go and look.

Strategy, in order of confidence:

1. **Episode ID we already recorded** - if the job has ``spotify_episode_id``,
   trust it and read that episode back.
2. **Exact title match on the show's episode list** - read from the live page
   when a session is available, otherwise from the SQLite cache written the
   last time we looked.  This is what stops the crash-after-upload case.
3. **Title + season + episode number** - strengthens the match when the user
   numbers their episodes.
4. **Nothing matched** - only then may a publish proceed.

A *false positive* here is expensive (we refuse to publish something new), a
*false negative* is catastrophic (we publish a duplicate).  The matcher is
therefore biased towards caution: normalisation, a time window and an explicit
``allow`` flag for genuinely recurring titles.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from ...core.config import AppConfig, ShowConfig
from ...core.models import PublishMode
from ...security.redaction import redact_text
from ..base import PublishRequest, RemoteEpisode
from .selectors import DASHBOARD

log = logging.getLogger(__name__)

__all__ = ["Reconciler", "MatchDecision", "normalise_title", "looks_like_a_duplicate"]


def normalise_title(title: str) -> str:
    """Fold a title to a comparable form.

    Unicode-normalises, lowercases, removes punctuation and collapses
    whitespace, so ``"THOT – Der Schlüssel!"`` and ``"thot der schlussel"``
    compare equal.  Content words are never dropped.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", str(title))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", " ", text).strip()
    return text


#: Remote states that mean the episode genuinely exists on Spotify.  Anything
#: else (failed, rejected, draft, unknown) is safe to publish again.
_LIVE_REMOTE_STATES = frozenset({"published", "scheduled", "processing", "ready"})


def looks_like_a_duplicate(
    request: PublishRequest,
    candidate: RemoteEpisode,
    *,
    window_hours: int = 72,
    require_numbering: bool = False,
    claimed_by_another: bool = False,
) -> tuple[bool, str]:
    """Decide whether *candidate* is this same episode.

    Returns ``(is_duplicate, reason)``.  The reason is shown to the user, so it
    always explains the decision in plain words.

    ``require_numbering`` tightens the match: the title must match *and* the
    season and episode number must match too.  That is used for a job that
    has never been sent, where a bare title is ambiguous - it could be an
    earlier episode of the show, or a different recording that happens to
    share a title, and silently dropping the new file is the worse mistake.

    ``claimed_by_another`` says the candidate is already recorded against a
    different local job, which is proof that it is not this file's episode.
    """
    if not candidate.title:
        return False, "the Spotify entry has no title"
    # Only a live episode on Spotify is a duplicate.  A rejected or failed
    # entry with the same title must stay publishable, otherwise one bad
    # attempt would block the episode forever.
    state = (candidate.state or "unknown").strip().lower()
    if state and state not in _LIVE_REMOTE_STATES:
        return False, (
            f"the Spotify episode with that title is {state}, not a live "
            f"episode, so it is safe to publish"
        )
    want = normalise_title(request.metadata.title)
    got = normalise_title(candidate.title)
    if want and got and want == got:
        # A title that is being reused for a genuinely new episode later on.
        if request.metadata.publish is PublishMode.SCHEDULE and request.metadata.publish_at:
            if candidate.published_at:
                try:
                    when = candidate.published_at
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when > request.metadata.publish_at + timedelta(hours=window_hours):
                        return False, (
                            "a same-titled Spotify episode exists but it publishes "
                            "after this one"
                        )
                except (TypeError, ValueError):  # pragma: no cover
                    pass
        extra = []
        if (
            request.metadata.season is not None
            and candidate.season is not None
            and request.metadata.season != candidate.season
        ):
            return False, (
                f"a same-titled Spotify episode exists but in season "
                f"{candidate.season} instead of {request.metadata.season}"
            )
        if require_numbering:
            mine = request.metadata
            if (mine.season is None or candidate.season is None
                    or mine.season != candidate.season
                    or mine.episode_number is None or candidate.episode_number is None
                    or mine.episode_number != candidate.episode_number):
                return False, (
                    "Spotify has an episode with the same title, but this job "
                    "has never been uploaded, so a matching season and episode "
                    "number is needed to be sure it is the same recording"
                )
        if claimed_by_another:
            # The title matches, but this remote episode is already recorded
            # against a *different* local job - another file of the same name
            # that was published first.  Adopting it would file this
            # recording away as if it had been published, which is the one
            # failure that loses data rather than merely tidying up.
            return False, (
                f"the Spotify episode titled {candidate.title!r} belongs to "
                f"another episode of yours, not to this file"
            )
        if (
            request.metadata.episode_number is not None
            and candidate.episode_number is not None
            and request.metadata.episode_number != candidate.episode_number
        ):
            extra.append(f"episode number {candidate.episode_number}")
        suffix = f" (also matched on {' and '.join(extra)})" if extra else ""
        return True, f"Spotify already has an episode titled {candidate.title!r}{suffix}"

    if want and got:
        import difflib
        ratio = difflib.SequenceMatcher(None, want, got).ratio()
        if ratio >= 0.92:
            return False, (
                f"the closest Spotify title {redact_text(candidate.title)!r} is "
                f"similar but not identical, so it was treated as a different episode"
            )
    return False, (
        f"no Spotify episode titled {redact_text(request.metadata.title)!r} was found"
    )


@dataclass
class MatchDecision:
    is_duplicate: bool
    candidate: Optional[RemoteEpisode]
    reason: str
    #: "live" | "cache" | "recorded-id" | "title-warning" | "unavailable"
    source: str = "live"
    considered: int = 0

    @property
    def is_unknown(self) -> bool:
        """True when the remote list could not be read at all."""
        return self.source == "unavailable"


class Reconciler:
    def __init__(self, config: AppConfig, database=None):
        self.config = config
        self.database = database

    # -- live reading -------------------------------------------------------
    def list_remote_episodes(
        self,
        *,
        session=None,
        show_config: ShowConfig | None = None,
        limit: int = 60,
    ) -> Optional[list[RemoteEpisode]]:
        """Read the show's episode list from Spotify.

        Returns the episodes, or ``None`` when the list could not be read at
        all.  The difference matters: ``[]`` means "this show has no
        episodes", which is a real answer, while ``None`` means "we do not
        know", and a caller that treats the two alike will happily publish a
        duplicate.  Exceptions are not raised, so an unreachable Spotify
        never blocks the queue.
        """
        if session is None:
            return None
        try:
            if show_config is not None:
                self._open_show(session, show_config)
            rows = self._read_rows(session, limit=limit)
            if not session.count(DASHBOARD["episode_row_any"].all()[0]) and \
                    not any(session.exists(c, timeout_ms=300) for c in DASHBOARD["episode_row_any"].all()):
                for candidate in DASHBOARD["no_episodes"].all():
                    if session.exists(candidate, timeout_ms=400):
                        return []
            return rows
        except Exception as exc:  # noqa: BLE001
            log.warning("[spotify] Could not read the episode list: %s", exc)
            return None

    def _open_show(self, session, show: ShowConfig) -> None:
        """Navigate to the configured show's episode list."""
        target = show.spotify_show_id
        if target:
            url = f"{self.config.spotify.base_url}/pod/dashboard/episodes?show={target}"
            session.goto(url, timeout_ms=self.config.spotify.navigation_timeout_ms)
        else:
            session.goto(self.config.spotify.episodes_url,
                         timeout_ms=self.config.spotify.navigation_timeout_ms)
        session.wait_for(DASHBOARD["episode_row_any"].all()[0], timeout_ms=8000, state="attached")

    def _read_rows(self, session, *, limit: int) -> list[RemoteEpisode]:
        out: list[RemoteEpisode] = []
        for row_selector in DASHBOARD["episode_row_any"].all():
            count = session.count(row_selector)
            if count == 0:
                continue
            titles = session.texts(f'{row_selector} [data-testid="episode-title"]')
            if not titles:
                # Fall back to the row's first cell.  Reading the whole row
                # would fold the status and the date into the title, and that
                # string can never equal the title we are looking for - which
                # would silently disable duplicate detection.
                titles = session.texts(f"{row_selector} td >> nth=0")
            if not titles:
                titles = session.texts(f"{row_selector} >> nth=0")
            states = session.texts(f'{row_selector} [data-testid="episode-status"]') or \
                session.texts(f'{row_selector} [data-episode-status]')
            hrefs = session.attrs(f'{row_selector} a[href*="/episode/"]', "href")
            ids = session.attrs(row_selector, "data-episode-id")
            # The episode list shows the numbering, which is what makes a
            # same-title match decisive rather than a guess.
            seasons = [v for v in session.attrs(row_selector, "data-episode-season") if v]
            numbers = [v for v in session.attrs(row_selector, "data-episode-number") if v]
            for index in range(min(count, limit)):
                title = titles[index] if index < len(titles) else ""
                href = hrefs[index] if index < len(hrefs) else (hrefs[0] if hrefs else "")
                episode_id = ids[index] if index < len(ids) and ids[index] else ""
                if not episode_id and href:
                    match = re.search(r"/episode/([A-Za-z0-9_-]+)", href)
                    episode_id = match.group(1) if match else ""
                if not title and not episode_id:
                    continue
                state_text = states[index] if index < len(states) else ""
                out.append(RemoteEpisode(
                    episode_id=episode_id or f"row-{index}",
                    title=title,
                    state=_classify(state_text) or (state_text or "").lower(),
                    url=href or "",
                    season=_as_int(seasons[index] if index < len(seasons) else None),
                    episode_number=_as_int(
                        numbers[index] if index < len(numbers) else None
                    ),
                ))
            if out:
                break
        return out

    # -- matching -----------------------------------------------------------
    def find_matches(
        self,
        *,
        show_id: str,
        title: str,
        session=None,
        show_config: ShowConfig | None = None,
    ) -> list[RemoteEpisode]:
        """Return remote episodes whose title matches, live data preferred."""
        live = self.list_remote_episodes(session=session, show_config=show_config)
        if live:
            return [ep for ep in live if normalise_title(ep.title) == normalise_title(title)]
        return self.cached_matches(show_id, title)

    def cached_matches(self, show_id: str, title: str) -> list[RemoteEpisode]:
        """Matches from the SQLite cache written the last time we looked."""
        if self.database is None:
            return []
        rows = self.database.find_spotify_episodes_by_title(show_id, title)
        if not rows:
            # Also try a normalised comparison against the cached set.
            want = normalise_title(title)
            rows = [
                row for row in self.database.list_spotify_episodes(show_id, limit=200)
                if normalise_title(row.get("title", "")) == want
            ]
        return [
            RemoteEpisode(
                episode_id=row["episode_id"],
                title=row.get("title", ""),
                state=row.get("state", "unknown"),
                url=row.get("url", "") or "",
                published_at=_parse_iso(row.get("published_at")),
                scheduled_for=_parse_iso(row.get("scheduled_for")),
                duration=row.get("duration") or 0.0,
                season=row.get("season"),
                episode_number=row.get("episode_number"),
                is_explicit=bool(row.get("is_explicit")),
            )
            for row in rows
        ]

    def _claimed_by_another_job(self, job_id: str, candidate: RemoteEpisode) -> bool:
        """Is this remote episode already recorded against a *different* job?

        A title match alone cannot tell this recording from another file that
        happens to carry the same title.  The one piece of hard evidence
        available locally is the episode id another job has already claimed:
        if a different job owns it, this file is not it, and adopting would
        silently swallow the file the user just dropped in.
        """
        if not job_id or not candidate.episode_id or self.database is None:
            return False
        if not candidate.episode_id or candidate.episode_id.startswith("row-"):
            return False
        try:
            rows = self.database.conn.execute(
                "SELECT id FROM jobs WHERE spotify_episode_id = ? AND id != ? "
                "AND retired = 0",
                (candidate.episode_id, job_id),
            ).fetchall()
        except Exception:  # noqa: BLE001 - never fail a publish on a lookup
            return False
        return bool(rows)

    def pick_duplicate(
        self,
        candidates: Sequence[RemoteEpisode],
        *,
        request: PublishRequest,
        session=None,
        require_numbering: bool = False,
        job_id: str = "",
    ) -> Optional[RemoteEpisode]:
        """Return the candidate that is this episode, or ``None``."""
        for candidate in candidates:
            is_dup, reason = looks_like_a_duplicate(
                request, candidate, require_numbering=require_numbering,
                claimed_by_another=self._claimed_by_another_job(job_id, candidate),
            )
            log.info(
                "[spotify] Duplicate check for %r against %r: %s",
                request.metadata.title, candidate.title,
                "MATCH - " + reason if is_dup else "no match - " + reason,
            )
            if is_dup:
                return candidate
        return None

    def decide(
        self,
        *,
        request: PublishRequest,
        session=None,
        show_config: ShowConfig | None = None,
        recorded_episode_id: str = "",
        title_match_is_sufficient: bool = True,
    ) -> MatchDecision:
        """Full decision, with the reasons a human can audit.

        ``title_match_is_sufficient`` is False for a job that has never
        reached Spotify.  A title alone cannot tell this recording apart from
        an earlier episode the user published by hand, and silently adopting
        it would drop the new file.  In that case a match is reported as a
        *possible* duplicate and the caller decides.
        """
        title = request.metadata.title
        show_id = request.show_key
        if recorded_episode_id:
            known = RemoteEpisode(episode_id=recorded_episode_id, title=title, state="recorded")
            return MatchDecision(True, known, "this job already recorded a Spotify episode ID",
                                 source="recorded-id")
        # A job that has never been sent needs a *decisive* match: same title
        # and the same season and episode number.  A bare title could be an
        # earlier episode of the show or a different recording that reuses
        # the title, and silently dropping the new file is the worse mistake.
        strict = not title_match_is_sufficient

        live = self.list_remote_episodes(session=session, show_config=show_config)
        if live is None:
            live = []
        if live:
            source = "live"
            candidates = [
                ep for ep in live if normalise_title(ep.title) == normalise_title(title)
            ]
        else:
            source = "cache"
            candidates = self.cached_matches(show_id, title)
        job_id = getattr(request, "job_id", "") or ""
        match = self.pick_duplicate(candidates, request=request, session=session,
                                    require_numbering=strict, job_id=job_id)
        if match is not None:
            _, reason = looks_like_a_duplicate(
                request, match, require_numbering=strict,
                claimed_by_another=self._claimed_by_another_job(job_id, match),
            )
            return MatchDecision(True, match, reason, source=source, considered=len(candidates))
        if strict and candidates:
            # The titles line up but the numbering does not prove it is the
            # same recording.  Say so, and let the episode be published
            # rather than dropped without a trace.
            return MatchDecision(
                False, None,
                (f"Spotify has {len(candidates)} episode(s) titled "
                 f"{candidates[0].title!r}, but the season or episode number "
                 "does not match, so this is treated as a different recording"),
                source="title-warning", considered=len(candidates),
            )
        if source == "unavailable":
            # Nothing was actually checked.  Saying "no match" here would be
            # a false reassurance on exactly the path that prevents a
            # duplicate, so the caller is told the truth instead: unknown.
            return MatchDecision(
                False, None,
                "the Spotify episode list could not be read, so it is unknown "
                "whether this episode already exists",
                source="unavailable", considered=0,
            )
        return MatchDecision(
            False, None,
            f"no matching episode exists on Spotify ({len(candidates)} title match(es) checked)",
            source=source, considered=len(candidates),
        )

    # -- persistence --------------------------------------------------------
    def cache(self, show_id: str, episodes: Iterable[RemoteEpisode]) -> None:
        if self.database is None:
            return
        for episode in episodes:
            if not episode.episode_id:
                continue
            self.database.upsert_spotify_episode(show_id, episode.as_dict())
        self.database.set_spotify_last_checked(show_id)

    def adopt(self, show_id: str, episode: RemoteEpisode) -> None:
        if self.database is not None and episode.episode_id:
            self.database.upsert_spotify_episode(show_id, episode.as_dict())


def _classify(text: str) -> str:
    lowered = (text or "").strip().lower()
    for needle, value in (
        ("published", "published"), ("live", "published"),
        ("scheduled", "scheduled"), ("draft", "draft"),
        ("processing", "processing"), ("transcoding", "processing"),
        ("uploading", "uploading"),
    ):
        if needle in lowered:
            return value
    return ""


def _as_int(value) -> Optional[int]:
    """Read a numbering field that a page may render as an empty string."""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
