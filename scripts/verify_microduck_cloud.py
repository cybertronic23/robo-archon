#!/usr/bin/env python3
"""Live LLM acceptance. Run in the iTerm2 shell that exported your API key."""

import argparse
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--report", type=Path, default=ROOT / "tmp-episodes/microduck-live-llm.json"
    )
    args = p.parse_args()
    if not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        raise SystemExit(
            "No API key inherited. Run this script in the terminal that exported your key; never paste it into chat."
        )
    raw = args.report.with_suffix(".execution.json")
    instruction = "让 Microduck 前进两秒，向左转两秒，再前进两秒，最后停下。可以调用 microduck_patrol，它准确描述了这一有序组合。依据实际执行结果报告，不要声称精确路径或速度。"
    result = subprocess.run(
        [
            str(ROOT / "target/debug/robo-archon"),
            "--robot",
            "microduck",
            "--backend",
            "mujoco",
            "--skill-instruction",
            instruction,
            "--skill-report",
            str(raw),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    evidence = {
        "provider": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        "model": os.environ.get("LLM_MODEL", "deepseek-chat"),
        "mode": "LIVE cloud LLM + real local MuJoCo physics",
        "exit_code": result.returncode,
    }
    if raw.exists():
        evidence["execution"] = json.loads(raw.read_text())
    else:
        evidence["error"] = (
            "No execution report; check provider credentials/availability locally."
        )
    execution = evidence.get("execution", {})
    outcome = execution.get("result", {})
    evidence["accepted"] = (
        result.returncode == 0
        and outcome.get("status") == "succeeded"
        and len(outcome.get("steps", [])) >= 3
        and outcome.get("observation", {}).get("stop_confirmed") is True
        and bool(execution.get("feedback", {}).get("text"))
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"accepted": evidence["accepted"], "report": str(args.report)}))
    if not evidence["accepted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
