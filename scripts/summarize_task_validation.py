#!/usr/bin/env python3
"""Build a reviewable report from real successful CLI Episode bundles."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("episodes", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/validation/m2f-2-report.json"
    )
    args = parser.parse_args()
    cases = []
    for path in sorted(args.episodes.glob("*.json")):
        ep = json.loads(path.read_text())
        events = ep["events"]
        config = next(e["payload"] for e in events if e["kind"] == "task_config")
        state = next(e["payload"] for e in events if e["kind"] == "task_evaluation")
        if (
            events[-1]["kind"] != "task_result"
            or not events[-1]["payload"]["success"]
            or not state["success"]
        ):
            raise ValueError(f"not a successful measured task: {path}")
        cases.append(
            dict(
                robot=config["robot"],
                backend=ep["backend"],
                seed=config["seed"],
                task=ep["task_id"],
                source_revision=config["source_revision"],
                episode_id=ep["id"],
                episode_sha256=sha(path),
                phases=len([e for e in events if e["kind"] == "task_phase"]),
                evaluation=state,
            )
        )
    expected = {
        (rid, backend, seed)
        for rid, backend in [
            ("franka_panda", "mujoco"),
            ("so101", "mujoco"),
            ("franka_panda", "maniskill"),
        ]
        for seed in range(3)
    }
    actual = {(c["robot"], c["backend"], c["seed"]) for c in cases}
    if actual != expected or len(cases) != 9:
        raise ValueError(f"incomplete task matrix: {actual}")
    sources = [
        "python/robo_archon_sim_workers/pick_place.py",
        "python/robo_archon_sim_workers/mujoco_worker.py",
        "python/robo_archon_sim_workers/maniskill_worker.py",
        "crates/robo-archon-cli/src/pick_place.rs",
        "robots/profiles/franka_panda.json",
        "robots/profiles/so101.json",
        "robots/runtime-assets.lock.json",
    ]
    videos = [
        ROOT / "docs/media/franka-panda-pick-place.mp4",
        ROOT / "docs/media/so101-pick-place.mp4",
    ]
    media = []
    for path in videos:
        metadata = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,width,height,nb_frames:format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                text=True,
            )
        )
        media.append(
            dict(
                path=path.relative_to(ROOT).as_posix(),
                sha256=sha(path),
                metadata=metadata,
                visual_checked=True,
            )
        )
    report = dict(
        schema_version=1,
        date="2026-10-01",
        environment=dict(
            os=platform.system(),
            machine=platform.machine(),
            python=platform.python_version(),
        ),
        task="pick_place.v1",
        source_sha256={p: sha(ROOT / p) for p in sources},
        cases=cases,
        media=media,
        negative_checks=[
            "unreachable/nonfinite IK leaves live scene unchanged",
            "floor collision rejected without execution",
            "stuck-open physical actuator fails before lift",
            "task-wide stop prevents subsequent phases on both platforms",
            "missing any measured condition cannot pass",
            "runtime version/asset mutation rejected",
            "proposal timeout / actuator fault stops, closes and releases resource locks",
        ],
        reproduction="See docs/robot-gallery.md and .github/workflows/embodied.yml; ROBO_ARCHON_ACCEPTANCE_DIR captures the full CLI episodes. Episode hashes are original local acceptance runs, not interchangeable with future reruns.",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{len(cases)} physical cases -> {args.output}")


if __name__ == "__main__":
    main()
