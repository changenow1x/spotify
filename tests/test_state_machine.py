"""The job state machine.

This module is the single source of truth for what a job *means*, so the tests
are exhaustive: every state is checked for its transitions, its recovery
behaviour, its user-facing label and its hint.
"""

from __future__ import annotations

import pytest

from app.core.state_machine import (
    ACTIVE_STATES,
    RETRYABLE_STATES,
    STATE_HINTS,
    STATE_LABELS,
    STATES_AFTER_REMOTE_CREATE,
    SUCCESS_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    JobState,
    can_transition,
    is_active,
    is_success,
    is_terminal,
    needs_remote_check,
    recover_state,
    validate_transition,
)

ALL = list(JobState)


# ------------------------------------------------------------- invariants

class TestStateTable:
    def test_every_state_has_a_transition_entry(self):
        assert set(TRANSITIONS) == set(ALL)

    def test_every_state_has_a_label(self):
        assert set(STATE_LABELS) == set(ALL)

    def test_labels_are_plain_language(self):
        for state, label in STATE_LABELS.items():
            assert label and label == label.strip()
            assert label == label[0].upper() + label[1:], state

    def test_every_transition_target_is_a_real_state(self):
        for src, targets in TRANSITIONS.items():
            assert src in ALL
            for dst in targets:
                assert dst in ALL, f"{src} -> {dst}"

    def test_no_state_transitions_to_itself_unless_asked(self):
        for src, targets in TRANSITIONS.items():
            assert src not in targets, f"{src} -> {src} is never meaningful"

    def test_state_groups_are_disjoint_where_they_should_be(self):
        assert not (SUCCESS_STATES & {"UPLOADING"})
        assert SUCCESS_STATES <= TERMINAL_STATES
        assert not (TERMINAL_STATES & ACTIVE_STATES)
        assert STATES_AFTER_REMOTE_CREATE & SUCCESS_STATES


# ------------------------------------------------------------ transitions

class TestTransitions:
    def test_happy_path(self):
        path = [
            JobState.DISCOVERED, JobState.VALIDATING, JobState.VALIDATED,
            JobState.QUEUED, JobState.AUTHENTICATING, JobState.UPLOADING,
            JobState.PROCESSING, JobState.READY_TO_PUBLISH, JobState.PUBLISHING,
            JobState.PUBLISHED, JobState.ARCHIVING, JobState.ARCHIVED,
        ]
        for src, dst in zip(path, path[1:]):
            assert can_transition(src, dst), f"{src} -> {dst} must be allowed"
            validate_transition(src, dst)

    def test_every_state_can_reach_a_terminal_state(self):
        """No state may be a dead end that requires manual database surgery."""
        reachable = {JobState.PUBLISHED, JobState.SCHEDULED, JobState.FAILED,
                     JobState.CANCELLED}
        for start in ALL:
            if is_terminal(start):
                continue  # already finished; nothing to reach
            seen, frontier = set(), [start]
            while frontier:
                node = frontier.pop()
                if node in seen:
                    continue
                seen.add(node)
                if node in reachable:
                    break
                frontier.extend(TRANSITIONS.get(node, ()))
            assert seen & reachable, f"{start.value} can never finish"

    def test_archived_is_absolutely_final(self):
        assert TRANSITIONS[JobState.ARCHIVED] == frozenset()

    def test_illegal_transition_raises_with_both_states(self):
        with pytest.raises(IllegalTransition) as exc:
            validate_transition(JobState.DISCOVERED, JobState.PUBLISHED)
        assert "DISCOVERED" in str(exc.value) and "PUBLISHED" in str(exc.value)
        assert exc.value.as_dict() == {
            "error": "illegal_transition", "from": "DISCOVERED", "to": "PUBLISHED"}

    def test_can_transition_accepts_plain_strings(self):
        assert can_transition("QUEUED", "AUTHENTICATING") is True
        assert can_transition("ARCHIVED", "QUEUED") is False

    def test_unknown_state_is_rejected(self):
        with pytest.raises(ValueError):
            can_transition("NOT_A_STATE", "QUEUED")

    def test_publishing_can_fail_or_need_reconciliation(self):
        for dst in (JobState.RETRY_WAIT, JobState.FAILED, JobState.NEEDS_USER_ACTION,
                    JobState.CANCELLED, JobState.NEEDS_RECONCILIATION):
            assert can_transition(JobState.PUBLISHING, dst)

    def test_published_can_only_move_onwards(self):
        for dst in (JobState.ARCHIVING, JobState.FAILED, JobState.NEEDS_RECONCILIATION):
            assert can_transition(JobState.PUBLISHED, dst)
        assert not can_transition(JobState.PUBLISHED, JobState.UPLOADING)
        assert not can_transition(JobState.PUBLISHED, JobState.PUBLISHING)

    def test_scheduled_may_become_published_when_the_time_arrives(self):
        assert can_transition(JobState.SCHEDULED, JobState.PUBLISHED)

    def test_failure_never_loses_the_episode(self):
        """From FAILED the job must still be able to reach PUBLISHED."""
        assert can_transition(JobState.FAILED, JobState.PUBLISHED)

    def test_cancelled_is_nearly_final(self):
        assert can_transition(JobState.CANCELLED, JobState.QUEUED)
        assert can_transition(JobState.CANCELLED, JobState.RETRY_WAIT)
        assert not can_transition(JobState.CANCELLED, JobState.PUBLISHING)


