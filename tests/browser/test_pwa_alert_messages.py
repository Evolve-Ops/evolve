"""Browser smoke for the Recent Messages truncate+expand and Dispatcher
Health panel — PWA-polish PR ``pwa-polish-alert-messages``.

Three regressions this guards against:
  1. Long messages should truncate by default; tapping the row reveals
     the full body. (The original failure mode: 5+ rows of HTTP error
     payload per failed-send entry, on phone.)
  2. The Dispatcher Health panel renders the all-green one-liner when
     ``delivery-failures.jsonl`` is empty and the degraded summary
     when entries are present.
  3. The mobile (360px) layout stacks each row as a card via
     ``.resp-table`` rather than overflowing horizontally.

The tests stub the two underlying JSON endpoints via ``page.route`` so
they don't depend on the server's actual ``{shared_dir}/alerts/`` state.
That keeps these tests deterministic across CI runs.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "pytest_playwright",
    reason="install pytest-playwright + browsers to run cross-browser smoke",
)


def _subs_payload(
    events,
    *,
    group_id="system_health",
    group_label="System health",
    group_description="Pod daemons, gateways, and the alerting system.",
    group_enabled=True,
    is_safety_critical=False,
):
    """Minimal Phase-2 GET /api/alerts/subscriptions payload wrapping
    ``events`` (a list of catalog-event dicts) into ONE operator-facing
    Subscription group. The Configure tab and the Messages-tab provenance
    both read the 13-group shape (``subscriptions[].events[]`` +
    ``event_index``), so a single group is enough for these tests.

    The group's ``enabled`` is the on/off the toggle controls; the member
    events carry their own severity/frequency but no per-event gate."""
    member_keys = [e["key"] for e in events]
    return {
        "channel": "telegram",
        "chat_id": "123",
        "digest_hour_local": 8,
        "dispatcher_enabled": True,
        "pwa_push_enabled": False,
        "subscriptions": [
            {
                "id": group_id,
                "label": group_label,
                "description": group_description,
                "enabled": group_enabled,
                "is_safety_critical": is_safety_critical,
                "is_overridden": False,
                "default_enabled": True,
                "member_event_count": len(member_keys),
                "member_event_keys": member_keys,
                "events": events,
            }
        ],
        "event_index": {k: group_id for k in member_keys},
    }


def _route_alerts(page, *, dispatcher_log=None, failures=None,
                  subscriptions=None) -> None:
    """Stub both alerts endpoints so the page renders predictable data.

    WebKit + Playwright route interception fights with the admin UI's
    service worker registration (even though sw.js explicitly skips
    ``/api/`` paths, the SW being active confuses Playwright's network
    interceptor on WebKit specifically — same routes work fine on
    Chromium / Firefox). Prevent the SW from registering AT ALL via
    add_init_script before any page script runs; routes then intercept
    cleanly across the engine matrix.
    """
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )

    def _handle_log(route, request):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"records": dispatcher_log or []}),
        )

    def _handle_failures(route, request):
        summary = {
            "total": len(failures or []),
            "window_hours": 24,
            "most_recent_ts": (failures or [{}])[0].get("ts") if failures else None,
            "by_channel": [
                {"channel": "telegram", "count": len(failures or []),
                 "sample_error": (failures or [{}])[0].get("error", "")}
            ] if failures else [],
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "records": failures or [],
                "summary": summary,
            }),
        )

    def _handle_subs(route, request):
        # Only stub the catalog GET. The inline-toggle POST is captured by a
        # test-specific route registered AFTER this one (Playwright routes are
        # last-registered-first), so fall through here to let it win.
        if request.method != "GET":
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(subscriptions or _subs_payload([])),
        )

    page.route("**/api/alerts/dispatcher-log*", _handle_log)
    page.route("**/api/alerts/delivery-failures*", _handle_failures)
    # The Messages tab now also fetches the subscription catalog to resolve
    # each row's catalog_event into provenance.
    page.route("**/api/alerts/subscriptions", _handle_subs)


def _open_reports_subscriptions(page, admin_server: str) -> None:
    """Force-activate Reports → Subscriptions → Messages without going
    through the SPA's nav() flow.

    The full nav() path has multiple async side effects — page summary
    band loaders, chat-drawer page change, persisted-subtab restore,
    initial-load _restoreNav re-firing nav() after localStorage writes —
    any of which can re-render ``#al-activity-body`` AFTER the first
    render completes. WebKit's strict element-stability check then
    treats every subsequent ``row.click()`` as targeting a detached
    node and times out.

    For these tests we only care that the JS that renders the activity
    list does the right thing given mocked data. Force the page active
    via class manipulation, call ``_alLoadActivity`` once explicitly,
    await its in-flight promise to resolve. Single render, no nav-
    triggered side effects, deterministic across all three engines.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Wait for the home-page load() to settle so its async fetches
    # don't compete for the network with our mocked-route resolution.
    # Settle signal is "the home-page load() has updated #last-refresh".
    # The prefix is "As of " (was "Refreshed" prior to PR #2159 — the
    # full 'Refreshed HH:MM:SS PM' overflowed the 220px sidebar footer
    # once the reload icon took some of its width). Either prefix is a
    # fine settle signal — we just need *something* there other than
    # the "Loading…" placeholder.
    page.wait_for_function(
        "() => { const el = document.getElementById('last-refresh');"
        "        return el && el.textContent && el.textContent !== 'Loading…'; }",
        timeout=15_000,
    )
    # Activate the reports page directly. Skips nav() and all the
    # cross-page side effects it triggers (drawer refresh, page summary
    # loaders, persisted-subtab restore that re-fires nav).
    page.evaluate(
        """
        () => {
          document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
          document.getElementById('page-reports').classList.add('active');
        }
        """
    )
    # Call the activity loader exactly once and wait for it to settle.
    # The in-flight guard prevents a double-fire even if something else
    # later tries to re-call it; we wait for it to clear so the row is
    # both rendered and stable.
    page.evaluate("_alLoadActivity()")
    page.wait_for_function(
        """
        () => typeof _alLoadActivity_inflight !== 'undefined'
              && _alLoadActivity_inflight === null
              && document.getElementById('al-activity-body') !== null
        """,
        timeout=10_000,
    )


