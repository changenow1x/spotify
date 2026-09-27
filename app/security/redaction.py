"""Secret redaction for logs and error messages.

The rule enforced here: nothing that looks like a credential reaches a log
file, the dashboard, the database ``last_error`` column, or stdout.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = ["redact_text", "redact_mapping", "REDACTION", "scrub_exception",
           "redact_html"]

REDACTION = "***REDACTED***"

# Key names whose values are always masked, regardless of format.
_SENSITIVE_KEYS = re.compile(
    r"(pass(word|phrase)?|secret|token|cookie|authorization|auth|bearer|"
    r"session|credential|api[_-]?key|private[_-]?key|refresh|access[_-]?token|"
    r"otp|2fa|otpauth|saml|signature)",
    re.IGNORECASE,
)

# Value shapes that are always masked, regardless of the key they sit under.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization / bearer headers
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 " + REDACTION),
    (re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"), "authorization=" + REDACTION),
    # Cookie / Set-Cookie
    (re.compile(r"(?i)\b(set-)?cookie\s*[:=]\s*[^\r\n]+"), "cookie=" + REDACTION),
    # Basic auth in a URL
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTION + "@"),
    # Explicit key=value secrets, e.g. "password=..." or "client_secret: ..."
    (re.compile(
        r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|apikey|"
        r"client[_-]?secret|private[_-]?key|auth[_-]?token|access[_-]?token|"
        r"refresh[_-]?token|session[_-]?token)\b\s*[:=]\s*\S+"
     ), r"\1=" + REDACTION),
    # Spotify access/refresh tokens look like base64 blobs
    (re.compile(r"\bAQ[A-Za-z0-9_\-]{32,}\b"), REDACTION),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), REDACTION),
    # Long hex secrets (32+ chars) that are clearly not a hash of our own making
    (re.compile(r"\b[0-9a-fA-F]{64,}\b"), REDACTION),
)

# Log lines that must never be persisted verbatim.
_SENSITIVE_LINE = re.compile(
    r"(?i)\b(password|passphrase|api[_-]?key|secret|token|cookie|authorization)\b"
)


def redact_text(text: str, *, max_len: int = 4000) -> str:
    """Mask credential-shaped substrings in free-form text."""
    if not text:
        return ""
    out = str(text)
    for pattern, repl in _VALUE_PATTERNS:
        out = pattern.sub(repl, out)
    if len(out) > max_len:
        out = out[:max_len] + f"... [truncated {len(out) - max_len} chars]"
    return out


def redact_mapping(data: Any, *, max_depth: int = 8) -> Any:
    """Recursively copy *data*, masking values under sensitive keys."""
    return _redact(data, 0, max_depth)


def _redact(data: Any, depth: int, max_depth: int) -> Any:
    if depth > max_depth:
        return "<truncated:max-depth>"
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and _SENSITIVE_KEYS.search(key):
                out[key] = REDACTION
            else:
                out[key] = _redact(value, depth + 1, max_depth)
        return out
    if isinstance(data, (list, tuple)):
        return [_redact(v, depth + 1, max_depth) for v in data]
    if isinstance(data, str):
        return redact_text(data)
    return data


#: Cookie/session names that appear in a real page.  ``redact_text`` needs the
#: literal word "cookie" next to the value, which is true of a header but not
#: of an HTML attribute, so a captured page needs its own rule.
_HTML_SESSION_RE = re.compile(
    r"(?i)\b("
    r"sp[_-]?session(?:[_-]?id)?|spotify[_-]?[a-z_]*session[a-z_]*|"
    r"__Secure-[A-Za-z0-9_-]+|__Host-[A-Za-z0-9_-]+|"
    r"sess(?:ion)?[_-]?id|sessid|auth[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|id[_-]?token|"
    r"[a-z0-9_]*session[a-z0-9_]*cookie"
    r")\b(\s*[=:]\s*)\"?([^\s\"';<>]{6,})"
)


def redact_html(html: str) -> str:
    """Scrub a captured page before it is written to disk.

    A page captured while the user is signed in can carry session values in
    inline scripts and in attributes.  The structure of the page is what makes
    a diagnostic useful, so everything except the values is kept.
    """
    if not html:
        return html
    out = _HTML_SESSION_RE.sub(r"\1\2" + REDACTION, html)
    return redact_text(out, max_len=len(out) + 1)


def scrub_exception(exc: BaseException) -> str:
    """Render an exception as a redacted, single-line, secret-free string."""
    try:
        rendered = f"{type(exc).__name__}: {exc}"
    except Exception:  # pragma: no cover - pathological __str__
        rendered = type(exc).__name__
    return redact_text(rendered)


def contains_sensitive_key(keys: Iterable[str]) -> bool:
    return any(_SENSITIVE_KEYS.search(k or "") for k in keys)
