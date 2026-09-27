"""Centralised Spotify UI selectors.

**This is the only file that needs editing when Spotify changes its website.**

Design rules
------------
* Every logical field is a :class:`SelectorSet` holding an *ordered list* of
  candidate selectors.  The browser layer tries them in order and reports
  which one matched, so a partial UI change degrades instead of breaking.
* Selectors prefer stable, documented hooks (Playwright ``role=``/``label=``
  matching, ``data-testid``) and only then fall back to CSS.  No screen
  coordinates, ever.
* Selectors are grouped by page so ``spotify-doctor`` can test them
  independently and report exactly which field broke.
* Each set carries a ``description`` used in the error message, so a failure
  tells a non-programmer what could not be found.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SelectorSet", "SELECTORS", "PAGES", "selector_names", "validate_registry"]


@dataclass(frozen=True)
class SelectorSet:
    """Ordered candidate selectors for one logical field."""

    name: str
    description: str
    candidates: tuple[str, ...]
    #: "visible" | "attached" - which Playwright state to wait for.
    state: str = "visible"
    #: Set when the element can legitimately be absent (optional features).
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError(f"SelectorSet {self.name!r} must have at least one candidate")

    def all(self) -> tuple[str, ...]:
        return self.candidates

    def primary(self) -> str:
        return self.candidates[0]


# --------------------------------------------------------------------------
# Page 1: authentication
# --------------------------------------------------------------------------
LOGIN = {
    "login_marker": SelectorSet(
        "login_marker",
        "A marker that proves we are on the Spotify login page.",
        (
            'input[name="login"]',
            'input[type="password"]',
            'input[id="loginInput-password"]',
            "#login-page",
            'a[href*="/pod/login"]',
        ),
    ),
    "login_submit": SelectorSet(
        "login_submit",
        "The button that submits the Spotify login form.",
        (
            'button[type="submit"]',
            'button:has-text("Log in")',
            'button:has-text("Anmelden")',
        ),
    ),
    "logged_in_marker": SelectorSet(
        "logged_in_marker",
        "Anything that only exists once we are signed in.",
        (
            'a[href*="/pod/dashboard"]',
            'a[href*="/pod/dashboard/episodes"]',
            '[data-testid="sidebar"]',
            'text=/New episode/i',
        ),
    ),
    "verify_email_marker": SelectorSet(
        "verify_email_marker",
        "Spotify's 'confirm your email' screen, shown before the first publish.",
        (
            'text=/verify your email/i',
            'text=/confirm your email/i',
            'text=/bestätigen sie ihre e-mail/i',
            'text=/verification email/i',
        ),
    ),
    "two_step_marker": SelectorSet(
        "two_step_marker",
        "Spotify's two-step verification prompt (the human completes it).",
        (
            'text=/two-step verification/i',
            'text=/two-step/i',
            'text=/authentifizierung/i',
        ),
    ),
    "account_menu": SelectorSet(
        "account_menu",
        "The avatar/menu used to leave or re-check the session.",
        (
            '[data-testid="profile-button"]',
            'button[aria-label*="Account" i]',
            'button[aria-label*="Konto" i]',
            'button:has-text("Account")',
        ),
    ),
}

# --------------------------------------------------------------------------
# Page 2: show selection / episode list
# --------------------------------------------------------------------------
DASHBOARD = {
    "shows_nav": SelectorSet(
        "shows_nav",
        "The sidebar entry that lists the shows on the account.",
        (
            'a[href*="/pod/dashboard/shows"]',
            '[data-testid="shows-nav"]',
            'text=/Your shows/i',
            'text=/Deine Shows/i',
        ),
    ),
    "show_item": SelectorSet(
        "show_item",
        "One show in the show list. {name} is substituted with the show name.",
        (
            '[data-testid="show-item"]',
            'a[href*="/pod/dashboard/episode"] >> text=/{name}/',
            'li:has-text("{name}")',
        ),
    ),
    "episodes_nav": SelectorSet(
        "episodes_nav",
        "The sidebar entry that opens the episode list.",
        (
            'a[href*="/pod/dashboard/episodes"]',
            '[data-testid="episodes-nav"]',
            'text=/Episodes/i',
            'text=/Folgen/i',
        ),
    ),
    "new_episode_button": SelectorSet(
        "new_episode_button",
        "The 'New episode' button.",
        (
            '[data-testid="new-episode-button"]',
            'button:has-text("New episode")',
            'button:has-text("Neue Folge")',
            'button:has-text("New Episode")',
            'a:has-text("New episode")',
        ),
    ),
    "episode_row": SelectorSet(
        "episode_row",
        "A row in the episode list. {title} is substituted with the episode title.",
        (
            '[data-testid="episode-row"]:has-text("{title}")',
            '[data-testid="episode-list-item"]:has-text("{title}")',
            'tr:has-text("{title}")',
            'li:has-text("{title}") >> a',
        ),
    ),
    "episode_row_any": SelectorSet(
        "episode_row_any",
        "Any row in the episode list (used to list what already exists).",
        (
            '[data-testid="episode-row"]',
            '[data-testid="episode-list-item"]',
            "tr[data-episode-id]",
        ),
    ),
    "episode_state": SelectorSet(
        "episode_state",
        "The state chip on an episode row (Published / Scheduled / Draft).",
        (
            '[data-testid="episode-row"] [data-testid="episode-status"]',
            '[data-testid="episode-list-item"] [data-testid="episode-status"]',
            '[data-episode-status]',
        ),
    ),
    "episode_link": SelectorSet(
        "episode_link",
        "The link to the episode's own page, used to capture its URL/ID.",
        (
            '[data-testid="episode-row"] a[href*="/episode/"]',
            'a[href*="/pod/dashboard/episode/"]',
        ),
    ),
    "episodes_heading": SelectorSet(
        "episodes_heading",
        "Heading proving we are on the episode list page.",
        (
            '[data-testid="episodes-page"]',
            'h1:has-text("Episodes")',
            'h1:has-text("Folgen")',
            'h2:has-text("Episodes")',
        ),
    ),
    "no_episodes": SelectorSet(
        "no_episodes",
        "Shown when the show has no episodes at all.",
        (
            '[data-testid="empty-episodes"]',
            'text=/no episodes yet/i',
            'text=/noch keine folgen/i',
        ),
    ),
}

# --------------------------------------------------------------------------
# Page 3: upload + details
# --------------------------------------------------------------------------
EDITOR = {
    "file_input": SelectorSet(
        "file_input",
        "The hidden <input type=file> the media is attached to.",
        (
            'input[type="file"]',
            '[data-testid="file-input"] input',
            '[data-testid="upload-input"]',
        ),
        state="attached",
    ),
    "select_file_button": SelectorSet(
        "select_file_button",
        "The visible 'Select a file' button that opens the file picker.",
        (
            'button:has-text("Select a file")',
            'button:has-text("Datei auswählen")',
            '[data-testid="select-file-button"]',
            'label:has-text("Select a file")',
        ),
    ),
    "upload_progress": SelectorSet(
        "upload_progress",
        "The upload progress bar/number. Optional - Spotify may not show one.",
        (
            '[data-testid="upload-progress"]',
            '[role="progressbar"]',
            'progress',
            '[class*="rogress"] [class*="ercent"]',
            'text=/uploading/i',
        ),
        optional=True,
    ),
    "processing_marker": SelectorSet(
        "processing_marker",
        "Text Spotify shows while it transcodes the media.",
        (
            'text=/processing/i',
            'text=/transcoding/i',
            'text=/wird verarbeitet/i',
            '[data-testid="processing"]',
        ),
        optional=True,
    ),
    "upload_error": SelectorSet(
        "upload_error",
        "An error Spotify shows after a failed upload.",
        (
            '[data-testid="upload-error"]',
            '[role="alert"]',
            'text=/upload failed/i',
            'text=/something went wrong/i',
            'text=/fehler/i',
        ),
        optional=True,
    ),
    "title_input": SelectorSet(
        "title_input",
        "The episode title field.",
        (
            '[data-testid="title-input"]',
            'input[name="title"]',
            'input[id="title"]',
            'input[placeholder*="Title" i]',
            'input[placeholder*="Titel" i]',
            'input[aria-label*="Title" i]',
            'input[aria-label*="Titel" i]',
        ),
    ),
    "description_input": SelectorSet(
        "description_input",
        "The episode description / show notes editor.",
        (
            '[data-testid="description-input"]',
            '[data-testid="description"] [contenteditable="true"]',
            'div[contenteditable="true"][role="textbox"]',
            'textarea[name="description"]',
            'textarea[id="description"]',
            'textarea[aria-label*="Description" i]',
        ),
    ),
    "html_toggle": SelectorSet(
        "html_toggle",
        "The switch that reveals the raw HTML editor for the description.",
        (
            '[data-testid="html-toggle"]',
            'button:has-text("HTML")',
            'label:has-text("HTML")',
        ),
        optional=True,
    ),
    "explicit_toggle": SelectorSet(
        "explicit_toggle",
        "The control under 'Content checks' that marks the episode explicit.",
        (
            '[data-testid="content-check-explicit"]',
            'input[name="explicit"]',
            'input[id*="explicit" i]',
            '[data-testid="explicit-toggle"]',
            'label:has-text("Explicit")',
            'text=/explicit/i',
        ),
    ),
    "season_input": SelectorSet(
        "season_input",
        "The season number field under 'Additional details'.",
        (
            '[data-testid="season-input"]',
            'input[name="season"]',
            'input[id*="season" i]',
            'input[aria-label*="Season" i]',
            'input[placeholder*="Season" i]',
        ),
        optional=True,
    ),
    "episode_number_input": SelectorSet(
        "episode_number_input",
        "The episode number field under 'Additional details'.",
        (
            '[data-testid="episode-number-input"]',
            'input[name="episode"]',
            'input[name="episodeNumber"]',
            'input[id*="episode-number" i]',
            'input[aria-label*="Episode number" i]',
            'input[aria-label*="Folgenummer" i]',
        ),
        optional=True,
    ),
    "additional_details_toggle": SelectorSet(
        "additional_details_toggle",
        "The control that expands 'Additional details'.",
        (
            'button:has-text("Additional details")',
            '[data-testid="additional-details-toggle"]',
            'summary:has-text("Additional details")',
        ),
        optional=True,
    ),
    "publish_now_radio": SelectorSet(
        "publish_now_radio",
        "The 'Now' option under Publish date.",
        (
            '[data-testid="publish-now"]',
            'input[value="now"]',
            'input[name="publishDate"][value="now"]',
            'label:has-text("Now")',
            'text=/^Publish now$/i',
        ),
    ),
    "publish_schedule_radio": SelectorSet(
        "publish_schedule_radio",
        "The 'Schedule' option under Publish date.",
        (
            '[data-testid="publish-schedule"]',
            'input[value="schedule"]',
            'input[name="publishDate"][value="schedule"]',
            'label:has-text("Schedule")',
            'label:has-text("Planen")',
        ),
    ),
    "schedule_date_input": SelectorSet(
        "schedule_date_input",
        "The date field shown after choosing Schedule.",
        (
            '[data-testid="schedule-date"]',
            'input[name="publishDate"]',
            'input[type="date"]',
            'input[id*="publish-date" i]',
        ),
        optional=True,
    ),
    "schedule_time_input": SelectorSet(
        "schedule_time_input",
        "The time field shown after choosing Schedule.",
        (
            '[data-testid="schedule-time"]',
            'input[name="publishTime"]',
            'input[type="time"]',
            'input[id*="publish-time" i]',
        ),
        optional=True,
    ),
    "publish_button": SelectorSet(
        "publish_button",
        "The button that publishes the episode.",
        (
            '[data-testid="publish-button"]',
            'button:has-text("Publish")',
            'button:has-text("Veröffentlichen")',
            'button:has-text("Publish episode")',
        ),
    ),
    "save_draft_button": SelectorSet(
        "save_draft_button",
        "The button that saves the episode as a draft.",
        (
            '[data-testid="save-draft-button"]',
            'button:has-text("Save draft")',
            'button:has-text("Entwurf speichern")',
        ),
        optional=True,
    ),
    "close_button": SelectorSet(
        "close_button",
        "The X used to leave the editor without publishing.",
        (
            '[data-testid="close-button"]',
            'button[aria-label*="Close" i]',
            'button[aria-label*="Schließen" i]',
        ),
        optional=True,
    ),
    "form_error": SelectorSet(
        "form_error",
        "A validation error Spotify shows before it will accept the episode.",
        (
            '[data-testid="form-error"]',
            '[role="alert"]',
            'text=/required/i',
            'text=/erforderlich/i',
        ),
        optional=True,
    ),
    "success_toast": SelectorSet(
        "success_toast",
        "The confirmation Spotify shows after a successful publish.",
        (
            '[data-testid="toast-success"]',
            '[role="status"]',
            'text=/published/i',
            'text=/veröffentlicht/i',
        ),
        optional=True,
    ),
}

PAGES: dict[str, dict[str, SelectorSet]] = {
    "login": LOGIN,
    "dashboard": DASHBOARD,
    "editor": EDITOR,
}

SELECTORS: dict[str, SelectorSet] = {
    name: selector_set
    for page in PAGES.values()
    for name, selector_set in page.items()
}

#: Substitutions allowed inside a candidate selector.
TEMPLATES = {"name", "title"}

#: A selector must begin with one of Playwright's engines (``text=``,
#: ``role=``) or a CSS/XPath-ish character.  Anything else is a typo that would
#: silently never match, which is exactly the failure mode we refuse to accept.
SELECTOR_PREFIXES = (
    "text=", "role=", "label=", "xpath=", "css=",
    "[", "#", ".", "*", ":",
    "a", "b", "c", "d", "f", "h", "i", "l", "n", "p", "s", "t", "u", "x",
)


def selector_names() -> list[str]:
    return sorted(SELECTORS)


def get(name: str) -> SelectorSet:
    try:
        return SELECTORS[name]
    except KeyError:
        raise KeyError(
            f"Unknown selector {name!r}. Known: {', '.join(selector_names())}"
        ) from None


def validate_registry() -> list[str]:
    """Static self-check of the registry.  Returns a list of problems.

    ``spotify-doctor`` runs this before touching a browser, so an obvious typo
    is caught instantly instead of halfway through a 6 GB upload.
    """
    problems: list[str] = []
    for page_name, page in PAGES.items():
        if not page:
            problems.append(f"Page group {page_name!r} is empty.")
        for key, selector_set in page.items():
            if key != selector_set.name:
                problems.append(
                    f"Page {page_name}: key {key!r} does not match SelectorSet "
                    f"name {selector_set.name!r}."
                )
            if not selector_set.candidates:
                problems.append(f"{key}: no selector candidates.")
            seen: set[str] = set()
            for candidate in selector_set.candidates:
                if not candidate.strip():
                    problems.append(f"{key}: empty selector candidate.")
                    continue
                if candidate in seen:
                    problems.append(f"{key}: duplicate selector {candidate!r}.")
                seen.add(candidate)
                if not candidate.strip().startswith(SELECTOR_PREFIXES):
                    problems.append(
                        f"{key}: selector {candidate!r} does not start with a known "
                        f"selector engine prefix."
                    )
                if "{" in candidate:
                    inner = candidate.split("{", 1)[1].split("}", 1)[0]
                    if inner not in TEMPLATES:
                        problems.append(
                            f"{key}: selector {candidate!r} uses unknown template "
                            f"{{{inner}}}."
                        )
            if selector_set.state not in {"visible", "attached", "hidden", "detached"}:
                problems.append(f"{key}: invalid wait state {selector_set.state!r}.")
    if "publish_button" not in SELECTORS:
        problems.append("The critical control 'publish_button' is missing.")
    return problems


def probe_targets() -> list[tuple[str, str, str]]:
    """``(page, name, primary_selector)`` for every selector - used by doctor."""
    return [
        (page, name, s.primary())
        for page, group in PAGES.items()
        for name, s in group.items()
    ]
