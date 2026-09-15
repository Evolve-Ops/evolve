"""Handover landing-page + admin API routes (V2.4-5).

Two surfaces share one module:

  • End-user, unauthenticated, served at the **root** of the host:
      GET  /handover/<token>              — friendly onboarding page
      POST /handover/<token>/onboard      — apply preferences + mark claimed

  • Operator-facing JSON API, mounted on the admin UI:
      POST /api/handover/generate         — generate / fetch / rotate a token
      GET  /api/handover/list             — list active tokens (for the modal)

The token IS the authentication on the public endpoints (matches Plex's
invite-link UX). The admin endpoints reuse the same network/session
gating as the rest of the admin server — handled at the network layer,
per ``feedback_ui_authorization_presumed``.

Voice tone for the landing page: thoughtful + warm, no jargon. The Plex
test (``feedback_design_constraint_mildly_tech_capable``) is the gate
— a brand-new user must complete onboarding without a Stack Overflow
search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..config import DEFAULT_SHARED_DIR, load_network
from ..handover import (
    DEFAULT_EXPIRES_IN_DAYS,
    KNOWN_AUDIENCES,
    VOICE_PRESETS,
    build_handover_url,
    claim_token,
    create_token,
    is_claimed,
    is_expired,
    is_usable,
    load_token,
    normalize_preferences,
    tokens_dir,
    write_preferences_to_bot,
)


def _shared(network_path: Path) -> Path:
    net = load_network(network_path)
    return Path(net.get("sharedDir") or DEFAULT_SHARED_DIR)


def _bot_display_name(network: dict, bot_id: str) -> str:
    bots = (network.get("bots") or {})
    cfg = bots.get(bot_id) or {}
    name = cfg.get("display_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return bot_id


def _operator_name(network: dict) -> str:
    """Best-effort operator label for the friendly-expired page."""
    pod = network.get("pod") or {}
    name = pod.get("operator_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "your administrator"


def _esc(s: Any) -> str:
    """Minimal HTML escape — keeps the templates self-contained."""
    if s is None:
        return ""
    t = str(s)
    return (
        t.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ── HTML templates ────────────────────────────────────────────────────────────
# Inline so this file is self-contained. Voice: warm, plain, no jargon.
# No references to RSI / generators / openclaw / proposals / charters.

_PAGE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       background: #fafafa; color: #1a1a1a; margin: 0; padding: 0;
       min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.card { background: white; border-radius: 14px; box-shadow: 0 4px 24px rgba(0,0,0,0.06);
        max-width: 480px; width: 100%; margin: 24px; padding: 32px; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
.greeting { color: #555; font-size: 0.95rem; margin: 0 0 24px; line-height: 1.5; }
.step { margin: 22px 0; }
.step label { display: block; font-size: 0.78rem; color: #666; font-weight: 600;
              text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 8px; }
input[type=text], select, textarea {
   width: 100%; box-sizing: border-box; padding: 10px 12px; font-size: 1rem;
   border: 1px solid #ddd; border-radius: 8px; background: #fcfcfc;
   font-family: inherit; }
textarea { resize: vertical; min-height: 68px; }
.choices { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.choice { padding: 10px 12px; background: #f4f4f4; border-radius: 8px; cursor: pointer;
          text-align: center; font-size: 0.92rem; transition: all 0.15s;
          border: 2px solid transparent; }
.choice:hover { background: #ececec; }
.choice.active { background: #eef4ff; border-color: #4a7fd6; }
.choice .sub { font-size: 0.72rem; color: #888; margin-top: 2px; }
button.primary { background: #2563eb; color: white; border: 0;
                 padding: 12px 22px; font-size: 1rem; border-radius: 8px;
                 cursor: pointer; width: 100%; margin-top: 16px; }
button.primary:hover { background: #1d4ed8; }
button.primary:disabled { background: #9aa6b8; cursor: not-allowed; }
.subtle { color: #888; font-size: 0.82rem; line-height: 1.5; }
.safety { margin-top: 24px; padding: 14px; background: #fffaf0;
          border-radius: 8px; border-left: 3px solid #f0b020; font-size: 0.82rem; color: #604010; }
.success { text-align: center; padding: 12px 0; }
.success .check { font-size: 3rem; color: #16a34a; line-height: 1; margin-bottom: 8px; }
.error-page { text-align: center; }
.error-page h1 { color: #444; }
"""


