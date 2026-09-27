"""Secret redaction: nothing sensitive may reach a log, file or the dashboard."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.security.redaction import (
    REDACTION,
    contains_sensitive_key,
    redact_mapping,
    redact_text,
    scrub_exception,
)


class TestRedactText:
    @pytest.mark.parametrize("text,forbidden", [
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ("authorization=Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef",
         "eyJhbGciOiJIUzI1NiJ9"),
        ("Cookie: session_token=abc123; other=1", "session_token=abc123"),
        ("Set-Cookie: spotify_session=deadbeef; Path=/", "deadbeef"),
        ("password=hunter2hunter2", "hunter2hunter2"),
        ("https://user:supersecret@creators.spotify.com/x", "supersecret"),
        ("refresh_token=AQABCdefGHIjklMNOpqrSTUvwxYZ0123456789",
         "AQABCdefGHIjklMNOpqrSTUvwxYZ0123456789"),
        ("a" * 80, None),
    ])
    def test_credentials_are_masked(self, text, forbidden):
        out = redact_text(text)
        assert REDACTION in out
        if forbidden:
            assert forbidden not in out

    def test_long_hex_is_masked(self):
        digest = "d" * 64
        assert digest not in redact_text(f"hash={digest}")

    def test_ordinary_text_untouched(self):
        text = "Detected episode THOT - Der Schluessel in the incoming folder."
        assert redact_text(text) == text

    def test_german_text_untouched(self):
        text = "Überprüfung abgeschlossen: Schlüssel gefunden – Größe 4 GB"
        assert redact_text(text) == text

    def test_truncation(self):
        out = redact_text("x" * 10_000, max_len=100)
        assert "truncated" in out
        assert len(out) < 200

    def test_empty(self):
        assert redact_text("") == ""


class TestRedactMapping:
    def test_sensitive_keys_masked(self):
        data = {
            "user": "max@example.com",
            "password": "hunter2",
            "access_token": "AQB...",
            "cookie": "session=1",
            "authorization": "Bearer x",
            "nested": {"api_key": "k", "title": "Folge 1"},
        }
        out = redact_mapping(data)
        assert out["user"] == "max@example.com"
        assert out["password"] == REDACTION
        assert out["access_token"] == REDACTION
        assert out["cookie"] == REDACTION
        assert out["authorization"] == REDACTION
        assert out["nested"]["api_key"] == REDACTION
        assert out["nested"]["title"] == "Folge 1"

    def test_lists_are_walked(self):
        out = redact_mapping([{"password": "x"}, {"ok": "y"}])
        assert out[0]["password"] == REDACTION
        assert out[1]["ok"] == "y"

    def test_max_depth_guard(self):
        deep = current = {}
        for _ in range(40):
            current["n"] = {}
            current = current["n"]
        out = redact_mapping(deep)
        assert "max-depth" in str(out)


def test_contains_sensitive_key():
    assert contains_sensitive_key(["password"])
    assert contains_sensitive_key(["REFRESH_TOKEN"])
    assert not contains_sensitive_key(["title", "state"])


def test_scrub_exception():
    class Boom(RuntimeError):
        pass

    out = scrub_exception(Boom("failed with Bearer sekrit-token-123456"))
    assert REDACTION in out
    assert "sekrit-token-123456" not in out
    assert out.startswith("Boom:")


class TestLogsNeverContainSecrets:
    def test_error_column_is_redacted(self, config, database):
        database.log_event(
            "job1", None, "error",
            "Login failed for Bearer eyJhbGciOiJI.eyJzdWIiOiIx.SECRETSIGNATURE",
        )
        rows = database.conn.execute(
            "SELECT message FROM job_events WHERE job_id='job1'").fetchall()
        assert "SECRETSIGNATURE" not in rows[0][0]
        assert REDACTION in rows[0][0]

    def test_audit_detail_is_redacted(self, config, database):
        database.audit("test", detail="cookie=abcdef1234567890")
        row = database.conn.execute("SELECT detail FROM audit_log").fetchone()
        assert "abcdef1234567890" not in row[0]

    def test_logging_filter_redacts(self, tmp_path):
        import logging

        from app.core.logging_setup import RedactingFilter

        target = tmp_path / "out.log"
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.addFilter(RedactingFilter())
        logger = logging.getLogger("redaction-test")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("token=%s", "AQSuperSecretTokenValue1234567890")
        handler.flush()
        content = target.read_text(encoding="utf-8")
        assert "AQSuperSecretTokenValue1234567890" not in content
        assert REDACTION in content


class TestRedactingFilterMechanics:
    """Regression tests for ordering bugs that silently leak or crash logging."""

    TOKEN = "AQBSTRonglySecretSpotifyTokenValue0123456789abcdef"

    def test_percent_formatting_still_works(self, tmp_path):
        """Redaction must not eat %s placeholders out of the format string."""
        import logging

        from app.core.logging_setup import RedactingFilter

        target = tmp_path / "fmt.log"
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.addFilter(RedactingFilter())
        logger = logging.getLogger("fmt-test")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("uploading %s to show %d", "ep12.mp4", 7)

        assert target.read_text(encoding="utf-8").strip() == (
            "uploading ep12.mp4 to show 7"
        )

    def test_interpolated_arguments_are_redacted(self, tmp_path):
        """A secret passed as a %s argument must not reach the file."""
        import logging

        from app.core.logging_setup import RedactingFilter

        target = tmp_path / "args.log"
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.addFilter(RedactingFilter())
        logger = logging.getLogger("args-test")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("token=%s", self.TOKEN)

        content = target.read_text(encoding="utf-8")
        assert self.TOKEN not in content
        assert "token=***REDACTED***" in content

    def test_mapping_arguments_are_redacted(self, tmp_path):
        import logging

        from app.core.logging_setup import RedactingFilter

        target = tmp_path / "map.log"
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.addFilter(RedactingFilter())
        logger = logging.getLogger("map-test")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("auth=%(token)s", {"token": self.TOKEN})

        assert self.TOKEN not in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Diagnostics are captured while the user is signed in, so the page on disk
# must never carry a session value.
# ---------------------------------------------------------------------------

class TestDiagnosticsNeverLeakASession:
    def test_captured_html_is_scrubbed_before_it_is_written(self, tmp_path):
        """A diagnostic captured on a signed-in page must not keep a token."""
        from app.publishers.spotify.browser import PlaywrightSession, SessionOptions

        session = PlaywrightSession.__new__(PlaywrightSession)
        session.options = SessionOptions(diagnostics_dir=tmp_path)
        session.content = lambda: (
            "<html><body>"
            "<script>var access_token='AQDLeELK9x0mVb3nQrStUvWxYz0123456789ABCDEFG';</script>"
            '<meta name="cookie" content="sp_session=abc123def456; other=1">'
            "<p>the upload button did not appear</p>"
            "</body></html>"
        )
        session.screenshot = lambda path: None
        session.url = lambda: "https://creators.spotify.com/pod/dashboard/episode/new"

        info = session.capture("upload-missing", reason="selector not found")
        assert info.get("html"), "the capture produced no file"

        written = Path(info["html"]).read_text(encoding="utf-8")
        assert "AQDLeELK9x0mVb3nQrStUvWxYz0123456789ABCDEFG" not in written, \
            "a Spotify access token was written to the diagnostic"
        assert "sp_session=abc123def456" not in written, \
            "a session cookie was written to the diagnostic"
        # The diagnostic is still useful: the page structure survives.
        assert "the upload button did not appear" in written

    def test_capture_is_opt_out(self, tmp_path):
        from app.publishers.spotify.browser import PlaywrightSession, SessionOptions

        session = PlaywrightSession.__new__(PlaywrightSession)
        session.options = SessionOptions(capture_diagnostics=False,
                                         diagnostics_dir=tmp_path)
        session.content = lambda: "<html>secret token=abc</html>"
        session.screenshot = lambda path: None
        session.url = lambda: "https://creators.spotify.com/"

        info = session.capture("anything")
        assert "html" not in info
        assert list(tmp_path.iterdir()) == [], "capturing wrote files while disabled"