_LONG_BODY = (
    "🟢 Daemon error spike resolved\n"
    "host_health: errors back below threshold; pod_health: gateways green; "
    "audit: no new findings in last sweep — everything looks clear after the "
    "spike at 13:42 UTC. subscription: system.daemon_error_spike"
)


def test_messages_row_truncates_long_body_by_default(page, admin_server: str) -> None:
    """A long message renders with the truncated span visible and the
    full span hidden until the row is clicked."""
    _route_alerts(page, dispatcher_log=[
        {"ts": "2099-01-01T12:00:00Z", "source": "signal_notifier",
         "result": "sent", "channel": "telegram",
         "message_excerpt": _LONG_BODY},
    ])
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg", timeout=5000)

    state = page.evaluate(
        """
        () => {
          const row = document.querySelector('tr.alert-msg');
          if (!row) return {found: false};
          const truncated = row.querySelector('.alert-msg-truncated');
          const full = row.querySelector('.alert-msg-full');
          return {
            found: true,
            expanded: row.getAttribute('data-expanded'),
            truncatedDisplay: truncated ? getComputedStyle(truncated).display : null,
            fullDisplay: full ? getComputedStyle(full).display : null,
            truncatedText: truncated ? truncated.textContent : null,
          };
        }
        """
    )
    assert state["found"]
    assert state["expanded"] == "false"
    assert state["truncatedDisplay"] != "none", "truncated copy hidden by default"
    assert state["fullDisplay"] == "none", "full body should be hidden by default"
    assert state["truncatedText"].endswith("…"), (
        f"truncated copy must end with ellipsis; got {state['truncatedText']!r}"
    )
    assert len(state["truncatedText"]) <= 130, (
        f"truncated copy too long: {len(state['truncatedText'])} chars"
    )


