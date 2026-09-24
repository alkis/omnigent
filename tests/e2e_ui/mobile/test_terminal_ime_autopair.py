"""E2E: an IME auto-pair must not corrupt the next composed candidate.

A mobile touch keyboard that auto-inserts paired punctuation puts ``()``
into xterm's helper textarea and leaves the caret *between* the pair.
xterm 6's ``CompositionHelper`` ignores the caret entirely:

- ``_handleAnyTextareaChanges`` (the keydown-229 path that forwards the
  pair) diffs textarea values only, so no cursor-left reaches the PTY:
  the terminal cursor stays after ``)`` while the textarea caret sits
  inside the pair.
- ``compositionstart`` records ``textarea.value.length`` (the value's
  end, not the caret) as the composition start, so when the following
  Chinese composition commits, the substring forwarded to the PTY is the
  text *after* that position — the trailing ``)`` — instead of the
  composed candidate.

Journey (mirrors the report; xterm's listeners on ``term.textarea`` do
not check ``isTrusted``, so synthetic IME events drive its real
composition state, exactly as in the sibling composition tests):

1. On a phone-profile browser, open a session and a shell pane (kebab →
   Shells → "New shell", the mobile entry point).
2. Focus the terminal. The touch keyboard inserts ``()`` with the caret
   between the pair: ``keydown(229)`` → textarea ``()`` / selection 1 →
   ``input`` → ``keyup(229)``.
3. Compose ``ni`` and select 你 without moving the caret out of the
   pair: composition events ending in ``compositionend(你)`` with the
   textarea at ``(你)`` / selection 2.

Expected: the composed candidate 你 reaches the PTY and the input stream
never decodes to ``())``. Observed on the unfixed build: the pair is
forwarded with no caret sync and the composition commit sends ``)``, so
the PTY input decodes to ``())`` and the pane echoes ``())``.
"""

from __future__ import annotations

import os
import re
import time

from playwright.sync_api import Browser, Page, ViewportSize, expect

# iPhone-12-class portrait viewport — below the Tailwind ``md`` breakpoint,
# so the mobile kebab navigation (the phone user's entry point) renders.
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# The candidate the user selects from the IME after composing "ni".
_CANDIDATE = "你"

# What a mobile keyboard's auto-pairing does: while the IME is active
# (keydown 229), the pair lands in the textarea with the caret between it.
_AUTO_PAIR_REPLAY = """(ta) => {
  const key = (type) => {
    const ev = new KeyboardEvent(type, {
      key: "Process", bubbles: true, cancelable: true,
    });
    Object.defineProperty(ev, "keyCode", { value: 229 });
    return ev;
  };
  ta.dispatchEvent(key("keydown"));
  ta.value += "()";
  ta.selectionStart = ta.selectionEnd = ta.value.length - 1;
  ta.dispatchEvent(new InputEvent("input", {
    data: "()", inputType: "insertText", bubbles: true, composed: true,
  }));
  ta.dispatchEvent(key("keyup"));
}"""

# One IME preedit update: replace the previous preedit at the caret with
# the new one, exactly as a browser mutates the textarea mid-composition.
_COMPOSITION_STEP = """(ta, { prev, next }) => {
  const start = ta.selectionStart - prev.length;
  ta.dispatchEvent(new CompositionEvent("compositionupdate", { data: next, bubbles: true }));
  ta.value = ta.value.slice(0, start) + next + ta.value.slice(start + prev.length);
  ta.selectionStart = ta.selectionEnd = start + next.length;
  ta.dispatchEvent(new InputEvent("input", {
    data: next, inputType: "insertCompositionText", bubbles: true, composed: true,
  }));
}"""

_COMPOSITION_END = """(ta, data) => {
  ta.dispatchEvent(new CompositionEvent("compositionend", { data, bubbles: true }));
  ta.dispatchEvent(new InputEvent("input", {
    data, inputType: "insertCompositionText", bubbles: true, composed: true,
  }));
}"""


def _capture_attach_frames(page: Page) -> tuple[list[bytes], list[bytes]]:
    """Record every frame sent/received on terminal-attach WebSockets.

    Registered before navigation so neither the relay attach nor a later
    direct-loopback re-dial can slip through. Frames are normalized to
    bytes (keystrokes go up as binary; text frames are UTF-8 encoded).

    :param page: Playwright page, not yet navigated.
    :returns: ``(sent, received)`` lists that fill in as frames flow.
    """
    sent: list[bytes] = []
    received: list[bytes] = []

    def _as_bytes(payload: str | bytes) -> bytes:
        return payload if isinstance(payload, bytes) else payload.encode("utf-8")

    def _on_ws(ws: object) -> None:
        url = ws.url  # type: ignore[attr-defined]
        if "/attach" not in url:
            return
        ws.on("framesent", lambda payload: sent.append(_as_bytes(payload)))  # type: ignore[attr-defined]
        ws.on("framereceived", lambda payload: received.append(_as_bytes(payload)))  # type: ignore[attr-defined]

    page.on("websocket", _on_ws)
    return sent, received