# ----------------------------------------------------------- classifications

class TestClassifications:
    @pytest.mark.parametrize("state", sorted(SUCCESS_STATES, key=lambda s: s.value))
    def test_success_states(self, state):
        assert is_success(state) and is_terminal(state)

    @pytest.mark.parametrize("state", sorted(ACTIVE_STATES, key=lambda s: s.value))
    def test_active_states(self, state):
        assert is_active(state) and not is_terminal(state)

    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
    def test_terminal_states(self, state):
        assert is_terminal(state) and not is_active(state)

    def test_every_state_is_classified_one_way(self):
        for state in ALL:
            assert sum([is_terminal(state), is_active(state)]) <= 1

    @pytest.mark.parametrize("state", sorted(STATES_AFTER_REMOTE_CREATE,
                                             key=lambda s: s.value))
    def test_states_that_may_already_have_a_remote_episode(self, state):
        assert needs_remote_check(state) is True

    def test_states_before_the_upload_never_need_a_remote_check(self):
        for state in (JobState.DISCOVERED, JobState.VALIDATING,
                      JobState.VALIDATED, JobState.QUEUED, JobState.RETRY_WAIT):
            assert needs_remote_check(state) is False

    def test_retryable_states(self):
        assert JobState.FAILED in RETRYABLE_STATES
        assert JobState.ARCHIVED not in RETRYABLE_STATES


# --------------------------------------------------------------- recovery

class TestRecovery:
    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
    def test_terminal_states_are_left_alone(self, state):
        assert recover_state(state) is state

    @pytest.mark.parametrize("state", [
        JobState.UPLOADING, JobState.PROCESSING, JobState.READY_TO_PUBLISH,
        JobState.PUBLISHING, JobState.ARCHIVING, JobState.AUTHENTICATING,
    ])
    def test_mid_remote_create_always_asks_spotify_first(self, state):
        """The one rule that makes duplicate publication impossible."""
        assert recover_state(state) is JobState.NEEDS_RECONCILIATION

    @pytest.mark.parametrize("state", [
        JobState.DISCOVERED, JobState.VALIDATING, JobState.VALIDATED,
        JobState.QUEUED, JobState.RETRY_WAIT,
    ])
    def test_early_states_resume_normally(self, state):
        assert recover_state(state) is JobState.RETRY_WAIT

    def test_recovery_is_idempotent(self):
        """Two restarts must not escalate a job into manual intervention."""
        for state in ALL:
            once = recover_state(state)
            assert recover_state(once) is once, state

    def test_recovery_reaches_a_fixed_point(self):
        for state in ALL:
            current = state
            for _ in range(5):
                nxt = recover_state(current)
                if nxt is current:
                    break
                current = nxt
            assert recover_state(current) is current

    def test_recovery_never_loses_a_published_episode(self):
        for state in ALL:
            target = recover_state(state)
            if is_success(state):
                assert is_success(target), state

    def test_recovery_never_lands_on_a_state_that_needs_a_remote_check_when_nothing_was_sent(self):
        for state in (JobState.DISCOVERED, JobState.VALIDATING, JobState.VALIDATED,
                      JobState.QUEUED, JobState.RETRY_WAIT):
            assert not needs_remote_check(recover_state(state))


# ----------------------------------------------------------------- labels

class TestUserFacingText:
    def test_hints_only_exist_for_states_that_need_explaining(self):
        for state in STATE_HINTS:
            assert state in ALL
        assert JobState.PUBLISHED in STATE_HINTS
        assert JobState.NEEDS_RECONCILIATION in STATE_HINTS

    def test_hints_are_encouraging_not_technical(self):
        for state, hint in STATE_HINTS.items():
            assert STATE_LABELS[state] not in hint
            assert len(hint) > 20

    def test_unknown_state_names_are_rejected(self):
        with pytest.raises(ValueError):
            JobState("SOMETHING_ELSE")