def test_messages_row_expands_on_click_and_collapses_again(
    page, admin_server: str,
) -> None:
    """Click toggles the data-expanded attribute; CSS swaps the visible
    span. A second click collapses again.

    Drives the click through ``page.evaluate`` rather than
    ``Locator.click`` so the test doesn't interact with Playwright's
    element-stability retry loop. (On CI WebKit the test was timing out
    not on toggle behavior but on stability — the cross-engine
    behaviour we care about is "click event → data-expanded flip", and
    JS click dispatch is identical across engines.)
    """
    _route_alerts(page, dispatcher_log=[
        {"ts": "2099-01-01T12:00:00Z", "source": "signal_notifier",
         "result": "sent", "channel": "telegram",
         "message_excerpt": _LONG_BODY},
    ])
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg")
    # The wait_for_selector above is the real settle signal: the mocked
    # alert routes resolve synchronously, so once the row exists the data
    # is rendered. We deliberately do NOT wait_for_load_state("networkidle")
    # here — the SPA's unmocked /api/health poller keeps the network busy,
    # so networkidle is racy and was a source of goto/wait timeouts.

    # Drive the full click sequence in a single evaluate so DOM state
    # is observed at each step without re-entering Playwright's auto-
    # wait between dispatches.
    timeline = page.evaluate(
        """
        () => {
          const row = document.querySelector('tr.alert-msg');
          if (!row) return {found: false};
          const snapshot = () => {
            const truncated = row.querySelector('.alert-msg-truncated');
            const full = row.querySelector('.alert-msg-full');
            return {
              expanded: row.getAttribute('data-expanded'),
              truncatedDisplay: truncated ? getComputedStyle(truncated).display : null,
              fullDisplay: full ? getComputedStyle(full).display : null,
            };
          };
          const click = () => row.dispatchEvent(
            new MouseEvent('click', {bubbles: true, cancelable: true})
          );
          const initial = snapshot();
          click();
          const afterOpen = snapshot();
          click();
          const afterClose = snapshot();
          return {found: true, initial, afterOpen, afterClose};
        }
        """
    )
    assert timeline["found"]
    # Initial: collapsed.
    assert timeline["initial"]["expanded"] == "false", timeline
    assert timeline["initial"]["fullDisplay"] == "none", timeline
    assert timeline["initial"]["truncatedDisplay"] != "none", timeline
    # After click 1: expanded.
    assert timeline["afterOpen"]["expanded"] == "true", timeline
    assert timeline["afterOpen"]["fullDisplay"] == "block", timeline
    assert timeline["afterOpen"]["truncatedDisplay"] == "none", timeline
    # After click 2: collapsed again.
    assert timeline["afterClose"]["expanded"] == "false", timeline
    assert timeline["afterClose"]["fullDisplay"] == "none", timeline
    assert timeline["afterClose"]["truncatedDisplay"] != "none", timeline


def test_dispatcher_health_panel_renders_all_green_when_empty(
    page, admin_server: str,
) -> None:
    """No delivery failures → the panel renders the OK one-liner.

    Pin specifically: the panel mounts (not removed entirely), uses the
    ``.ok`` modifier, and the body says deliveries succeeded.
    """
    _route_alerts(page, dispatcher_log=[], failures=[])
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector(".dispatcher-health", timeout=5000)

    health = page.locator(".dispatcher-health")
    assert "ok" in (health.get_attribute("class") or "")
    assert "deliveries succeeded" in health.inner_text().lower()


def test_dispatcher_health_panel_renders_degraded_summary(
    page, admin_server: str,
) -> None:
    """Failures present → panel shows degraded summary with channel
    breakdown and a "View failures →" toggle."""
    failure = {
        "ts": "2099-01-01T12:00:00Z", "source": "audit", "result": "failed",
        "channel": "telegram",
        "error": "telegram http 400: bad parse entities",
        "message_excerpt": _LONG_BODY,
    }
    _route_alerts(
        page,
        dispatcher_log=[],
        failures=[failure, failure],
    )
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector(".dispatcher-health.degraded", timeout=5000)

    health_text = page.evaluate(
        "() => document.querySelector('.dispatcher-health').innerText"
    )
    assert "2 deliveries failed" in health_text

    # Drive the toggle click via evaluate so we don't fight Playwright's
    # element-stability retry under any residual re-render.
    result = page.evaluate(
        """
        () => {
          const toggle = document.querySelector('.dispatcher-health-toggle');
          if (!toggle) return {found: false};
          toggle.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const card = document.querySelector('.dispatcher-health');
          const rows = document.querySelectorAll(
            '.dispatcher-health-detail tr.alert-msg'
          );
          return {
            found: true,
            expanded: card.getAttribute('data-failures-expanded'),
            rowCount: rows.length,
          };
        }
        """
    )
    assert result["found"], "View failures toggle should render"
    assert result["expanded"] == "true", result
    assert result["rowCount"] == 2, result


