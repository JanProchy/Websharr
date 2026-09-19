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
        'class="grip"', 'class="qstatus"',
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


def test_queue_drag_handle_is_touch_and_keyboard_accessible(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert 'aria-label="Drag to reorder; use arrow keys for keyboard control"' in html
    assert "touch-action: none" in html
    assert ".grip { width: 44px; height: 44px" in html
    assert "grip.onpointerdown" in html
    assert 'window.addEventListener("pointermove", (e) => App.moveQueueDrag(e), { passive: false })' in html
    assert 'window.addEventListener("pointerup", (e) => App.endQueueDrag(e, false))' in html
    assert 'window.addEventListener("pointercancel", (e) => App.endQueueDrag(e, true))' in html
    assert 'e.key !== "ArrowUp" && e.key !== "ArrowDown"' in html
    assert 'e.key === "ArrowUp" ? -1 : 1' in html
    assert "movebtn" not in html


def test_alias_remove_target_is_44px(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert ".aliasrow .xbtn { position: absolute; top: 0; right: 0; min-width: 44px; min-height: 44px; }" in html


def test_queue_reorder_commits_are_serialized(client):
    """Drag/keyboard reorders must not fire overlapping queue/reorder POSTs —
    commitOrder() has to guard re-entry, disable the drag handles for the
    duration of the request, and release the busy
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
    # and keyboard moveJob() must not touch the DOM order while one is pending.
    assert "if (this.reorderBusy) return;" in commit_body
    assert "if (this.reorderBusy) return;" in move_body

    # The busy flag must be set, and the handles disabled, before the request.
    set_busy_pos = commit_body.index("this.reorderBusy = true;")
    disable_pos = commit_body.index('c.querySelector(".grip").disabled = true;')
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

    assert 'c.querySelector(".grip").disabled = this.reorderBusy;' in html


def test_pointer_drag_commits_only_changed_order_and_cancel_restores(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert "beginQueueDrag(e, card)" in html
    assert "moveQueueDrag(e)" in html
    assert "endQueueDrag(e, cancelled)" in html
    assert "grip.setPointerCapture(e.pointerId)" in html
    assert "originalOrder:" in html
    assert "if (cancelled)" in html
    assert "for (const id of drag.originalOrder)" in html
    assert 'if (order.some((id, i) => id !== drag.originalOrder[i])) this.commitOrder();' in html


def test_theme_picker_and_three_palettes_are_present(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    for theme, label in (
        ("websharr-blue", "Websharr Blue"),
        ("osaka-jade", "Osaka Jade"),
        ("space-grey", "Space Grey"),
    ):
        assert f'value="{theme}"' in html
        assert label in html
    assert 'html[data-theme="osaka-jade"]' in html
    assert 'html[data-theme="space-grey"]' in html
    assert "applyTheme(theme)" in html
    assert 'uiPost("settings", { theme: next })' in html


def test_theme_filter_applies_to_brand_and_empty_state_logos(client):
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    for theme in ("osaka-jade", "space-grey"):
        selector = (
            f'html[data-theme="{theme}"] .brand img,\n'
            f'  html[data-theme="{theme}"] .empty img'
        )
        assert selector in html
