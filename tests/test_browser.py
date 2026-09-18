"""Acceptance tests: the real frontend, in a real browser, against the real app.

These are the tests the manual checklist in PROGRESS.md used to stand in for.
Each one maps to a failure mode that actually shipped or that would be silent
until an operator hit it:

1. ``RUN`` on /pairs types; it does not write. The word cost two Postgres
   decisions once, from a user who believed they were typing in a search box.
2. A /control field survives a live re-render — same DOM node, same focus,
   typed value intact, and the value reaches the wire. The page repaints on
   every control frame and on a 1 s tick; rebuilding its cards made every
   field uneditable after about two keystrokes.
3. The focus ladder: an arrow from ``ARB>`` enters the list, a letter then
   reaches the page (and writes), Escape comes home and the same letter is
   typing again. This is the contract the whole scope model rests on.
4. The network security floor: a cross-site POST is refused, a same-origin
   one is not, and the same holds for the WebSocket handshake.
5. Every page boots clean: no uncaught exception, no console error. A page
   module that fails to import degrades to a stub, which is easy to miss.

Run them with ``uv run pytest tests/test_browser.py``; exclude them from a
fast loop with ``-m "not browser"``. They skip themselves where there is no
Chrome, so a bare CI container stays green.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from tests.browser import (
    Browser,
    TerminalState,
    chrome_available,
    guarded_app,
    serve,
    terminal,
)

pytestmark = pytest.mark.browser

needs_chrome = pytest.mark.skipif(not chrome_available(), reason="no Chrome/Chromium on this host")

PAIRS_READY = 'document.querySelectorAll("#pair-rows .pair-row").length === 3'
CMD_TEXT = 'document.getElementById("cmd-text").textContent'


def eventually(predicate: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    """Poll a server-side condition until it holds. No bare sleeps."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def focus_settles(page: Browser, element_id: str) -> None:
    page.wait_for(
        f'document.activeElement && document.activeElement.id === "{element_id}"',
        f"focus to land on #{element_id}",
    )


# --------------------------------------------------------------------------
# 1. the "RUN" test — the one that matters
# --------------------------------------------------------------------------


@needs_chrome
def test_typing_run_on_pairs_types_and_never_decides() -> None:
    """R, U, N with nothing focused must be three characters, not three acts.

    R reloads the list, U sets the selected pair PROPOSED and N sets it
    REJECTED — every one of them a write to the dataset that drives trading.
    They are LIST-scope keys, and the home position is COMMAND scope, so the
    only correct outcome of typing the word is the word.
    """
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/pairs", ready=PAIRS_READY)
        # The home position: every page mounts with the keyboard on ARB>.
        assert page.focused() == "cmd"

        page.clear()  # forget the mount's own GET /api/pairs
        page.type("run")

        page.wait_for(f'{CMD_TEXT} === "RUN"', 'the ARB> buffer to read "RUN"')
        # Not one request touched the pairs API: no decision POST, and not
        # even R's refetch.
        assert [r for r in page.requests() if "/api/pairs" in r.url] == []
        assert state.decisions == []

        # Escape clears the half-typed command and leaves the buffer empty.
        page.key("Escape")
        page.wait_for(f'{CMD_TEXT} === ""', "Escape to clear the command line")
        assert state.decisions == []


# --------------------------------------------------------------------------
# 2. the control-field test
# --------------------------------------------------------------------------


FIELD_STATE = """
(() => {
  const e = document.getElementById("ctl-minnet");
  return {
    same: e === window.__arbProbeNode,
    probe: e.__arbProbe || null,
    focused: document.activeElement === e,
    active: document.activeElement ? (document.activeElement.id
      || "<" + document.activeElement.tagName + ">") : "<none>",
    value: e.value,
  };
})()
"""