def test_alloadactivity_inflight_guard_present(page, admin_server: str) -> None:
    """nav('reports') fires _alLoadActivity via onPageActivate AND a
    second time via the persisted-subtab click. The two async loads
    raced on every engine and detached the table mid-click under WebKit's
    strict element-stability check.

    Pin the in-flight guard so a future refactor can't silently drop it:
    when _alLoadActivity is called twice in a row, the second call
    returns the SAME promise as the first (not a fresh fetch). Same
    pattern for _alLoadDispatcherHealth.
    """
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    # Both functions should exist as guards on the page-level scope.
    activity_has_guard = page.evaluate("typeof _alLoadActivity_inflight !== 'undefined'")
    health_has_guard = page.evaluate("typeof _alLoadDispatcherHealth_inflight !== 'undefined'")
    assert activity_has_guard, "_alLoadActivity_inflight guard missing"
    assert health_has_guard, "_alLoadDispatcherHealth_inflight guard missing"


def test_messages_table_stacks_as_cards_at_phone_width(
    page, admin_server: str,
) -> None:
    """At 360px each <tr> should render as a stacked card, not a
    horizontally-overflowing row.

    Detection: the .resp-table primitive promotes <td> to ``display:
    block`` under the phone media query. We sniff computed style rather
    than measuring layout pixels so this is robust across engines.
    """
    page.set_viewport_size({"width": 360, "height": 720})
    _route_alerts(page, dispatcher_log=[
        {"ts": "2099-01-01T12:00:00Z", "source": "signal_notifier",
         "result": "sent", "channel": "telegram",
         "message_excerpt": _LONG_BODY},
    ])
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg")

    display = page.evaluate(
        "() => getComputedStyle(document.querySelector("
        "  'tr.alert-msg > td'"
        ")).display"
    )
    assert display in ("block", "flex"), (
        f"expected card-stack layout (block/flex td) at 360px; got {display!r}"
    )


# ── Subscription provenance + inline enable/disable (subscription-completeness
#    workstream C) ──────────────────────────────────────────────────────────

_PROV_EVENT = {
    "key": "system.daemon_error_spike",
    "label": "Daemon error spike",
    "description": "A pod daemon logged a burst of errors.",
    "severity": "warn",
    "is_safety_critical": False,
    "default_enabled": True,
    "default_frequency": "immediate",
    "allowed_frequencies": [{"key": "immediate", "label": "Immediate"}],
    "subscription": {
        "event_key": "system.daemon_error_spike",
        "enabled": True,
        "frequency": "immediate",
        "is_overridden": False,
    },
}

_SAFETY_EVENT = {
    "key": "meta.dispatcher_health",
    "label": "Dispatcher health",
    "description": "Alert delivery itself is failing.",
    "severity": "critical",
    "is_safety_critical": True,
    "default_enabled": True,
    "default_frequency": "immediate",
    "allowed_frequencies": [{"key": "immediate", "label": "Immediate"}],
    "subscription": {
        "event_key": "meta.dispatcher_health",
        "enabled": True,
        "frequency": "immediate",
        "is_overridden": False,
    },
}


def test_expanded_row_shows_subscription_provenance(
    page, admin_server: str,
) -> None:
    """Expanding a message row reveals which subscription it belongs to:
    the catalog label, the event_key, and an inline toggle button."""
    _route_alerts(
        page,
        dispatcher_log=[
            {"ts": "2099-01-01T12:00:00Z", "source": "signal_notifier",
             "result": "sent", "channel": "telegram",
             "catalog_event": "system.daemon_error_spike",
             "message_excerpt": _LONG_BODY},
        ],
        subscriptions=_subs_payload([_PROV_EVENT]),
    )
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg")

    state = page.evaluate(
        """
        () => {
          const row = document.querySelector('tr.alert-msg');
          row.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const prov = row.querySelector('.alert-msg-prov');
          const toggle = row.querySelector('.alert-msg-prov-toggle');
          return {
            hasProv: !!prov,
            provText: prov ? prov.innerText : null,
            toggleText: toggle ? toggle.textContent.trim() : null,
            toggleEnabledAttr: toggle ? toggle.dataset.enabled : null,
          };
        }
        """
    )
    assert state["hasProv"], "provenance block should render in the expanded row"
    # Phase 2: provenance shows the GROUP the event belongs to (label +
    # description) and the member event_key as the "subscription:" tag.
    assert "System health" in state["provText"], state
    assert "system.daemon_error_spike" in state["provText"], state
    # Group is enabled → toggle offers to disable it.
    assert state["toggleEnabledAttr"] == "1", state
    assert state["toggleText"] == "Disable this subscription", state