def _wait_for_sent_bytes(page: Page, sent: list[bytes], needle: bytes, timeout_s: float) -> bool:
    """Poll until *needle* appears in the concatenated sent frames."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if needle in b"".join(sent):
            return True
        page.wait_for_timeout(100)
    return needle in b"".join(sent)


def _open_new_shell_mobile(page: Page) -> None:
    """Open a shell the way a phone user does: kebab → Shells → New shell."""
    page.get_by_role("button", name="Conversation actions").click()
    shells_entry = page.get_by_role("menuitem", name="Shells", exact=True)
    expect(shells_entry).to_be_visible(timeout=10_000)
    shells_entry.click()
    drawer = page.get_by_test_id("shells-panel-drawer")
    expect(drawer).to_have_attribute("data-state", "open")
    drawer.get_by_role("button", name="New shell").click()


def test_ime_autopair_then_candidate_reaches_pty(
    browser: Browser, terminal_session: tuple[str, str]
) -> None:
    """The candidate composed inside an IME auto-pair must reach the PTY.

    Fails on the unfixed build: the composition commit sends the pair's
    trailing ``)`` instead of the candidate, so the PTY input decodes to
    ``())`` and the composed text never arrives.

    :param browser: Playwright browser; the test opens its own phone-profile
        context (the default ``page`` fixture is not mobile).
    :param terminal_session: ``(base_url, session_id)`` of a runner-bound
        session whose agent declares a shell (``terminals:`` block).
    """
    base_url, session_id = terminal_session

    context_kwargs: dict[str, object] = {
        "viewport": _MOBILE_VIEWPORT,
        "has_touch": True,
        "is_mobile": True,
    }
    # The e2e_ui recording hook patches the async API only; opt this sync
    # context into recording explicitly when a run requests it.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**context_kwargs)
    try:
        page = context.new_page()
        sent, _received = _capture_attach_frames(page)
        page.goto(f"{base_url}/c/{session_id}")

        _open_new_shell_mobile(page)
        terminal_view = page.locator('[data-testid="terminal-view"]:visible').first
        expect(terminal_view).to_be_visible(timeout=60_000)
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)

        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()

        # Sanity: prove the frame capture and the input path are live before
        # asserting on an absence — a plain keystroke must show up as a sent
        # frame, otherwise the real assertions below could fail for capture
        # reasons rather than the bug.
        page.keyboard.type("q")
        assert _wait_for_sent_bytes(page, sent, b"q", timeout_s=10), (
            "attach WebSocket frame capture saw no keystroke frame; "
            f"sent so far: {b''.join(sent)!r}"
        )
        page.keyboard.press("Backspace")

        # Step 2 — the touch keyboard auto-inserts the pair, caret inside it.
        textarea.evaluate(_AUTO_PAIR_REPLAY)
        assert _wait_for_sent_bytes(page, sent, b"()", timeout_s=5), (
            "the auto-pair itself never reached the PTY; the replay did not "
            f"drive xterm's input path. Sent so far: {b''.join(sent)!r}"
        )

        # Step 3 — compose "ni" at the caret and select the candidate.
        textarea.evaluate(
            '(ta) => ta.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }))'
        )
        textarea.evaluate(_COMPOSITION_STEP, {"prev": "", "next": "n"})
        page.wait_for_timeout(100)
        textarea.evaluate(_COMPOSITION_STEP, {"prev": "n", "next": "ni"})
        page.wait_for_timeout(100)

        # The preedit overlay is up: the composition is genuinely in flight.
        composition_view = terminal_view.locator(".composition-view")
        expect(composition_view).to_have_class(re.compile(r"\bactive\b"))
        expect(composition_view).to_have_text("ni")

        textarea.evaluate(_COMPOSITION_STEP, {"prev": "ni", "next": _CANDIDATE})
        textarea.evaluate(_COMPOSITION_END, _CANDIDATE)

        committed = _wait_for_sent_bytes(page, sent, _CANDIDATE.encode("utf-8"), timeout_s=5)
        # Let the PTY echo paint before judging, so a failure is visible in
        # the pane (and in a recording) as the corrupted `())` line.
        page.wait_for_timeout(1_000)
        all_sent = b"".join(sent)
        assert committed, (
            "the composed candidate never reached the PTY: the auto-pair's "
            "caret position was dropped and the composition commit picked up "
            f"the trailing ')'. Input sent after focus: {all_sent!r}"
        )
        assert b"())" not in all_sent, (
            "the terminal sent the corrupted '())' byte stream to the PTY "
            f"instead of keeping the candidate inside the pair: {all_sent!r}"
        )
        # Order matters, not just presence: pair, then a realigning
        # cursor-left (CSI or SS3, per DECCKM), then the candidate.
        pair_at = all_sent.find(b"()")
        assert pair_at != -1, f"the pair was not sent intact: {all_sent!r}"
        left_positions = [
            pos
            for pos in (all_sent.find(seq, pair_at) for seq in (b"\x1b[D", b"\x1bOD"))
            if pos != -1
        ]
        assert left_positions, f"no realigning cursor-left followed the pair: {all_sent!r}"
        assert all_sent.find(_CANDIDATE.encode("utf-8"), min(left_positions)) != -1, (
            f"the candidate did not follow the cursor-left: {all_sent!r}"
        )
    finally:
        context.close()