@needs_chrome
def test_control_field_survives_live_rerenders_and_reaches_the_wire() -> None:
    """Type into a numeric limit while the page repaints under you.

    The defect was node REPLACEMENT: render() rebuilt its cards with
    replaceChildren on every control frame and on the 1 s tick, so the
    <input> was destroyed after about two keystrokes and focus fell to
    <body>. Hence the three assertions: same node (tagged, then compared by
    identity), focus still inside it, and the characters still there. The
    click at the end proves the value was not merely on screen — it is what
    the action posts.
    """
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/control", ready='document.getElementById("ctl-minnet") !== null')
        page.wait_ws_live()
        # The server's value, written in by render() before anyone touched it:
        # 50 ticks, shown in the operator's unit — cents per contract.
        assert page.eval('document.getElementById("ctl-minnet").value') == "0.50"

        # TAB is the documented way into the fields, so use it rather than a
        # programmatic focus(): the focus path is part of what broke.
        page.key("Tab")
        focus_settles(page, "ctl-minnet")
        page.eval(
            '(() => { const e = document.getElementById("ctl-minnet");'
            ' e.__arbProbe = "tagged"; window.__arbProbeNode = e; e.select(); return true; })()'
        )

        page.type("0.7")

        # Drive a real re-render: a control frame is what the live plane
        # broadcasts after every action, and the page repaints on it.
        state.push_control(tracked_pairs=4242)
        page.wait_for(
            'document.getElementById("control-page").textContent.indexOf("4,242") >= 0',
            "the control frame to repaint the page",
        )

        after_frame = page.eval(FIELD_STATE)
        assert after_frame["same"] is True, "the <input> was replaced by the re-render"
        assert after_frame["probe"] == "tagged"
        assert after_frame["focused"] is True, f"focus fell to {after_frame['active']}"
        assert after_frame["value"] == "0.7"

        # Keep typing across the repaint, then wait for the 1 s tick — the
        # other path that used to rebuild the DOM under the caret.
        page.type("5")
        # Wait for EVIDENCE the tick ran, not for the clock. A wall-clock sleep
        # here made the assertions below vacuous: deleting the setInterval from
        # control.js left this test green, because time passes either way.
        # render() rewrites textContent every tick, so a MutationObserver sees it.
        page.eval(
            """window.__arbTicks = 0;
               window.__arbObs = new MutationObserver((rs) => { window.__arbTicks += rs.length; });
               window.__arbObs.observe(document.getElementById("control-page"),
                 { subtree: true, childList: true, characterData: true });"""
        )
        page.wait_for(
            "window.__arbTicks > 0",
            "the 1 s control tick to actually repaint the page",
            timeout=10.0,
        )

        after_tick = page.eval(FIELD_STATE)
        assert after_tick["same"] is True, "the 1 s tick replaced the <input>"
        assert after_tick["focused"] is True, f"focus fell to {after_tick['active']}"
        assert after_tick["value"] == "0.75"

        # And the edit is what the action sends, not the server's old value —
        # converted exactly: 0.75¢ is 75 ticks.
        page.click('button[data-action="paper.limits"]')
        eventually(
            lambda: any(a == "paper.limits" for a, _p, _c in state.controls),
            "APPLY LIMITS to post",
        )
        params = next(p for a, p, _c in state.controls if a == "paper.limits")
        assert params is not None and params["min_net_ticks"] == 75


# --------------------------------------------------------------------------
# 3. keyboard / focus integration
# --------------------------------------------------------------------------


@needs_chrome
def test_arrow_enters_the_list_a_letter_then_writes_escape_comes_home() -> None:
    """The scope ladder, end to end, on the page that has the writes.

    Same keystroke, two outcomes, decided only by where the focus is: this is
    the positive half of the "RUN" test, and it is what makes that test's
    silence meaningful rather than accidental.
    """
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/pairs", ready=PAIRS_READY)
        assert page.focused() == "cmd"

        # An arrow from the home position moves FOCUS, not the cursor.
        page.key("ArrowDown")
        focus_settles(page, "pair-rows")

        page.clear()
        page.key("n")  # LIST scope: the page owns the letter, and it writes
        eventually(lambda: state.decisions == [(101, "rejected")], "the REJECT to post")
        posts = [r for r in page.requests() if r.hits("/api/pairs/101/decide", "POST")]
        assert len(posts) == 1

        # Escape leaves the list and disarms the page.
        page.key("Escape")
        focus_settles(page, "cmd")

        # The very same letter is typing again.
        page.clear()
        page.key("n")
        page.wait_for(f'{CMD_TEXT} === "N"', "the letter to type at ARB>")
        assert state.decisions == [(101, "rejected")]
        assert [r for r in page.requests() if "/decide" in r.url] == []


# --------------------------------------------------------------------------
# 4. the security floor (plain HTTP and a raw WebSocket — no browser)
# --------------------------------------------------------------------------