def _render_page(body: str, title: str = "Welcome") -> Response:
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>{_PAGE_CSS}</style>
</head>
<body><div class="card">{body}</div></body>
</html>"""
    return Response(html, mimetype="text/html")


def _render_onboard(rec: dict, network: dict) -> Response:
    bot_id = rec["bot_id"]
    display = _bot_display_name(network, bot_id)
    greeting = rec.get("message") or f"Welcome — your assistant is ready."

    voice_choices = "".join(
        f'<div class="choice" data-voice="{_esc(v)}" onclick="pickVoice(this)">'
        f"{_esc(v)}<div class=\"sub\">{_esc(_voice_blurb(v))}</div></div>"
        for v in VOICE_PRESETS
    )

    body = f"""
    <h1>Hello.</h1>
    <p class="greeting">{_esc(greeting)}</p>
    <p class="subtle">This takes about a minute. We'll tune {_esc(display)} to your style — you can change any of this later just by asking.</p>

    <form id="onboard-form" onsubmit="return submitForm(event)">
      <div class="step">
        <label>How should {_esc(display)} address you?</label>
        <input type="text" name="preferred_name" placeholder="Your name or nickname" maxlength="80" autocomplete="off">
      </div>

      <div class="step">
        <label>Voice</label>
        <div class="choices">{voice_choices}</div>
        <input type="hidden" name="voice" id="voice-hidden" value="">
      </div>

      <div class="step">
        <label>Anything else you'd like {_esc(display)} to know? (optional)</label>
        <textarea name="notes" placeholder="e.g. I'm usually in calls 9–11am, prefer brief check-ins, two kids whose names are…" maxlength="500"></textarea>
      </div>

      <button type="submit" class="primary" id="onboard-submit">All set — let's go</button>
    </form>

    <div class="safety">
      <strong>You're in control.</strong> Your assistant runs on hardware controlled by whoever installed it for you, not in the cloud. They can pause it at any time — just ask them, or use the command <code>evolve-admin pause-all</code> on the host.
    </div>

    <script>
    function pickVoice(el) {{
      document.querySelectorAll('.choice').forEach(c => c.classList.remove('active'));
      el.classList.add('active');
      document.getElementById('voice-hidden').value = el.dataset.voice;
    }}
    async function submitForm(e) {{
      e.preventDefault();
      const btn = document.getElementById('onboard-submit');
      btn.disabled = true;
      btn.textContent = 'Saving…';
      const fd = new FormData(e.target);
      const body = {{}};
      fd.forEach((v, k) => body[k] = v);
      try {{
        const r = await fetch(window.location.pathname + '/onboard', {{
          method: 'POST',
          headers: {{'content-type': 'application/json'}},
          body: JSON.stringify(body),
        }});
        const data = await r.json().catch(() => ({{}}));
        if (r.ok && data.ok) {{
          document.querySelector('.card').innerHTML = data.success_html;
        }} else {{
          btn.disabled = false;
          btn.textContent = 'Try again';
          alert((data && data.error) || 'Something went wrong. Try again, or ask the person who sent you the link.');
        }}
      }} catch (err) {{
        btn.disabled = false;
        btn.textContent = 'Try again';
        alert('Network error. Try again, or ask the person who sent you the link.');
      }}
      return false;
    }}
    </script>
    """
    return _render_page(body, title=f"Welcome — {display}")


def _voice_blurb(v: str) -> str:
    return {
        "Casual": "friendly, warm",
        "Concierge": "formal, attentive",
        "Buddy": "playful, easygoing",
        "Professional": "crisp, efficient",
    }.get(v, "")


def _render_expired_page(rec: dict | None, network: dict, kind: str) -> Response:
    """Friendly page for expired/claimed/missing tokens.

    ``kind`` ∈ {"expired", "already_claimed", "not_found"} — voice
    differs slightly. No traceback, no token echo.
    """
    op = _operator_name(network)
    if kind == "already_claimed":
        headline = "This link's already been used."
        body_text = (
            f"Looks like your assistant is already set up. Just say hi in your "
            f"usual chat app and {op if op == 'your administrator' else op} can "
            f"send a fresh link if you need one."
        )
    elif kind == "not_found":
        headline = "We don't recognize this link."
        body_text = (
            f"It may have been mistyped. Ask {op} for a fresh link."
        )
    else:  # expired
        headline = "This link's no longer active."
        body_text = (
            f"Setup links work for about a week. Ask {op} for a fresh one — "
            "they can run a quick command to generate it."
        )
    body = f"""
    <div class="error-page">
      <h1>{_esc(headline)}</h1>
      <p class="subtle">{_esc(body_text)}</p>
    </div>
    """
    return _render_page(body, title="Setup link")


def _render_success(rec: dict, network: dict) -> dict:
    bot_id = rec["bot_id"]
    display = _bot_display_name(network, bot_id)
    prefs = rec.get("preferences") or {}
    name = prefs.get("preferred_name") or "you"
    voice = prefs.get("voice") or prefs.get("voice_freeform") or ""
    voice_line = (
        f"<p class=\"subtle\">Voice: <strong>{_esc(voice)}</strong>.</p>"
        if voice else ""
    )

    # Channel hint — generic. There used to be a specific-channel variant here
    # ("Say hi on <strong>Telegram</strong>"), fed by a helper that read
    # bot.channel / bot.transport / bot.messaging off network.json. None of
    # those keys exist in the schema — nothing writes them — so the helper
    # returned "" fleet-wide and every user ever saw this generic line. Removed
    # rather than repaired: with multi-platform support the hint becomes
    # multi-valued ("on Telegram or Discord") and belongs on the channel
    # registry. Rebuild home is [META:users] M1-B4, not here.
    channel_line = (
        f"<p class=\"subtle\">{_esc(display)} is ready. "
        "Say hi in your usual chat app.</p>"
    )

    body = f"""
    <div class="success">
      <div class="check">✓</div>
      <h1>You're all set, {_esc(name)}.</h1>
      {voice_line}
      {channel_line}
    </div>
    <div class="safety">
      <strong>One more thing — you're in control.</strong> Your assistant
      can be paused at any time. If you ever want a break, ask the person who set
      this up, or have them run <code>evolve-admin pause-all</code> on the host.
    </div>
    """
    # Return the inner HTML; the page already has the card wrapper.
    return {"ok": True, "success_html": body}


# ── Route registration ────────────────────────────────────────────────────────


def register_handover_routes(app: Flask, network_path: Path) -> None:
    """Mount handover endpoints on ``app``.

    Public (token-gated):
      GET  /handover/<token>
      POST /handover/<token>/onboard

    Admin (network-gated):
      POST /api/handover/generate
      GET  /api/handover/list
    """

    @app.get("/handover/<token>")
    def handover_landing(token: str):
        if not _looks_like_token(token):
            return _render_expired_page(None, load_network(network_path), "not_found")
        network = load_network(network_path)
        shared = _shared(network_path)
        rec = load_token(shared, token)
        if rec is None:
            return _render_expired_page(None, network, "not_found")
        if is_expired(rec):
            return _render_expired_page(rec, network, "expired")
        if is_claimed(rec):
            return _render_expired_page(rec, network, "already_claimed")
        return _render_onboard(rec, network)

    @app.post("/handover/<token>/onboard")
    def handover_onboard(token: str):
        if not _looks_like_token(token):
            return jsonify({"ok": False, "error": "Unknown link."}), 404
        network = load_network(network_path)
        shared = _shared(network_path)
        raw = request.get_json(silent=True) or {}
        prefs = normalize_preferences(raw)
        rec, err = claim_token(shared, token, prefs)
        if err == "not_found":
            return jsonify({"ok": False, "error": "We don't recognize this link."}), 404
        if err == "expired":
            return jsonify({"ok": False, "error": "This link's no longer active."}), 410
        if err == "already_claimed":
            return jsonify({"ok": False, "error": "This link's already been used."}), 409
        # err is None → success. Apply prefs into the bot's workspace
        # (best-effort — claim still recorded even if write fails).
        try:
            write_preferences_to_bot(rec["bot_id"], prefs, shared, network=network)
        except Exception:
            pass
        return jsonify(_render_success(rec, network))

    # ── Admin API ─────────────────────────────────────────────────────────────

    @app.post("/api/handover/generate")
    def api_handover_generate():
        body = request.get_json(silent=True) or {}
        bot_id = (body.get("bot_id") or "").strip()
        if not bot_id:
            return jsonify({"ok": False, "error": "bot_id required"}), 400

        network = load_network(network_path)
        bots = network.get("bots") or {}
        if bot_id not in bots:
            return jsonify({
                "ok": False,
                "error": f"{bot_id!r} isn't registered. Use 'evolve-admin add-bot' first.",
            }), 404

        audience = body.get("audience") or "personal_bot_user"
        if audience not in KNOWN_AUDIENCES:
            audience = "personal_bot_user"
        message = (body.get("message") or "").strip()
        try:
            expires_in_days = int(body.get("expires_in_days") or DEFAULT_EXPIRES_IN_DAYS)
        except (TypeError, ValueError):
            expires_in_days = DEFAULT_EXPIRES_IN_DAYS
        rotate = bool(body.get("rotate"))

        shared = _shared(network_path)
        rec, created = create_token(
            shared,
            bot_id=bot_id,
            audience=audience,
            message=message,
            expires_in_days=expires_in_days,
            rotate=rotate,
        )
        url = build_handover_url(network, rec["token"])
        return jsonify({
            "ok": True,
            "created": created,
            "rotated": bool(rotate and created),
            "token": rec["token"],
            "bot_id": rec["bot_id"],
            "url": url,
            "expires_at": rec["expires_at"],
            "message": rec.get("message", ""),
            "audience": rec.get("audience", audience),
        })

    @app.get("/api/handover/list")
    def api_handover_list():
        network = load_network(network_path)
        shared = _shared(network_path)
        out: list[dict] = []
        for f in tokens_dir(shared).glob("*.json"):
            try:
                import json
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            url = build_handover_url(network, rec.get("token", ""))
            out.append({
                "token": rec.get("token"),
                "bot_id": rec.get("bot_id"),
                "audience": rec.get("audience"),
                "message": rec.get("message", ""),
                "created_at": rec.get("created_at"),
                "expires_at": rec.get("expires_at"),
                "claimed_at": rec.get("claimed_at"),
                "url": url,
                "usable": is_usable(rec),
            })
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return jsonify({"ok": True, "tokens": out})


def _looks_like_token(s: str) -> bool:
    """Cheap shape check before touching disk — keeps probes from
    creating phantom 404 log entries downstream."""
    if not isinstance(s, str):
        return False
    if len(s) != 32:
        return False
    return all(c in "0123456789abcdef" for c in s)