def test_unresolvable_catalog_event_degrades_without_toggle(
    page, admin_server: str,
) -> None:
    """A row whose catalog_event isn't in the live catalog still shows the
    raw key as provenance, but offers no toggle."""
    _route_alerts(
        page,
        dispatcher_log=[
            {"ts": "2099-01-01T12:00:00Z", "source": "signal_notifier",
             "result": "sent", "channel": "telegram",
             "catalog_event": "system.gone_missing",
             "message_excerpt": _LONG_BODY},
        ],
        subscriptions=_subs_payload([_PROV_EVENT]),
    )
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg")

    state = page.evaluate(
        """
        () => {
          const row = document.querySelector('tr.alert-msg');
          row.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const prov = row.querySelector('.alert-msg-prov');
          const toggle = row.querySelector('.alert-msg-prov-toggle');
          return {
            provText: prov ? prov.innerText : null,
            hasToggle: !!toggle,
          };
        }
        """
    )
    assert state["provText"] and "system.gone_missing" in state["provText"], state
    assert not state["hasToggle"], "no toggle for an unresolvable catalog_event"


def test_safety_critical_disable_triggers_confirm(
    page, admin_server: str,
) -> None:
    """Disabling a safety-critical subscription from the expanded row pops the
    themed confirm modal before the PUT. Cancelling it cancels the change; the
    PUT must not fire and the toggle stays 'Disable…'. (DOM confirmModal(), not
    a native dialog — those are suppressed in the desktop webview.)"""
    put_calls = []

    def _capture_put(route, request):
        if request.method == "POST":
            put_calls.append(request.post_data)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"updated": []}))
        else:
            route.fallback()

    _route_alerts(
        page,
        dispatcher_log=[
            {"ts": "2099-01-01T12:00:00Z", "source": "meta",
             "result": "sent", "channel": "telegram",
             "catalog_event": "meta.dispatcher_health",
             "message_excerpt": _LONG_BODY},
        ],
        subscriptions=_subs_payload(
            [_SAFETY_EVENT],
            group_id="alerting_system",
            group_label="Alerting system",
            is_safety_critical=True,
        ),
    )
    # Register AFTER _route_alerts so this POST handler wins (LIFO).
    page.route("**/api/alerts/subscriptions", _capture_put)
    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg")

    # First: CANCEL the confirm modal → no PUT, toggle unchanged. The confirm
    # is the themed DOM confirmModal() (dom-utils.js), not a native dialog —
    # native confirm()/alert() are silently suppressed in the desktop webview.
    page.evaluate(
        """
        () => {
          const row = document.querySelector('tr.alert-msg');
          row.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const toggle = row.querySelector('.alert-msg-prov-toggle');
          toggle.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        }
        """
    )
    page.wait_for_selector(".confirm-modal-overlay", timeout=5000)
    confirm_body = page.eval_on_selector(
        ".confirm-modal-body", "el => el.textContent"
    )
    assert "safety-critical" in confirm_body.lower(), confirm_body
    page.click('.confirm-modal-overlay [data-cm="cancel"]')
    page.wait_for_selector(".confirm-modal-overlay", state="detached", timeout=5000)
    assert not put_calls, "cancelling the confirm must not fire the PUT"

    state_after_dismiss = page.evaluate(
        "() => document.querySelector('.alert-msg-prov-toggle').dataset.enabled"
    )
    assert state_after_dismiss == "1", "toggle should stay enabled after cancel"

    # Second: CONFIRM → PUT fires with enabled:false, toggle flips.
    page.evaluate(
        """
        () => {
          const toggle = document.querySelector('.alert-msg-prov-toggle');
          toggle.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        }
        """
    )
    page.wait_for_selector('.confirm-modal-overlay [data-cm="ok"]', timeout=5000)
    page.click('.confirm-modal-overlay [data-cm="ok"]')
    page.wait_for_function(
        "() => document.querySelector('.alert-msg-prov-toggle')"
        "        && document.querySelector('.alert-msg-prov-toggle').dataset.enabled === '0'",
        timeout=5000,
    )
    assert put_calls, "accepting the confirm must fire the POST"
    sent = json.loads(put_calls[-1])
    # Phase 2: the inline toggle operates on the GROUP, not the event.
    assert sent.get("subscription_id") == "alerting_system", sent
    assert "event_key" not in sent, sent
    assert sent.get("enabled") is False, sent


