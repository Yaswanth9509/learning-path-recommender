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

    # ...and tokens bound by class across the whole set, e.g. `$$(".tab")`,
    # which wires every tab at once. Those buttons carry an id only so the
    # panels can point at them with `aria-labelledby`, never to be looked up.
    for selector in re.findall(r"""\$\$?\(\s*["'](\.[^"']+)["']\s*\)""", JS):
        delegated.update(re.findall(r"\.([A-Za-z0-9_-]+)", selector))

    # A `type="submit"` button is wired by its form, and every form in this
    # page has a submit handler — asserted just below, so this cannot rot into
    # a loophole.
    unwired = []
    for tag in re.findall(r"<button[^>]*>", HTML):
        found = re.search(r"""\bid=["']([A-Za-z0-9_-]+)["']""", tag)
        if not found:
            continue
        button = found.group(1)
        if 'type="submit"' in tag:
            continue  # its form's submit handler runs it
        if re.search(rf"""["'#]{re.escape(button)}["']""", JS):
            continue  # referenced by id somewhere in the client
        classes = set(re.findall(r"""\bclass=["']([^"']*)["']""", tag)[0].split()) \
            if re.search(r"""\bclass=["']""", tag) else set()
        if classes & delegated:
            continue  # handled by delegation
        unwired.append(button)

    assert unwired == [], f"buttons in index.html that app.js never handles: {unwired}"


def test_every_form_actually_handles_its_own_submit():
    """Submit buttons are exempted above, so the forms must earn that."""
    forms = re.findall(r"""<form[^>]*\bid=["']([A-Za-z0-9_-]+)["']""", HTML)
    assert forms, "no id'd forms found — did the markup change shape?"
    for form_id in forms:
        handled = re.search(
            rf"""["']#{re.escape(form_id)}["']\s*\)\s*\.addEventListener\(\s*["']submit["']""",
            JS,
        )
        assert handled, f"<form id={form_id}> has no submit handler"


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


def test_the_client_never_asks_through_a_browser_dialog():
    """`window.prompt` is a control the browser is allowed to switch off.

    Chrome returns null from it — silently, and for the rest of the page's
    life — once the user ticks "prevent this page from creating additional
    dialogs" on any earlier one. Rename, Change name and the recovery question
    were all built on it, so all three read as dead buttons with no error and
    nothing in the console. Asking happens in the page now.
    """
    called = re.findall(r"\bwindow\.(prompt|confirm|alert)\s*\(", JS)
    assert not called, f"the client still asks through a browser dialog: {called}"
    assert "function askModal(" in JS, "the in-page replacement for prompt() is gone"


def test_asking_in_the_page_always_settles():
    """A promise that never resolves leaves the caller waiting forever.

    Dismissing the dialog by ✕, backdrop or Escape has to resolve it too, not
    just submitting — otherwise cancelling a rename hangs the handler.
    """
    assert "modalOnClose" in JS, "closing the modal no longer notifies its caller"
    block = re.search(r"function closeModal\(\).*?\n\}", JS, re.DOTALL)
    assert block and "onClose" in block.group(0), (
        "closeModal does not run the dismissal callback, so cancel never resolves"
    )


@pytest.mark.parametrize("input_id", ["authPassword", "resetPassword"])
def test_every_password_input_has_a_reveal_control(input_id):
    """Typing a password blind makes a failed sign-in the only feedback on a typo."""
    toggle = f"{input_id}Toggle"
    assert re.search(rf"""id=["']{toggle}["']""", HTML), (
        f"#{input_id} has no reveal button beside it"
    )
    assert re.search(rf"""["']#{toggle}["']""", JS), (
        f"#{toggle} is in the markup but nothing wires it up"
    )
    assert "function setPasswordShown(" in JS, "the reveal helper is gone"


def test_the_reset_panel_is_bounded():
    """Switching to reset hides the tab pill, which was the only thing giving
    that column an edge. Without one the form floated loose in the panel."""
    block = re.search(r"\.auth-recover\s*\{[^}]*\}", CSS)
    assert block, ".auth-recover no longer carries its own rules"
    for prop in ("border:", "border-radius:", "padding:"):
        assert prop in block.group(0), f".auth-recover sets no {prop.rstrip(':')}"


def test_the_tab_strip_keeps_its_aria_state():
    """`role="tablist"` was declared and never honoured.

    Every tab carried the role while nothing set `aria-selected`, so a screen
    reader announced a tab list with no selection in it, and the panels were
    not associated with the tabs that label them.
    """
    tabs = re.findall(r"<button[^>]*\bclass=[\"']tab[^\"']*[\"'][^>]*>", HTML, re.DOTALL)
    assert len(tabs) == 7, f"expected 7 tabs, found {len(tabs)}"
    for tag in tabs:
        assert "aria-selected=" in tag, f"tab without aria-selected: {tag[:70]}"
        assert "aria-controls=" in tag, f"tab without aria-controls: {tag[:70]}"
        assert "tabindex=" in tag, f"tab without a roving tabindex: {tag[:70]}"

    panels = re.findall(r"<section[^>]*\bclass=[\"']panel[^\"']*[\"'][^>]*>", HTML)
    assert len(panels) == 7
    for tag in panels:
        assert "aria-labelledby=" in tag, f"panel not labelled by its tab: {tag[:70]}"

    # And the state has to be maintained, not just declared once in the markup.
    assert 'setAttribute("aria-selected"' in JS, "aria-selected is never updated"


