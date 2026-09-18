"""Screenshot, or filmstrip, the terminal in headless Chrome.

Usage:
  uv run python scripts/snap.py OUT.png                       # the whole page
  uv run python scripts/snap.py OUT --selector '#depth' --frames 6 --interval 120
  uv run python scripts/snap.py OUT.png --market polymarket_us:fed-oct-2026-hold \\
      --until 'document.getElementById("dp-tag").textContent.includes("HELD")'
  uv run python scripts/snap.py OUT.png --width 1280 --reduced

Points at scripts/preview_ui.py (port 8765) by default; `--base` for another
server. With `--frames N` the output is a prefix and frames are written as
OUT-00.png, OUT-01.png ...: motion is judged across frames, not from one
still. Captures at device scale 2 and prints any console errors, so a page
that broke is not mistaken for a page that looks right.
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.browser import Browser, _chrome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--path", default="/")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--selector", help="clip to this element (default: the viewport)")
    ap.add_argument("--market", help="click this market's MONITOR row first")
    ap.add_argument("--until", help="wait for this JS expression to be truthy")
    ap.add_argument("--settle", type=float, default=2.0, help="seconds to let the page run")
    ap.add_argument("--frames", type=int, default=1)
    ap.add_argument("--interval", type=int, default=120, help="ms between frames")
    ap.add_argument("--reduced", action="store_true", help="emulate prefers-reduced-motion")
    args = ap.parse_args()

    with _chrome() as cdp:
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": args.width, "height": args.height, "deviceScaleFactor": 2, "mobile": False},
        )
        if args.reduced:
            cdp.send(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]},
            )
        page = Browser(cdp, args.base)
        page.goto(args.path)
        page.wait_ws_live()
        time.sleep(1.0)
        if args.market:
            sel = f'.mon-row[data-id="{args.market}"]'
            page.eval(f"document.querySelector({sel!r}).click()")
        if args.until:
            page.wait_for(args.until, "the --until condition", timeout=60)
        time.sleep(args.settle)

        clip = None
        if args.selector:
            box = page.eval(
                f"(() => {{ const r = document.querySelector({args.selector!r})"
                ".getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })()"
            )
            clip = {"x": box[0], "y": box[1], "width": box[2], "height": box[3], "scale": 1}

        for i in range(args.frames):
            params: dict[str, object] = {"format": "png"}
            if clip:
                params["clip"] = clip
            data = cdp.send("Page.captureScreenshot", params)["data"]
            out = args.out if args.frames == 1 else f"{args.out}-{i:02d}.png"
            Path(out).write_bytes(base64.b64decode(data))
            if i + 1 < args.frames:
                time.sleep(args.interval / 1000)

        for line in page.console():
            if line.level in ("error", "warning"):
                print(f"console {line.level}: {line.text[:200]}")
        print(f"wrote {args.frames} frame(s) to {args.out}")


if __name__ == "__main__":
    main()