def test_cross_site_requests_are_refused_and_same_origin_ones_are_not() -> None:
    """The guard ``run_ui`` wraps the app in, exercised over the wire.

    Done with a plain client rather than through the browser so the headers
    are exactly what the test says they are: a browser would not let a page
    forge ``Origin`` and the result would prove less.
    """
    state = TerminalState()
    with serve(guarded_app(state)) as base_url:
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            cross = client.post(
                "/api/control/recording.stop",
                json={},
                headers={"Origin": "https://evil.example"},
            )
            marked = client.post(
                "/api/control/recording.stop",
                json={},
                headers={"Origin": base_url, "Sec-Fetch-Site": "cross-site"},
            )
            same = client.post("/api/control/recording.stop", json={}, headers={"Origin": base_url})
            read = client.get("/api/control", headers={"Origin": "https://evil.example"})

        ws_url = "ws://" + base_url.removeprefix("http://") + "/ws"
        with pytest.raises(WebSocketException):
            # The same-origin policy does not apply to WebSocket, so Origin is
            # the only thing standing between a cross-site page and the feed.
            with connect(ws_url, additional_headers={"Origin": "https://evil.example"}):
                pass
        with connect(ws_url, additional_headers={"Origin": base_url}) as allowed:
            hello = allowed.recv()

    assert cross.status_code == 403
    assert marked.status_code == 403
    # Through the guard and into the executor: this stub has no control plane,
    # so 409 is the honest answer and proves the request was not refused.
    assert same.status_code == 409 and "control plane" in same.json()["error"]
    # A cross-site GET is a read, and reads are not state-changing.
    assert read.status_code == 200
    assert '"t": "hello"' in (hello if isinstance(hello, str) else hello.decode())
    # Exactly one request reached the executor: the same-origin POST.
    assert [a for a, _p, _c in state.controls] == ["recording.stop"]


# --------------------------------------------------------------------------
# 5. every page boots clean
# --------------------------------------------------------------------------

PAGES = (
    ("/", "monitor-page"),
    ("/arb", "arbpage"),
    ("/pairs", "pairs"),
    ("/paper", "paperpage"),
    ("/system", "system-page"),
    ("/control", "control-page"),
    ("/help", "help-page"),
    ("/market/kalshi%3AAAA", "des"),
)


@needs_chrome
def test_every_route_cold_loads_without_a_console_error() -> None:
    """Each page URL is a deep link, and a broken module is a silent stub.

    main.js catches an import failure and substitutes a placeholder page, so a
    module that throws costs you a whole screen with no other symptom than one
    console.error. This is the test that reads it.
    """
    state = TerminalState()
    with terminal(state) as page:
        for path, root in PAGES:
            page.clear()
            page.goto(path, ready=f'!document.getElementById("{root}").hidden')
            page.wait_ws_live()
            bad = [line for line in page.console() if line.level in ("error", "exception")]
            assert bad == [], f"{path} logged {bad}"


@needs_chrome
def test_a_blurred_edit_is_not_silently_reverted_by_the_next_frame() -> None:
    """Type a limit, TAB away, then let a control frame land.

    The focused-field guard is only half of it. `syncField` also refuses to
    write into a field you have EDITED but left — otherwise you type a new
    min-net, tab to the next box, one frame arrives (they arrive on every
    change anywhere) and your edit is silently replaced by the server's old
    value. APPLY then posts a number you never chose, which is the worst
    shape this can take: no error, no visible change, wrong write.
    """
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/control", ready='document.getElementById("ctl-minnet") !== null')
        page.wait_ws_live()

        page.key("Tab")
        focus_settles(page, "ctl-minnet")
        page.eval('document.getElementById("ctl-minnet").select()')
        page.type("0.75")
        # Leave the field the way an operator would: on to the next limit.
        page.key("Tab")
        page.wait_for(
            'document.activeElement.id !== "ctl-minnet"',
            "focus to leave the edited field",
        )

        state.push_control(tracked_pairs=4242)
        page.wait_for(
            'document.body.textContent.indexOf("4,242") >= 0',
            "the control frame to repaint the page",
        )

        assert page.eval('document.getElementById("ctl-minnet").value') == "0.75", (
            "a blurred-but-edited field was reverted by the next control frame"
        )

        page.click('button[data-action="paper.limits"]')
        eventually(
            lambda: any(a == "paper.limits" for a, _p, _c in state.controls),
            "APPLY LIMITS to post",
        )
        params = next(p for a, p, _c in state.controls if a == "paper.limits")
        assert params is not None and params["min_net_ticks"] == 75


