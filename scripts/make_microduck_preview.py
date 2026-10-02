#!/usr/bin/env python3
"""Create a compact GIF from actual worker frames and measured behavior intervals."""

import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--frames", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/media/microduck-archon-behaviors.gif",
    )
    args = p.parse_args()
    report = json.loads(args.report.read_text())
    result = report.get("result", report)
    frames = sorted(args.frames.glob("*.png"))
    labels = {
        "sitstand": "Sit and stand",
        "ground_pick": "Ground peck",
        "kick_left": "Left kick (no ball)",
        "kick_right": "Right kick (no ball)",
        "roulade": "Forward roll",
    }
    previews = []
    for step in result["steps"]:
        obs = step["observation"]
        motion = obs.get("motion") or {}
        behavior = motion.get("behavior")
        if behavior not in labels:
            continue
        # One rendered frame per five 20ms policy ticks; stop adds settling after motion.
        end = obs["simulation_time"] - 0.45
        start = max(0, end - motion["samples"] * 0.02)
        for index in range(max(0, int(start * 10)), min(len(frames), int(end * 10)), 2):
            with Image.open(frames[index]) as source:
                preview = source.convert("RGB").resize(
                    (280, 210), Image.Resampling.LANCZOS
                )
            draw = ImageDraw.Draw(preview)
            draw.rectangle((0, 0, 280, 22), fill="white")
            draw.text((8, 6), "Archon / Microduck: " + labels[behavior], fill="black")
            previews.append(preview.quantize(colors=32))
    if not previews:
        raise SystemExit("No measured behavior frames")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previews[0].save(
        args.output,
        save_all=True,
        append_images=previews[1:],
        duration=200,
        loop=0,
        optimize=True,
    )
    size = args.output.stat().st_size
    if size > 1024 * 1024:
        raise SystemExit(
            f"Preview exceeds 1 MiB: {size}; reduce resolution/frame count"
        )
    print(
        json.dumps({"output": str(args.output), "bytes": size, "frames": len(previews)})
    )


if __name__ == "__main__":
    main()
