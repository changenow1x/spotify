"""Spotify adapter: selectors, payload mapping and duplicate reconciliation.

No browser and no network are involved.  The selector registry is checked for
internal consistency, the mapping is checked against what Spotify documents,
and the duplicate heuristics are exercised directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.models import Chapter, EpisodeMetadata, PublishMode
from app.publishers.base import (
    AuthStatus,
    PublicationStatus,
    PublishRequest,
    RemoteEpisode,
)
from app.publishers.spotify.mapping import (
    SpotifyEpisodePayload,
    build_description,
    check_payload_supported,
    map_metadata_to_spotify,
    supported_summary,
)
from app.publishers.spotify.reconciliation import (
    Reconciler,
    looks_like_a_duplicate,
    normalise_title,
)
from app.publishers.spotify.selectors import (
    get,
    probe_targets,
    selector_names,
    validate_registry,
)

TITLE = "Episode 12 – The One With Long Titles"


def _meta(**over) -> EpisodeMetadata:
    base = dict(
        title=TITLE,
        description="A description that mentions https://example.com/x.",
        season=2,
        episode_number=12,
        explicit=True,
        publish=PublishMode.NOW,
        chapters=[
            Chapter(0, "Intro", "00:00"),
            Chapter(90, "Main topic", "01:30"),
            Chapter(240, "Outro", "04:00"),
        ],
    )
    base.update(over)
    return EpisodeMetadata(**base)


# -------------------------------------------------------------- selectors

class TestSelectorRegistry:
    def test_every_selector_resolves(self):
        for name in selector_names():
            s = get(name)
            assert s.candidates, f"{name} has no selector candidates"
            assert s.description, f"{name} has no description"

    def test_primary_is_the_first_candidate(self):
        for name in selector_names():
            assert get(name).primary() == get(name).candidates[0], name

    def test_fallback_lists_are_deduplicated(self):
        for name in selector_names():
            s = get(name)
            assert len(s.candidates) == len(set(s.candidates)), name

    def test_no_coordinate_clicking(self):
        """Selectors are DOM/role/label based; x/y would break on any layout."""
        for name in selector_names():
            for selector in get(name).candidates:
                assert "nth-child" not in selector, name
                assert not selector.strip().startswith("x="), name

    def test_registry_is_self_consistent(self):
        assert validate_registry() == []

    def test_unknown_selector_raises(self):
        with pytest.raises(KeyError):
            get("definitely-not-a-real-selector")

    def test_probe_targets_are_well_formed(self):
        targets = probe_targets()
        assert len(targets) == len(selector_names())
        for page, name, selector in targets:
            assert page
            assert name in selector_names(), f"{page}/{name}"
            assert selector == get(name).primary()
            assert selector in get(name).candidates

    def test_key_fields_all_have_selectors(self):
        names = set(selector_names())
        for required in ("title_input", "description_input", "season_input",
                         "episode_number_input", "explicit_toggle",
                         "publish_now_radio", "publish_schedule_radio",
                         "schedule_date_input", "schedule_time_input",
                         "file_input", "publish_button"):
            assert required in names, f"missing selector: {required}"

    def test_every_selector_needed_by_a_payload_exists(self):
        """A rename in the mapping must not silently point at nothing."""
        payload = map_metadata_to_spotify(_meta())
        for field, selector_name in payload.targets.items():
            assert selector_name in selector_names(), f"{field} -> {selector_name}"

    def test_wait_states_are_playwright_states(self):
        for name in selector_names():
            assert get(name).state in {"visible", "attached", "hidden", "detached"}


# ---------------------------------------------------------------- mapping

class TestPayloadMapping:
    def test_scalar_fields(self):
        payload = map_metadata_to_spotify(_meta())
        assert payload.title == TITLE
        assert payload.season == 2
        assert payload.episode_number == 12
        assert payload.explicit is True

    def test_unicode_survives(self):
        title = "Überschrift mit Ümlauten – 日本語 – emoji 🎧"
        assert map_metadata_to_spotify(_meta(title=title)).title == title

    def test_chapters_are_appended_to_the_description(self):
        payload = map_metadata_to_spotify(_meta())
        for stamp in ("00:00", "01:30", "04:00"):
            assert stamp in payload.description
        assert "Intro" in payload.description

    def test_chapters_can_be_left_out(self):
        payload = map_metadata_to_spotify(_meta(), include_chapters=False)
        assert "Intro" not in payload.description
        assert "A description" in payload.description

    def test_publish_now(self):
        payload = map_metadata_to_spotify(_meta(publish=PublishMode.NOW))
        assert payload.publish_now is True
        assert payload.schedule_date == "" and payload.schedule_time == ""

    def test_schedule_uses_the_configured_timezone(self):
        when = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
        payload = map_metadata_to_spotify(
            _meta(publish=PublishMode.SCHEDULE, publish_at=when, timezone="Europe/Berlin"))
        assert payload.publish_now is False
        # 08:00 UTC is 09:00 in Berlin in March (CET, +1).
        assert payload.schedule_date == "2026-03-01"
        assert payload.schedule_time == "09:00"
        assert payload.schedule_utc == when
        assert "Berlin" in payload.schedule_local_label or "CET" in payload.schedule_local_label

    def test_fields_spotify_cannot_hold_are_reported_not_silently_dropped(self):
        payload = map_metadata_to_spotify(
            _meta(author="Alex", language="de", artwork_path="cover.png"))
        assert payload.local_only["author"] == "Alex"
        assert any("AUTHOR" in w for w in payload.warnings)
        assert any("ARTWORK_PATH" in w for w in payload.warnings)

    def test_surrounding_blanks_are_removed_but_announced(self):
        payload = map_metadata_to_spotify(_meta(title="  Padded title  "))
        assert payload.title == "Padded title"
        assert any("TITLE" in w for w in payload.warnings)

    def test_inner_text_is_never_rewritten(self):
        raw = "  Ein Podcast über „Zeug“ – Teil 3/4  "
        payload = map_metadata_to_spotify(_meta(title=raw))
        assert payload.title == raw.strip()
        assert "„Zeug“" in payload.title and "3/4" in payload.title

    def test_payload_is_serialisable(self):
        data = map_metadata_to_spotify(_meta()).as_dict()
        json.dumps(data)  # must not raise

    def test_describe_is_human_readable(self):
        text = map_metadata_to_spotify(_meta()).describe()
        assert "season=2" in text and "episode=12" in text


class TestDescriptionBuilding:
    def test_chapter_order_is_preserved(self):
        text = build_description(_meta())
        assert text.index("Intro") < text.index("Main topic") < text.index("Outro")

    def test_no_chapters_leaves_the_description_alone(self):
        text = build_description(_meta(chapters=[]))
        assert text.strip() == _meta().description

    def test_chapters_can_be_suppressed(self):
        text = build_description(_meta(), append_chapters=False)
        assert "Intro" not in text

    def test_description_text_is_never_truncated(self):
        long_text = "word " * 500
        assert build_description(_meta(description=long_text)).startswith(long_text.strip())


class TestPayloadSupport:
    def test_normal_payload_is_supported(self):
        assert check_payload_supported(map_metadata_to_spotify(_meta())) == []

    def test_empty_title_is_blocked(self):
        payload = map_metadata_to_spotify(_meta())
        payload.title = ""
        assert any("title" in p.lower() for p in check_payload_supported(payload))

    def test_empty_description_is_blocked(self):
        payload = map_metadata_to_spotify(_meta())
        payload.description = "   "
        assert any("description" in p.lower() for p in check_payload_supported(payload))

    def test_schedule_without_a_time_is_blocked(self):
        payload = map_metadata_to_spotify(_meta())
        payload.publish_now = False
        payload.schedule_date = ""
        problems = check_payload_supported(payload)
        assert any("scheduled" in p.lower() for p in problems)

    def test_supported_summary_is_documentation_not_a_secret(self):
        summary = supported_summary()
        json.dumps(summary)
        assert summary


# ---------------------------------------------------------- reconciliation

def _request(**over) -> PublishRequest:
    base = dict(
        show_key="test-show",
        show_name="Test Show",
        media_path=Path("/incoming/ep01.mp4"),
        metadata=_meta(),
        job_id="job-1",
    )
    base.update(over)
    return PublishRequest(**base)


def _remote(**over) -> RemoteEpisode:
    base = dict(
        episode_id="remote-1",
        title=TITLE,
        state="published",
        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
        season=2,
        episode_number=12,
    )
    base.update(over)
    return RemoteEpisode(**base)


class TestTitleNormalisation:
    @pytest.mark.parametrize("left,right", [
        ("Episode 1", "episode 1"),
        ("Episode  1", "Episode 1"),
        ("  Episode 1  ", "Episode 1"),
        ("Episode 1", "Episode 1"),
    ])
    def test_folding(self, left, right):
        assert normalise_title(left) == normalise_title(right)

    def test_different_titles_stay_different(self):
        assert normalise_title("Episode 1") != normalise_title("Episode 2")

    def test_empty_is_safe(self):
        assert normalise_title("") == ""


class TestDuplicateHeuristics:
    def test_same_title_and_number_is_a_duplicate(self):
        is_dup, reason = looks_like_a_duplicate(_request(), _remote())
        assert is_dup is True
        assert TITLE in reason

    def test_different_title_is_not(self):
        is_dup, _ = looks_like_a_duplicate(
            _request(), _remote(title="Something else entirely"))
        assert is_dup is False

    @pytest.mark.parametrize("state", ["failed", "rejected", "draft", "error"])
    def test_a_dead_remote_episode_never_blocks_a_republish(self, state):
        is_dup, reason = looks_like_a_duplicate(_request(), _remote(state=state))
        assert is_dup is False, reason
        assert "safe to publish" in reason

    def test_unknown_state_is_not_treated_as_live(self):
        is_dup, _ = looks_like_a_duplicate(_request(), _remote(state="unknown"))
        assert is_dup is False

    @pytest.mark.parametrize("state", ["published", "scheduled", "processing"])
    def test_live_states_do_count(self, state):
        is_dup, _ = looks_like_a_duplicate(_request(), _remote(state=state))
        assert is_dup is True

    def test_same_title_different_season_is_not(self):
        is_dup, reason = looks_like_a_duplicate(_request(), _remote(season=1))
        assert is_dup is False
        assert "season" in reason.lower()

    def test_same_title_different_episode_number_is_still_a_duplicate(self):
        """Two episodes can share a title; a number clash is reported."""
        is_dup, reason = looks_like_a_duplicate(_request(), _remote(episode_number=11))
        assert is_dup is True
        assert "episode number 11" in reason

    def test_a_scheduled_remake_in_the_future_is_not_a_duplicate(self):
        request = _request(metadata=_meta(
            publish=PublishMode.SCHEDULE,
            publish_at=datetime(2026, 3, 1, tzinfo=timezone.utc)))
        is_dup, reason = looks_like_a_duplicate(request, _remote(
            published_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        assert is_dup is False
        assert "publishes after" in reason

    def test_a_near_identical_title_is_reported_but_not_blocked(self):
        """A typo must not silently become a duplicate - and must not crash."""
        is_dup, reason = looks_like_a_duplicate(
            _request(), _remote(title="Episodes 12 – The One With Long Title"))
        assert is_dup is False
        assert "similar" in reason or "no Spotify episode" in reason

    def test_missing_remote_title_is_never_a_duplicate(self):
        is_dup, reason = looks_like_a_duplicate(_request(), _remote(title=""))
        assert is_dup is False and reason

    def test_reason_never_contains_a_secret(self):
        from app.security.redaction import REDACTION

        _, reason = looks_like_a_duplicate(
            _request(metadata=_meta(title="password=hunter2")), _remote())
        assert "hunter2" not in reason or REDACTION in reason


class TestReconcilerWithoutDatabase:
    """Without a cache the reconciler must degrade quietly, not crash."""

    def test_cached_matches_are_empty(self, config):
        assert Reconciler(config).cached_matches("test-show", TITLE) == []

    def test_cache_is_a_no_op(self, config):
        Reconciler(config).cache("test-show", [_remote()])  # must not raise

    def test_decide_without_a_cache_is_not_a_duplicate(self, config):
        decision = Reconciler(config).decide(request=_request())
        assert decision.is_duplicate is False


class TestReconcilerWithCache:
    @pytest.fixture
    def rec(self, config, database):
        database.upsert_spotify_show("test-show", "Test Show", "abc")
        return Reconciler(config, database)

    def test_cached_matches_by_title(self, rec):
        rec.cache("test-show", [_remote()])
        assert [m.episode_id for m in rec.cached_matches("test-show", TITLE)] == ["remote-1"]

    def test_cache_lookup_is_folded(self, rec):
        rec.cache("test-show", [_remote()])
        assert rec.cached_matches("test-show", "EPISODE 12 - the one with long titles!")

    def test_cached_fields_survive_the_round_trip(self, rec):
        rec.cache("test-show", [_remote(season=2, episode_number=12, url="https://s/x")])
        found = rec.cached_matches("test-show", TITLE)[0]
        assert found.season == 2 and found.episode_number == 12
        assert found.url == "https://s/x"
        assert found.published_at is not None

    def test_shows_do_not_share_a_cache(self, rec):
        rec.cache("test-show", [_remote()])
        assert rec.cached_matches("other-show", TITLE) == []

    def test_untitled_remotes_are_never_cached(self, rec):
        rec.cache("test-show", [RemoteEpisode(episode_id="", title=TITLE)])
        assert rec.cached_matches("test-show", TITLE) == []

    def test_decide_adopts_a_cached_duplicate(self, rec):
        rec.cache("test-show", [_remote()])
        decision = rec.decide(request=_request())
        assert decision.is_duplicate is True
        assert decision.candidate.episode_id == "remote-1"

    def test_decide_proceeds_when_nothing_matches(self, rec):
        rec.cache("test-show", [_remote()])
        decision = rec.decide(request=_request(metadata=_meta(title="Brand new one")))
        assert decision.is_duplicate is False
        assert decision.candidate is None
        assert "no matching episode" in decision.reason.lower()
        assert decision.considered == 0

    def test_decide_respects_a_recorded_episode_id(self, rec):
        """Once we know the remote id, we adopt it without a title search."""
        rec.cache("test-show", [_remote(episode_id="recorded-9")])
        decision = rec.decide(request=_request(), recorded_episode_id="recorded-9")
        assert decision.is_duplicate is True

    def test_cache_survives_a_restart(self, config, database):
        database.upsert_spotify_show("test-show", "Test Show", "abc")
        Reconciler(config, database).cache("test-show", [_remote()])
        fresh = Reconciler(config, database)
        assert fresh.cached_matches("test-show", TITLE)

    def test_pick_duplicate_returns_none_when_nothing_matches(self, config):
        assert Reconciler(config).pick_duplicate(
            [_remote(title="Unrelated")], request=_request()) is None

    def test_pick_duplicate_finds_the_right_one(self, config):
        chosen = Reconciler(config).pick_duplicate(
            [_remote(episode_id="a", title="Unrelated"), _remote()],
            request=_request())
        assert chosen.episode_id == "remote-1"

    def test_adopt_records_the_episode(self, rec, database):
        rec.adopt("test-show", _remote(episode_id="adopted-1"))
        assert database.find_spotify_episode("test-show", "adopted-1") is not None


# ------------------------------------------------------- status semantics

class TestStatusVocabulary:
    def test_the_six_outcomes_are_distinct(self):
        values = [s.value for s in PublicationStatus]
        assert len(values) == len(set(values))
        for expected in ("PUBLISHED", "SCHEDULED", "UPLOADED", "PROCESSING",
                         "FAILED", "UNKNOWN", "DUPLICATE", "DRAFT"):
            assert expected in values, f"missing status: {expected}"

    def test_unknown_is_never_published(self):
        assert PublicationStatus.UNKNOWN.value != PublicationStatus.PUBLISHED.value

    def test_auth_status_vocabulary(self):
        values = {s.value for s in AuthStatus}
        assert "CONNECTED" in values
        assert {"NOT_CONNECTED", "EXPIRED", "NEEDS_VERIFICATION"} <= values
        assert "UNKNOWN" in values

    def test_remote_episode_is_serialisable(self):
        json.dumps(_remote().as_dict())