@needs_chrome
def test_a_leaned_on_decision_key_writes_once_not_once_per_autorepeat() -> None:
    """Hold Y in the pairs list: one decision, not one per repeat.

    `decidePair` advances the cursor under a status filter, so without the
    e.repeat guard a stuck key walks the queue writing as it goes — five
    Postgres decisions from one press. Both layers guard it (core/keys.js
    drops a repeated printable in LIST, and the page refuses e.repeat), and
    neither guard was covered by a real browser until this test.
    """
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/pairs", ready=PAIRS_READY)
        page.wait_ws_live()

        page.key("ArrowDown")
        focus_settles(page, "pair-rows")

        page.key("y")  # the real press
        for _ in range(4):
            page.key("y", repeat=True)  # what a held key sends after it

        eventually(lambda: len(state.decisions) >= 1, "the first decision to post")
        # Give the repeats every chance to land before counting.
        time.sleep(0.5)
        assert len(state.decisions) == 1, (
            f"a held key wrote {len(state.decisions)} decisions: {state.decisions}"
        )


# --------------------------------------------------------------------------
# 7. the depth panel shows exactly the book it was sent
# --------------------------------------------------------------------------

C = 10_000  # Qty units per contract


def _book(bids: list[list[int]], asks: list[list[int]], **extra: object) -> dict[str, object]:
    return {
        "t": "book",
        "market_id": "kalshi:AAA",
        "bids": bids,
        "asks": asks,
        "valid": True,
        "reason": None,
        "age_ms": 0.0,
        "ts_ms": int(time.time() * 1000),
        **extra,
    }


def _text(page: Browser, element_id: str) -> str:
    return page.eval(f'document.getElementById("{element_id}").textContent')


def _cell(page: Browser, rank: int, css: str) -> str:
    return page.eval(
        f"document.querySelector('#ladder .ladder-row[data-rank=\"{rank}\"] {css}').textContent"
    )


@needs_chrome
def test_the_depth_panel_prints_the_book_it_was_sent_and_never_a_mid_it_cannot_stand_behind() -> (
    None
):
    """The redesigned DEPTH panel is mostly canvas, and a canvas cannot be
    read back — so every number it draws is also DOM, and this checks the DOM
    against the frame, digit for digit, in a real browser.

    Prices are ticks of $0.0001 and sizes 1e-4 contracts: a misplaced factor
    anywhere between the wire and the ladder shows up here as a wrong string.
    Then the book goes structurally invalid, and the panel must stop stating a
    mid anywhere — the hero says INVALID and the ladder greys, but every level
    stays printed, because an operator needs to see what the untrusted book
    claims."""
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/")
        page.wait_ws_live()
        page.wait_for(
            'document.body.classList.contains("has-sel")', "the first market to be selected"
        )

        state.broadcast(
            _book([[5100, 101 * C + 2020], [5000, 4 * C + 9156]], [[5200, 18 * C + 788]])
        )
        page.wait_for('document.getElementById("dp-mid").textContent === "51.50"', "the mid")

        assert _text(page, "dp-bb") == "51.00"
        assert _text(page, "dp-ba") == "52.00"
        assert _text(page, "dp-midsub").startswith("SPR 1.00¢ · 1 TICK · NO 48.50%")
        # the touch row: YES, its NO complement, exact size and cumulative
        assert _cell(page, 0, ".yes.c-b") == "51.00"
        assert _cell(page, 0, ".no.c-b") == "49.00"
        assert _cell(page, 0, ".qty.c-b") == "101.202"
        assert _cell(page, 0, ".yes.c-a") == "52.00"
        assert _cell(page, 0, ".no.c-a") == "48.00"
        assert _cell(page, 0, ".qty.c-a") == "18.0788"
        # cumulative depth is the exact running sum, fractions and all
        assert _cell(page, 1, ".cum.c-b") == "106.1176"
        # the ask side ends after one level, and says so without inventing one
        end_a = page.eval(
            "document.querySelector('#ladder .ladder-row[data-rank=\"1\"]').dataset.endA"
        )
        assert "END OF BOOK · 1 LVL" in end_a
        assert _cell(page, 1, ".qty.c-a") == ""
        label = page.eval('document.getElementById("dp-scope").getAttribute("aria-label")')
        assert "Bids 106.1176 contracts over 2 levels" in label

        state.broadcast(_book([[5100, 7 * C]], [[5200, 3 * C]], valid=False, reason="seq_gap"))
        page.wait_for(
            'document.getElementById("depth-banner").textContent'
            '.indexOf("INVALID · SEQ GAP") === 0',
            "the invalid banner",
        )
        assert _text(page, "dp-mid") == "INVALID"
        assert "UNTRUSTED" in _text(page, "dp-bblbl")
        assert page.eval('document.getElementById("ladder").classList.contains("dim")')
        # still printed: an untrusted book is greyed, never hidden
        assert _cell(page, 0, ".qty.c-b") == "7"
        assert _cell(page, 0, ".yes.c-a") == "52.00"

        errors = [c for c in page.console() if c.level == "error"]
        assert not errors, errors