def test_the_backdrop_does_not_imitate_the_skill_graph():
    """The lattice was a node-and-edge motif behind a node-and-edge diagram.

    Decoration that mimics the one visual carrying meaning competes with it.
    """
    assert "@keyframes lattice" not in CSS, "the drifting lattice is back"
    assert "circle cx=" not in CSS, "the backdrop draws graph nodes again"


def test_the_rationale_is_collapsed_by_default():
    """Twelve open rationales stacked is a wall nobody reads."""
    assert 'el("details", { class: "item-why" })' in JS, (
        "the per-item rationale is no longer a disclosure"
    )
    assert ".item-why summary" in CSS


def test_item_kind_does_not_borrow_the_status_palette():
    """`assessment` was emerald — the colour this file uses for done.

    An assessment nobody had started rendered green beside the words "Not
    started". Category must not be dressed as state.
    """
    for rule in ("role-core", "role-project", "role-assessment",
                 "kind-exam", "kind-certification", "kind-subject"):
        block = re.search(rf"\.{rule}\b[^{{]*\{{([^}}]*)\}}", CSS)
        assert block, f".{rule} has no rule any more"
        body = block.group(1)
        for status_colour in ("--good", "--warn", "--danger", "--brand"):
            assert status_colour not in body, (
                f".{rule} uses the status colour {status_colour}: {body.strip()}"
            )


def test_no_hand_rolled_type_sizes_outside_the_scale():
    """Twelve one-offs bypassed the token scale before this.

    SVG text is exempt: it sets px inside a scaled viewBox, where rem does not
    behave the same way.
    """
    svg_rules = ("ring-text", "node-label", "node-sub", "layer-label")
    offenders = []
    for match in re.finditer(r"([^{}]+)\{([^}]*font-size:\s*([^;}]+)[^}]*)\}", CSS):
        selector, _, size = match.group(1).strip(), match.group(2), match.group(3).strip()
        if size.startswith("var(") or size.endswith("em") or "px" not in size and "rem" not in size:
            continue
        if any(name in selector for name in svg_rules) or selector.startswith("body"):
            continue
        offenders.append(f"{selector.splitlines()[-1].strip()} -> {size}")
    assert not offenders, f"font sizes outside the token scale: {offenders}"


# ------------------------------------------------------------ documentation
def _docs() -> dict[str, str]:
    return {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "docs/solution-documentation.html")
    }


def test_the_documented_catalogue_size_is_the_real_one():
    """Both documents claimed 85 skills / 139 items / 14 goals for weeks.

    A number in prose has no compiler, so it only stays true if something
    checks it.
    """
    from backend.catalog import get_catalog

    catalog = get_catalog()
    counts = {
        "skills": len(catalog.skills),
        "items": len(catalog.items),
        "goals": len(catalog.goals),
    }
    for name, text in _docs().items():
        for noun, real in counts.items():
            claimed = {int(n) for n in re.findall(rf"(\d+)\s+{noun}\b", text)}
            wrong = {n for n in claimed if n != real} - {1}
            assert not wrong, (
                f"{name} claims {wrong} {noun}; there are {real}"
            )


def test_the_documented_python_versions_are_the_tested_ones():
    """The README claimed CI covered 3.10-3.12 while 3.10 was failing."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version:\s*\[([^\]]+)\]", workflow)
    assert matrix, "the CI matrix no longer declares python-version"
    tested = sorted(re.findall(r"3\.\d+", matrix.group(1)))

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floor = re.search(r'requires-python\s*=\s*">=(\d+\.\d+)"', pyproject)
    assert floor and floor.group(1) == tested[0], (
        f"requires-python says {floor and floor.group(1)}, CI floor is {tested[0]}"
    )

    classifiers = sorted(re.findall(r"Python :: (3\.\d+)", pyproject))
    assert classifiers == tested, (
        f"pyproject advertises {classifiers}, CI tests {tested}"
    )

    for name, text in _docs().items():
        for version in re.findall(r"Python 3\.\d+", text):
            assert version.split()[-1] in tested, (
                f"{name} claims support for {version}, which CI does not test"
            )


def test_the_documented_scoring_weights_are_the_real_ones():
    """Both documents listed six components long after there were five.

    `quality` was removed with the invented ratings it read, and the tables
    kept advertising it at 0.05 — describing data that no longer exists. The
    Explain modal in the app said "six" too, so users saw it as well.
    """
    from backend.recommender import WEIGHTS

    for name, text in _docs().items():
        for component, weight in WEIGHTS.items():
            assert component in text, f"{name} never mentions `{component}`"
            assert f"{weight:.2f}" in text, (
                f"{name} does not carry {component}'s real weight {weight:.2f}"
            )
        assert "<code>quality</code>" not in text and "`quality`" not in text, (
            f"{name} still documents the removed `quality` component"
        )

    #: Number words that would be wrong for the current component count.
    WRONG = {5: "six", 6: "five"}[len(WEIGHTS)]
    for name, text in {**_docs(), "frontend/app.js": JS}.items():
        low = text.lower()
        for phrase in (f"{WRONG} component", f"{WRONG} contribution",
                       f"{WRONG}-component"):
            assert phrase not in low, (
                f"{name} says '{phrase}' where there are {len(WEIGHTS)}"
            )