def test_subscription_toggle_failed_put_does_not_falsely_flip(
    page, admin_server: str,
) -> None:
    """A failed subscription PUT (HTTP error → ``api()`` returns ``{error}``,
    it does not throw) must NOT flip the toggle to 'success'. The operator
    sees an error toast and the toggle stays in its prior (enabled) state.
    (The error was previously a native alert(); those are silently suppressed
    in the desktop webview, so it now surfaces via the themed toast.)"""
    def _fail_put(route, request):
        if request.method == "POST":
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"error": "boom"}))
        else:
            route.fallback()

    _route_alerts(
        page,
        dispatcher_log=[
            {"ts": "2099-01-01T12:00:00Z", "source": "signal_notifier",
             "result": "sent", "channel": "telegram",
             "catalog_event": _PROV_EVENT["key"],
             "message_excerpt": _LONG_BODY},
        ],
        subscriptions=_subs_payload([_PROV_EVENT]),
    )
    # Register AFTER _route_alerts so this POST handler wins (LIFO).
    page.route("**/api/alerts/subscriptions", _fail_put)

    _open_reports_subscriptions(page, admin_server)
    page.wait_for_selector("tr.alert-msg")

    before = page.evaluate(
        """
        () => {
          const row = document.querySelector('tr.alert-msg');
          row.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const t = row.querySelector('.alert-msg-prov-toggle');
          return t ? t.dataset.enabled : null;
        }
        """
    )
    assert before == "1", f"toggle should start enabled, got {before!r}"

    # Click the (Disable) toggle → failing PUT → must revert, not flip.
    page.evaluate(
        """
        () => document.querySelector('tr.alert-msg .alert-msg-prov-toggle')
                .dispatchEvent(new MouseEvent('click', {bubbles: true}))
        """
    )
    page.wait_for_timeout(300)

    after = page.evaluate(
        "() => document.querySelector('tr.alert-msg .alert-msg-prov-toggle').dataset.enabled"
    )
    assert after == "1", f"failed PUT must NOT flip the toggle, got {after!r}"
    page.wait_for_selector("#toast.show", timeout=5000)
    toast_text = page.eval_on_selector("#toast", "el => el.textContent")
    assert "Failed to update subscription" in toast_text, toast_text


# ── Configure tab — 13 subscription groups (Phase 2 chip 2) ─────────────────
#
# The Configure inner-tab renders the operator-facing GROUPS (not the ~65
# per-event toggles). Each group has one enable/disable toggle wired to
# POST {subscription_id, enabled}; member events render read-only inside an
# expandable list; safety-critical groups warn on disable.

_GROUP_EVENT_A = {
    "key": "system.daemon_error_spike",
    "label": "Daemon error spike",
    "description": "A pod daemon logged a burst of errors.",
    "severity": "warn",
    "is_safety_critical": False,
    "default_enabled": True,
    "default_frequency": "immediate",
    "allowed_frequencies": [{"key": "immediate", "label": "Immediate"}],
    "subscription_id": "pod_gateway_health",
    "subscription": {
        "event_key": "system.daemon_error_spike",
        "enabled": True, "frequency": "immediate", "is_overridden": False,
    },
}


def _route_configure(page, subscriptions) -> None:
    """Stub the GET catalog + the lazy push-devices fetch the Configure
    render kicks off, and let the toggle POST fall through to a
    test-registered handler (LIFO)."""
    page.add_init_script(
        "Object.defineProperty(navigator, 'serviceWorker', {value: undefined});"
    )

    def _handle_subs(route, request):
        if request.method != "GET":
            route.fallback()
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(subscriptions))

    def _handle_push(route, request):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"subscriptions": [], "pwa_push_enabled": False}))

    page.route("**/api/alerts/subscriptions", _handle_subs)
    page.route("**/api/push/subscriptions", _handle_push)


def _open_reports_configure(page, admin_server: str) -> None:
    """Force-activate Reports → Subscriptions → Configure and render the
    group list once via _alLoadSubscriptions (skips nav() side effects, same
    rationale as _open_reports_subscriptions)."""
    page.goto(f"{admin_server}/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => { const el = document.getElementById('last-refresh');"
        "        return el && el.textContent && el.textContent !== 'Loading…'; }",
        timeout=15_000,
    )
    page.evaluate(
        """
        () => {
          document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
          document.getElementById('page-reports').classList.add('active');
        }
        """
    )
    page.evaluate("_alLoadSubscriptions()")
    # The Configure list renders into #al-subs-body, which lives inside the
    # (inactive) configure inner-subtab — so the group rows are present in the
    # DOM but not *visible*. We assert on render correctness via JS (checked
    # state, dispatched events, datasets) which works on hidden nodes, so wait
    # for the rows to be ATTACHED rather than visible.
    page.wait_for_selector(".al-subs-group", state="attached", timeout=10_000)