# --------------------------------------------------------------------------
# 8. SYSTEM gives a verdict, and a problem leads it
# --------------------------------------------------------------------------


@needs_chrome
def test_system_gives_a_verdict_and_a_dropped_recording_leads_it() -> None:
    """SYSTEM used to print counters and leave the judgement to the reader.
    It now has to say, in words, whether anything is wrong — and when the
    recorder starts losing messages, that must become the headline and the
    first check, with the fix attached, without anyone hunting for it."""
    state = TerminalState()
    # Healthy venues, so the recorder is the only thing that can go wrong.
    state.kalshi_status = lambda: ("live", "last frame 200ms ago")  # type: ignore[method-assign]
    state.polymarket_status = lambda: ("polled", "REST polling 3 markets")  # type: ignore[method-assign]
    stats = {
        "t": "stats",
        "msg_total": 500,
        "msg_rate_1s": 12.0,
        "parse_errors": 0,
        "seq_gaps": 0,
        "ws_clients": 1,
        "uptime_s": 300.0,
        "latency_ms": {"last": 40.0, "median": 38.0, "p95": 60.0, "n": 200},
        "rtt_ms": 70.0,
        "clock_skew_ms": 3.0,
        "recorder": {"enqueued": 500, "dropped": 0},
    }
    with terminal(state) as page:
        page.goto("/system")
        page.wait_ws_live()
        state.broadcast(stats)
        page.wait_for(
            'document.querySelector("#sysc-recorder .sysc-tag").textContent === "OK"',
            "the recorder check to read OK",
        )
        page.wait_for(
            'document.querySelector("#sysc-kalshi .sysc-tag").textContent === "OK"',
            "the status poll to land",
        )
        assert "PROBLEM" not in page.text(".sysv-head")

        state.broadcast({**stats, "recorder": {"enqueued": 900, "dropped": 25}})
        page.wait_for(
            'document.querySelector(".sysv-head").textContent === "1 PROBLEM"',
            "the verdict to name the problem",
        )
        assert "RECORDER" in page.text(".sysv-line")
        first = page.eval('document.querySelector("#sys-checks .sysc").id')
        assert first == "sysc-recorder", "problems sort to the top"
        assert "25 MESSAGES LOST" in page.text("#sysc-recorder .sysc-reading")
        # Nobody picked a row, so the detail pane follows the worst check.
        assert page.text(".sysd-name") == "RECORDER"
        assert page.eval('!document.querySelector(".sysd-fix").hidden')

        errors = [c for c in page.console() if c.level == "error"]
        assert not errors, errors


# --------------------------------------------------------------------------
# 9. /control: what it sends, and when it asks first
# --------------------------------------------------------------------------


def _with_universe(state: TerminalState) -> None:
    state.control["universe"] = {
        "kalshi": {
            "tickers": ["KXBASE-A", "KXBASE-B", "KXLEG-1"],
            "base": ["KXBASE-A", "KXBASE-B"],
            "pairs": ["KXLEG-1"],
            "attached": True,
        },
        "polymarket_us": {
            "slugs": ["base-a", "leg-1"],
            "base": ["base-a"],
            "pairs": ["leg-1"],
            "attached": True,
        },
    }


