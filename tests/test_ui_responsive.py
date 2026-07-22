"""Structural assertions for the mobile/responsive UI (see /tmp/websharr-mobile-plan.md).

Geometry (actual rendered layout, overflow, touch targets) needs a real
browser and is covered separately by an ad-hoc Playwright check; this file
only asserts the markup/CSS building blocks are present and that the
existing tab/view id contract survived the restructuring.
"""


def test_mobile_drawer_and_breakpoints_present(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    for needle in (
        'id="hdr-toggle"', 'id="hdr-menu"', 'id="hdr-backdrop"',
        "@media (max-width: 860px)", "@media (max-width: 640px)",
        "@media (prefers-reduced-motion: reduce)",
        "env(safe-area-inset", "viewport-fit=cover",
        'class="ibtn movebtn moveup"', 'class="ibtn movebtn movedown"',
    ):
        assert needle in html, needle


def test_drawer_close_control_present_once(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert html.count('id="hdr-close"') == 1
    assert html.count('aria-label="Close menu"') == 1
    assert 'onclick="App.toggleMenu(false)"' in html
    # the close control must live inside the drawer panel, not duplicate the hamburger toggle
    menu_start = html.index('id="hdr-menu"')
    close_pos = html.index('id="hdr-close"')
    nav_pos = html.index("<nav ", menu_start)
    assert menu_start < close_pos < nav_pos


def test_six_tabs_still_unique(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    for tab in ("queue", "history", "search", "log", "help", "settings"):
        assert html.count(f'id="tab-{tab}"') == 1, tab
        assert html.count(f'id="view-{tab}"') == 1, tab


def test_drawer_does_not_steal_focus_on_show_or_closed_escape(client):
    """toggleMenu(false) must not unconditionally focus the hamburger — only
    when the drawer was actually open and is being closed. show() and a
    closed-drawer Escape must be no-ops with respect to focus."""
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert "this.toggleMenu(false, { returnFocus: false });" in html
    assert "if (open === wasOpen) return;" in html
    assert 'if (e.key === "Escape") { if (document.body.classList.contains("menu-open")) App.toggleMenu(false); return; }' in html


def test_drawer_inert_and_aria_hidden_management(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert "syncMenuInert" in html
    assert 'menuMql: window.matchMedia("(max-width: 860px)")' in html
    assert 'menu.toggleAttribute("inert", hidden)' in html
    assert 'menu.setAttribute("aria-hidden", "true")' in html
    assert 'menu.removeAttribute("aria-hidden")' in html
    # breakpoint crossing (e.g. rotation, window resize) must be handled live
    assert "App.menuMql.addEventListener(\"change\"" in html


def test_drawer_focus_trap_present(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert 'e.key !== "Tab"' in html
    assert "querySelectorAll('button, a[href], input, select, textarea" in html


def test_drawer_nav_not_mislabeled_as_dialog(client):
    """Primary navigation must stay a <nav>, not get role=dialog/aria-modal."""
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert 'role="dialog"' not in html
    assert "aria-modal" not in html
    assert '<nav aria-label="Main">' in html


def test_queue_move_buttons_available_through_tablet_width(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    # grip/movebtn swap must happen at the 860px breakpoint, not just 640px
    block_start = html.index("@media (max-width: 860px) {\n    .grip { display: none; }")
    block_end = html.index("/* history */", block_start)
    block = html[block_start:block_end]
    assert ".movebtn { display: inline-flex" in block
    assert "min-width: 44px; min-height: 44px" in block


def test_alias_remove_target_is_44px(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert ".aliasrow .xbtn { position: absolute; top: 0; right: 0; min-width: 44px; min-height: 44px; }" in html


def test_queue_reorder_commits_are_serialized(client):
    """Rapid taps on the mobile move buttons must not fire overlapping
    queue/reorder POSTs — commitOrder() has to guard re-entry, disable the
    move controls for the duration of the request, and release the busy
    flag in a finally so a network/API failure can't leave the controls
    (or the lock) stuck."""
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text

    assert "reorderBusy: false," in html

    commit_start = html.index("async commitOrder() {")
    move_start = html.index("async moveJob(id, delta) {")
    commit_body = html[commit_start:move_start]
    move_body = html[move_start:html.index("async setMaxConcurrent(delta) {")]

    # commitOrder() must bail out immediately if a commit is already in flight,
    # and moveJob() must not touch the DOM order at all while one is pending.
    assert "if (this.reorderBusy) return;" in commit_body
    assert "if (this.reorderBusy) return;" in move_body

    # The busy flag must be set, and the move buttons disabled, before the
    # network request is issued (not after), so a second rapid tap can't
    # slip in while the first request is still pending.
    set_busy_pos = commit_body.index("this.reorderBusy = true;")
    disable_pos = commit_body.index('.disabled = true;')
    await_pos = commit_body.index("await uiPost(")
    assert set_busy_pos < await_pos
    assert disable_pos < await_pos

    # The busy flag must be released in a finally, so it clears even if
    # uiPost() rejects (offline / API failure), instead of wedging the
    # move controls disabled forever.
    finally_pos = commit_body.index("finally {")
    release_pos = commit_body.index("this.reorderBusy = false;")
    refresh_pos = commit_body.index("this.refresh(true);")
    assert await_pos < finally_pos < release_pos < refresh_pos

    # Boundary (first/last) disabling must still take reorderBusy into
    # account so it doesn't fight with the busy-state disabling, and so
    # buttons come back correctly enabled once a commit finishes.
    assert 'c.querySelector(".moveup").disabled = this.reorderBusy || i === 0;' in html
    assert 'c.querySelector(".movedown").disabled = this.reorderBusy || i === cards.length - 1;' in html