def test_configure_renders_groups_not_per_event_toggles(
    page, admin_server: str,
) -> None:
    """Configure lists the operator-facing GROUPS — one toggle per group,
    not one per member event."""
    payload = _subs_payload(
        [_GROUP_EVENT_A],
        group_id="pod_gateway_health",
        group_label="Pod & gateway health",
        group_description="Daemons, gateways, and watchdogs.",
    )
    _route_configure(page, payload)
    _open_reports_configure(page, admin_server)

    state = page.evaluate(
        """
        () => {
          const groups = document.querySelectorAll('.al-subs-group');
          const first = groups[0];
          return {
            groupCount: groups.length,
            groupId: first ? first.dataset.subscriptionId : null,
            text: first ? first.innerText : '',
            toggleCount: first ? first.querySelectorAll('input[type=checkbox]').length : 0,
          };
        }
        """
    )
    assert state["groupCount"] == 1, state
    assert state["groupId"] == "pod_gateway_health", state
    assert "Pod & gateway health" in state["text"], state
    # Exactly one toggle for the group (no per-event checkboxes).
    assert state["toggleCount"] == 1, state


def test_configure_group_toggle_posts_subscription_id(
    page, admin_server: str,
) -> None:
    """Toggling a (non-safety) group POSTs {subscription_id, enabled} —
    the per-event ``enabled`` PUT surface is gone."""
    posts = []

    def _capture(route, request):
        if request.method == "POST":
            posts.append(request.post_data)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"updated_groups": [], "updated_events": []}))
        else:
            route.fallback()

    payload = _subs_payload(
        [_GROUP_EVENT_A],
        group_id="pod_gateway_health",
        group_label="Pod & gateway health",
    )
    _route_configure(page, payload)
    page.route("**/api/alerts/subscriptions", _capture)  # LIFO — wins for POST
    _open_reports_configure(page, admin_server)

    page.evaluate(
        """
        () => {
          const box = document.querySelector('.al-subs-group input[type=checkbox]');
          box.checked = false;
          box.dispatchEvent(new Event('change', {bubbles: true}));
        }
        """
    )
    page.wait_for_timeout(300)
    assert posts, "group toggle should POST"
    sent = json.loads(posts[-1])
    assert sent.get("subscription_id") == "pod_gateway_health", sent
    assert sent.get("enabled") is False, sent
    assert "event_key" not in sent, sent


def test_configure_safety_critical_group_disable_confirms(
    page, admin_server: str,
) -> None:
    """Disabling a safety-critical group pops the themed confirm modal;
    cancelling it skips the POST and re-checks the box. (DOM confirmModal(),
    not a native dialog — those are suppressed in the desktop webview.)"""
    posts = []

    def _capture(route, request):
        if request.method == "POST":
            posts.append(request.post_data)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"updated_groups": []}))
        else:
            route.fallback()

    payload = _subs_payload(
        [_GROUP_EVENT_A],
        group_id="security_findings",
        group_label="Security findings",
        is_safety_critical=True,
    )
    _route_configure(page, payload)
    page.route("**/api/alerts/subscriptions", _capture)
    _open_reports_configure(page, admin_server)

    page.evaluate(
        """
        () => {
          const box = document.querySelector('.al-subs-group input[type=checkbox]');
          box.checked = false;
          box.dispatchEvent(new Event('change', {bubbles: true}));
        }
        """
    )
    page.wait_for_selector(".confirm-modal-overlay", timeout=5000)
    confirm_body = page.eval_on_selector(
        ".confirm-modal-body", "el => el.textContent"
    )
    assert "safety-critical" in confirm_body.lower(), confirm_body
    page.click('.confirm-modal-overlay [data-cm="cancel"]')
    page.wait_for_selector(".confirm-modal-overlay", state="detached", timeout=5000)
    assert not posts, "cancelling the confirm must not POST"
    page.wait_for_function(
        "() => document.querySelector('.al-subs-group input[type=checkbox]').checked === true",
        timeout=5000,
    )