@needs_chrome
def test_the_universe_box_edits_the_base_list_and_never_writes_pair_legs_into_it() -> None:
    """The old box showed base markets PLUS the legs of watched pairs, and
    SUBSCRIBE wrote that whole list back as the base — one press turned every
    watched pair's leg into a permanent base market. The box now holds the
    base only, the legs are counted beside it, and what is posted is exactly
    what is in the box."""
    state = TerminalState()
    _with_universe(state)
    with terminal(state) as page:
        page.goto("/control", ready='document.getElementById("ctl-ktickers") !== null')
        page.wait_ws_live()
        page.wait_for(
            'document.getElementById("ctl-ktickers").value === "KXBASE-A\\nKXBASE-B"',
            "the box to hold the base list, without the pair leg",
        )
        assert "1 legs of watched pairs" in page.text("#csec-kalshi .ccount")
        apply_sel = 'button[data-action="universe.kalshi"]'
        assert page.eval(f"document.querySelector('{apply_sel}').disabled"), (
            "APPLY with nothing changed is a dead press"
        )

        page.eval(
            '(() => { const e = document.getElementById("ctl-ktickers");'
            ' e.value += "\\nkxnew-c"; e.dispatchEvent(new Event("input")); })()'
        )
        page.wait_for(f"!document.querySelector('{apply_sel}').disabled", "APPLY to enable")
        assert "+1 added" in page.text("#csec-kalshi .cdraft")
        page.click(apply_sel)
        # A set-replacing action is PRICED first: a preview, not an execution.
        page.wait_for(
            'document.querySelector("#csec-kalshi .cconfirm") !== null',
            "the confirm box, inside the section that asked",
        )
        assert state.previews == [
            ("universe.kalshi", {"tickers": ["KXBASE-A", "KXBASE-B", "KXNEW-C"]})
        ]
        assert not any(a == "universe.kalshi" for a, _p, _c in state.controls)
        assert "would universe.kalshi" in page.text("#csec-kalshi .cconfirm-eff")

        page.click("#csec-kalshi .cconfirm .cbtn.primary")
        eventually(
            lambda: any(a == "universe.kalshi" for a, _p, _c in state.controls),
            "CONFIRM to post the real action",
        )
        sent = next(p for a, p, _c in state.controls if a == "universe.kalshi")
        assert sent == {"tickers": ["KXBASE-A", "KXBASE-B", "KXNEW-C"]}, (
            "the leg must not ride along"
        )


@needs_chrome
def test_a_pending_confirmation_survives_another_sections_action() -> None:
    """One pending confirmation, one owner. Running DOCTOR used to clear
    `armed` wholesale, so a watch-set confirm in another section vanished
    without a word — and with it the sentence the operator was reading."""
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/control", ready='document.getElementById("ctl-pairstop") !== null')
        page.wait_ws_live()
        page.click('button[data-action="pairs.top"].primary')
        page.wait_for(
            'document.querySelector("#csec-watch .cconfirm") !== null', "the watch-set confirm box"
        )
        # The section that is waiting on an answer locks its own buttons.
        assert page.eval(
            """document.querySelector('button[data-action="pairs.top"].primary').disabled"""
        )

        page.click('button[data-action="jobs.doctor"]')
        eventually(
            lambda: any(a == "jobs.doctor" for a, _p, _c in state.controls), "DOCTOR to post"
        )
        page.wait_for(
            'document.querySelector("#csec-jobs .creceipt") !== null',
            "the jobs receipt, in the jobs section",
        )
        assert page.eval('document.querySelector("#csec-watch .cconfirm") !== null'), (
            "another section's action dismissed a pending confirmation"
        )
        assert not any(a == "pairs.top" for a, _p, _c in state.controls)


@needs_chrome
def test_an_invalid_limit_cannot_be_applied_and_says_which_field() -> None:
    """APPLY is a button that writes risk limits to a live trader. It enables
    only for a valid change, names the field that is wrong, and a value that is
    not a whole number of ticks is refused — never rounded."""
    state = TerminalState()
    with terminal(state) as page:
        page.goto("/control", ready='document.getElementById("ctl-minnet") !== null')
        page.wait_ws_live()
        apply_sel = 'button[data-action="paper.limits"]'
        assert page.eval(f"document.querySelector('{apply_sel}').disabled")

        page.key("Tab")
        focus_settles(page, "ctl-minnet")
        page.eval('document.getElementById("ctl-minnet").select()')
        page.type("0.505")
        page.wait_for(
            'document.querySelector("#csec-paper .cform-hint.bad") !== null', "the field error"
        )
        assert "nearest 0.01" in page.text("#csec-paper .cform-hint.bad")
        assert page.eval(f"document.querySelector('{apply_sel}').disabled")

        page.eval('document.getElementById("ctl-minnet").select()')
        page.type("0.6")
        page.wait_for(f"!document.querySelector('{apply_sel}').disabled", "APPLY to enable")
        assert "0.50¢ → 0.60¢" in page.text("#csec-paper .cdraft")
        assert state.controls == [], "nothing is sent until APPLY is pressed"
