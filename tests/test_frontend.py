"""Contract tests for the vanilla-JS frontend.

There is no build step and no framework, so nothing would otherwise catch a
renamed element id, a typo in an endpoint, or a stray CDN reference until the
page was opened by hand. These tests check exactly those three things, plus a
syntax parse when Node happens to be installed — all without adding a
dependency to the project.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.main import app

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "styles.css").read_text(encoding="utf-8")

#: `$("#thing")` / `$$("#thing")` — the only way the client reaches the DOM.
SELECTOR_RE = re.compile(r"""\$\$?\(\s*["'`]#([A-Za-z0-9_-]+)["'`]\s*\)""")
#: `/api/...` string literals, with `${...}` interpolations left in place.
ENDPOINT_RE = re.compile(r"""["'`](/api/[^"'`\s?]*)""")


def _routes() -> list[str]:
    return [route.path for route in app.routes if hasattr(route, "path")]


def _normalise(js_path: str) -> str:
    """`/api/learners/${id}/path` -> `/api/learners/{}/path`."""
    return re.sub(r"\$\{[^}]*\}", "{}", js_path).rstrip("/")


def _route_pattern(route: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", route).rstrip("/")


# --------------------------------------------------------------- DOM contract
def test_every_selector_the_client_uses_exists_in_the_page():
    ids_in_html = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", HTML))
    missing = sorted(set(SELECTOR_RE.findall(JS)) - ids_in_html)
    assert not missing, f"app.js queries ids that index.html does not define: {missing}"


def test_the_page_loads_only_local_assets():
    assert '<script src="/static/app.js">' in HTML
    assert 'href="/static/styles.css"' in HTML
    assert (FRONTEND / "app.js").exists()
    assert (FRONTEND / "styles.css").exists()


def test_the_page_never_loads_a_remote_asset():
    """'No build step, no CDN' is a claim the tests should enforce.

    A link the learner clicks is fine — that is the point of course URLs. What
    must never happen is the page *fetching* from another host.
    """
    remote_asset_patterns = [
        (r"""<script[^>]+src=["']https?://""", "remote script"),
        (r"""<link[^>]+href=["']https?://""", "remote stylesheet"),
        (r"""<(?:img|iframe|source|video|audio)[^>]+src=["']https?://""", "remote media"),
        (r"""url\(\s*['"]?https?://""", "remote CSS asset"),
        (r"""fetch\(\s*["'`]https?://""", "cross-origin fetch"),
        (r"""(?:XMLHttpRequest|WebSocket|EventSource|Worker)\s*\([^)]*https?://""", "remote connection"),
        (r"""\.src\s*=\s*["'`]https?://""", "remote asset assignment"),
    ]
    for name, source in (("index.html", HTML), ("app.js", JS), ("styles.css", CSS)):
        for pattern, what in remote_asset_patterns:
            hit = re.search(pattern, source, re.IGNORECASE)
            assert hit is None, f"{name} contains a {what}: {hit.group(0)!r}"


def test_outbound_links_are_opened_safely():
    """Course links leave the page, so they must not hand it to the opener."""
    for match in re.finditer(r"""el\(\s*["']a["'](.{0,400}?)\}\)""", JS, re.DOTALL):
        block = match.group(1)
        if "http" not in block and "url" not in block:
            continue  # a local link, e.g. the Markdown export blob
        if 'target: "_blank"' in block or "_blank" in block:
            assert "noopener" in block and "noreferrer" in block, (
                f"outbound link misses rel=noopener noreferrer: {block[:120]}"
            )


# ---------------------------------------------------------------- API contract
def test_every_endpoint_the_client_calls_is_a_real_route():
    known = {_route_pattern(path) for path in _routes()}
    called = {_normalise(path) for path in ENDPOINT_RE.findall(JS)}
    unknown = sorted(path for path in called if path not in known)
    assert not unknown, f"app.js calls endpoints the API does not serve: {unknown}"


@pytest.mark.parametrize("endpoint", [
    "/api/health",
    "/api/catalog/goals",
    "/api/catalog/skills",
    "/api/chat",
    "/api/learners",
])
def test_bootstrap_endpoints_are_wired(endpoint):
    """The page cannot start without these, so name them explicitly."""
    assert endpoint in JS


@pytest.mark.parametrize("feature,fragment", [
    ("skill graph", "/graph"),
    ("weekly plan", "/plan"),
    ("dashboard", "/dashboard"),
    ("achievements", "/achievements"),
    ("markdown export", "/export"),
    ("recommendations", "/recommendations"),
    ("gap analysis", "/gap"),
    ("per-item explanation", "/explain/"),
    ("progress tracking", "/progress"),
    ("feedback", "/feedback"),
])
def test_every_learner_feature_is_reachable_from_the_ui(feature, fragment):
    """Each documented capability has a client call behind it, not just a route."""
    assert fragment in JS, f"the UI never calls the {feature} endpoint"


def test_every_tab_is_deep_linkable():
    """Each panel must be reachable by URL, so views can be linked and shared."""
    panels = set(re.findall(r"""id=["']panel-([a-z]+)["']""", HTML))
    declared = re.search(r"const TABS = \[(.*?)\]", JS, re.DOTALL)
    assert declared, "app.js no longer declares the tab list"
    routed = set(re.findall(r'"([a-z]+)"', declared.group(1)))
    assert routed == panels, f"tabs and panels disagree: {routed ^ panels}"
    assert "hashchange" in JS, "back/forward navigation between tabs is not handled"


def test_every_button_in_the_page_is_wired_to_something():
    """A button with no handler is a dead control the user still clicks.

    `Rename`, `New learner` and `Save my work` all shipped this way: present in
    the markup, styled, and bound to nothing at all.
    """
    # Class tokens that a delegated `closest(...)` handler picks up, e.g. the
    # engine switch, which is bound once on `document` rather than per button.
    delegated = set()
    for selector in re.findall(r"""closest\??\.?\(\s*["']([^"']+)["']""", JS):
        delegated.update(re.findall(r"\.([A-Za-z0-9_-]+)", selector))

    unwired = []
    for tag in re.findall(r"<button[^>]*>", HTML):
        found = re.search(r"""\bid=["']([A-Za-z0-9_-]+)["']""", tag)
        if not found:
            continue
        button = found.group(1)
        if re.search(rf"""["'#]{re.escape(button)}["']""", JS):
            continue  # referenced by id somewhere in the client
        classes = set(re.findall(r"""\bclass=["']([^"']*)["']""", tag)[0].split()) \
            if re.search(r"""\bclass=["']""", tag) else set()
        if classes & delegated:
            continue  # handled by delegation
        unwired.append(button)

    assert unwired == [], f"buttons in index.html that app.js never handles: {unwired}"


def test_the_status_pill_class_is_not_reused_for_achievement_tiles():
    """`.badge` sets `white-space: nowrap` for the header pills.

    The achievement tiles are multi-line cards, so they carry their own class.
    Sharing `.badge` made their descriptions overflow into the next card.
    """
    assert re.search(r"^\.badge\s*\{[^}]*white-space:\s*nowrap", CSS, re.MULTILINE), (
        "the header status pill no longer sets nowrap — this guard needs rewriting"
    )
    assert '"ach-badge"' in JS, "achievement tiles must not render with class `badge`"
    assert ".badge-grid .badge" not in CSS


def test_the_toast_hides_completely_when_it_is_empty():
    """`translateY(120%)` of an ~18px empty box does not clear a 1.5rem inset.

    It left a blank pill floating at the bottom of every page.
    """
    resting = re.search(r"\.toast\s*\{[^}]*?transform:\s*([^;]+);", CSS, re.DOTALL)
    assert resting, "the toast no longer declares a resting transform"
    assert "%" not in resting.group(1).split("translateY")[-1].split(")")[0] or "calc" in resting.group(1), (
        f"the toast hides by a bare percentage of its own height: {resting.group(1)!r}"
    )


def test_the_client_has_no_innerhtml_path():
    """Catalogue text and model output must never be interpreted as markup."""
    assigned = [
        value.strip()
        for value in re.findall(r"innerHTML\s*=\s*([^;]+);", JS)
        if value.strip() not in ('""', "''", "``")
    ]
    assert not assigned, f"innerHTML assigned from a value: {assigned}"


# ------------------------------------------------------------------- syntax
@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_app_js_parses():
    result = subprocess.run(
        ["node", "--check", str(FRONTEND / "app.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_empty_tabs_offer_a_way_forward():
    """"Set a goal in the Chat tab first" is a dead end on five of seven tabs.

    A first-time visitor should be able to see the product working without
    typing anything, so every empty state carries actions.
    """
    assert "emptyWithStart" in JS, "the actionable empty state is gone"
    assert 'text: "Set a goal in the Chat tab first."' not in JS, (
        "a dead-end empty state came back"
    )
    assert "startExamplePath" in JS, "nothing builds the worked example"
    # The example has to be a goal the catalogue actually defines.
    match = re.search(r'const EXAMPLE_GOAL = "([^"]+)"', JS)
    assert match, "the example goal is no longer declared"
    from backend.catalog import get_catalog

    assert match.group(1) in get_catalog().goals


def test_the_worked_example_does_not_rebuild_silently():
    """It changes the learner's goal, so it must say what it did."""
    block = re.search(r"async function startExamplePath.*?\n\}", JS, re.DOTALL)
    assert block, "startExamplePath is gone"
    assert "toast(" in block.group(0), "the example switches goals without saying so"
