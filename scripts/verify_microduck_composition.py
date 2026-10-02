#!/usr/bin/env python3
"""Real MuJoCo composition/handoff plus LOCAL MOCK model/chat acceptance."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import signal

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "target/debug/robo-archon"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--report", type=Path, default=ROOT / "tmp-episodes/m2g4-acceptance.json"
    )
    p.add_argument("--mock-only", action="store_true")
    args = p.parse_args()
    directory = ROOT / "tmp-episodes/m2g4-tests"
    directory.mkdir(parents=True, exist_ok=True)
    base = [str(CLI), "--robot", "microduck", "--backend", "mujoco"]
    evidence = {"mode": "real MuJoCo physics, LOCAL MOCK LLM", "cases": {}}

    def run(name, flags, success=True):
        report = directory / (name + ".json")
        output = subprocess.run(
            base + flags + ["--skill-report", str(report)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert (output.returncode == 0) == success, (name, output.stderr[-2500:])
        value = json.loads(report.read_text()) if report.exists() else None
        evidence["cases"][name] = {"exit_code": output.returncode, "result": value}
        return value

    if not args.mock_only:
        result = run(
            "sequence", ["--run-skill-sequence", "skills/calls/microduck-sequence.json"]
        )
        assert len(result["steps"]) == 4 and result["status"] == "succeeded"
        times = [s["observation"]["simulation_time"] for s in result["steps"]]
        assert times == sorted(times) and len(set(times)) == 4
        for index, step in enumerate(result["steps"]):
            obs = step["observation"]
            assert obs["stop_confirmed"] and obs["fault"] is None
            if index < 3:
                delta = (
                    sum(
                        (obs["xyz"][j] - obs["motion"]["start_xyz"][j]) ** 2
                        for j in (0, 1)
                    )
                    ** 0.5
                )
                assert delta > 0.05, (index, delta)
            if index == 1:
                m = obs["motion"]
                assert m["sum_yaw_rate"] / m["samples"] > 0.15
        run(
            "registered_composite",
            ["--run-skill", "skills/calls/microduck-patrol.json"],
        )
        # Repeat in one episode and cross policy IDs without resetting physics.
        skills = directory / "skills"
        shutil.copytree(ROOT / "skills", skills, dirs_exist_ok=True)
        shutil.copytree(
            ROOT / "tmp-episodes/m2g3-fixture/skills/official-copy",
            skills / "example",
            dirs_exist_ok=True,
        )
        # Exporters can rename tensor inputs/outputs without changing the contract.
        named = directory / "named-policy"
        named.mkdir(exist_ok=True)
        source = ROOT / "tmp-episodes/m2g3-fixture/policy"
        weight = (source / "policy.onnx").read_bytes()
        for old, new in [
            (b"\x0a\x03obs", b"\x0a\x03xyz"),
            (b"\x12\x03obs", b"\x12\x03xyz"),
            (b"\x0a\x07actions", b"\x0a\x07targets"),
            (b"\x12\x07actions", b"\x12\x07targets"),
        ]:
            weight = weight.replace(old, new)
        (named / "policy.onnx").write_bytes(weight)
        manifest = json.loads((source / "policy.json").read_text())
        manifest.update(
            id="example.named",
            sha256=hashlib.sha256(weight).hexdigest(),
            source=manifest["source"] + "; tensor names renamed for handoff regression",
        )
        (named / "policy.json").write_text(json.dumps(manifest))
        installed = subprocess.run(
            [
                str(ROOT / ".venv-microduck/bin/python"),
                str(ROOT / "scripts/install_policy.py"),
                str(named),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert installed.returncode == 0, installed.stderr
        skill = json.loads((skills / "example/skill.json").read_text())
        skill.update(id="example.named.walk", tool_name="example_named_walk")
        skill["bindings"][0]["policy"] = "example.named"
        skill_dir = skills / "named"
        skill_dir.mkdir(exist_ok=True)
        (skill_dir / "skill.json").write_text(json.dumps(skill))
        plan = {
            "schema_version": 1,
            "timeout_ms": 16000,
            "steps": [
                {
                    "skill_id": id,
                    "parameters": {"vx": 0.4, "duration_ms": 2000},
                    "timeout_ms": 4000,
                }
                for id in [
                    "microduck.walk",
                    "example.walk",
                    "example.named.walk",
                    "microduck.walk",
                ]
            ],
        }
        path = directory / "handoff-plan.json"
        path.write_text(json.dumps(plan))
        result = run(
            "policy_handoff",
            ["--skills-dir", str(skills), "--run-skill-sequence", str(path)],
        )
        assert [s["observation"]["policy_id"] for s in result["steps"]] == [
            "velstand",
            "example.velstand",
            "example.named",
            "velstand",
        ]
        assert all(s["observation"]["stop_confirmed"] for s in result["steps"])
        assert all(
            a["observation"]["simulation_time"] < b["observation"]["simulation_time"]
            for a, b in zip(result["steps"], result["steps"][1:])
        )
        bad = json.loads(json.dumps(plan))
        bad["steps"][-1]["parameters"]["vx"] = 999
        path = directory / "invalid-tail.json"
        path.write_text(json.dumps(bad))
        frames = directory / "invalid-tail-frames"
        assert not frames.exists(), "use a fresh acceptance directory"
        run(
            "invalid_tail_before_motion",
            [
                "--skills-dir",
                str(skills),
                "--run-skill-sequence",
                str(path),
                "--skill-record-dir",
                str(frames),
            ],
            False,
        )
        assert not frames.exists()
        result = run(
            "cancelled_sequence",
            [
                "--run-skill-sequence",
                "skills/calls/microduck-sequence.json",
                "--auto-stop-ms",
                "1000",
            ],
            False,
        )
        assert (
            result["status"] == "cancelled"
            and len(result["steps"]) == 1
            and result["observation"]["stop_confirmed"]
        )

        # Rotated starts exercise body-frame control independently of world heading.
        headings = []
        for yaw in [-0.8, 0.8]:
            heading_report = directory / ("heading-" + str(yaw) + ".json")
            output = subprocess.run(
                base
                + [
                    "--run-skill-sequence",
                    "skills/calls/microduck-sequence.json",
                    "--skill-report",
                    str(heading_report),
                ],
                cwd=ROOT,
                env={**os.environ, "ROBO_ARCHON_MICRODUCK_INITIAL_YAW": str(yaw)},
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert output.returncode == 0, output.stderr[-1500:]
            heading = json.loads(heading_report.read_text())
            assert heading["status"] == "succeeded"
            assert all(s["observation"]["stop_confirmed"] for s in heading["steps"])
            headings.append({"initial_yaw": yaw, "result": heading})
        evidence["cases"]["rotated_starts"] = headings
    if not args.mock_only:
        weak_call = directory / "weak-call.json"
        weak_call.write_text(
            json.dumps(
                {
                    "skill_id": "microduck.walk",
                    "parameters": {"vx": 0.05, "duration_ms": 2000},
                    "timeout_ms": 4000,
                }
            )
        )
        weak = run("weak_command_is_failure", ["--run-skill", str(weak_call)], False)
        assert (
            weak["status"] == "failed"
            and "requested motion not observed" in weak["reason"]
            and weak["observation"]["stop_confirmed"]
        )
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(data)
            if "tools" in data:
                names = [t["function"]["name"] for t in data["tools"]]
                assert "archon_sequence" in names and "microduck_patrol" in names
                request = data["messages"][-1]["content"]
                if "second turn" in request:
                    assert (
                        "previous_measured_results" in request
                        and "stop_confirmed" in request
                    )
                    name, parameters = "microduck_stand", {"duration_ms": 1000}
                else:
                    name, parameters = (
                        "archon_sequence",
                        {
                            "steps": [
                                {
                                    "tool_name": "microduck_walk",
                                    "parameters": {"vx": 0.4, "duration_ms": 1000},
                                },
                                {
                                    "tool_name": "microduck_walk",
                                    "parameters": {
                                        "vx": 0.3,
                                        "yaw_rate": 1.0,
                                        "duration_ms": 1000,
                                    },
                                },
                                {
                                    "tool_name": "microduck_stop",
                                    "parameters": {"duration_ms": 1000},
                                },
                            ]
                        },
                    )
                message = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "local_mock_call",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(parameters),
                            },
                        }
                    ],
                }
            else:
                assert data["messages"][-1]["role"] == "tool"
                message = {
                    "role": "assistant",
                    "content": "LOCAL MOCK: measured result received.",
                }
            payload = json.dumps({"choices": [{"message": message}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    flags = [
        "--llm-api-key",
        "local-mock-fixture",
        "--llm-base-url",
        f"http://127.0.0.1:{server.server_port}/v1",
        "--llm-model",
        "local-mock",
    ]
    try:
        result = run(
            "llm_ordered_sequence",
            flags + ["--skill-instruction", "first turn: forward, turn, stop"],
        )
        assert (
            result["result"]["status"] == "succeeded"
            and len(result["result"]["steps"]) == 3
        )
        report = directory / "chat.json"
        process = subprocess.Popen(
            base + flags + ["--skill-chat", "--skill-report", str(report)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        outputs = queue.Queue()

        def read():
            for line in process.stdout:
                outputs.put(line)

        threading.Thread(target=read, daemon=True).start()
        try:
            turns = []
            for instruction in ["first turn", "second turn"]:
                process.stdin.write(instruction + "\n")
                process.stdin.flush()
                turns.append(json.loads(outputs.get(timeout=45)))
            process.stdin.write("/reset\n")
            process.stdin.flush()
            reset = json.loads(outputs.get(timeout=10))
            assert (
                reset["explicit_simulation_reset"]
                and reset["observation"]["episode_id"] == 1
                and reset["observation"]["simulation_time"] == 0
            )
            process.stdin.write("second turn after reset\n")
            process.stdin.flush()
            after_reset = json.loads(outputs.get(timeout=30))
            assert (
                after_reset["result"]["observation"]["episode_id"] == 1
                and after_reset["result"]["status"] == "succeeded"
            )
            process.stdin.write("/quit\n")
            process.stdin.flush()
            assert process.wait(timeout=10) == 0
            first = turns[0]["result"]["observation"]["simulation_time"]
            second = turns[1]["result"]["observation"]["simulation_time"]
            assert second > first
            assert all(t["result"]["observation"]["stop_confirmed"] for t in turns)
            evidence["cases"]["persistent_chat"] = {
                "turns": turns,
                "same_episode": True,
                "explicit_reset": reset,
                "after_reset": after_reset,
            }
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        # Stop/quit queued before worker startup discards old movement proposals.
        before = len(captured)
        queued = subprocess.run(
            base + flags + ["--skill-chat"],
            cwd=ROOT,
            input="first turn\n/stop\n/quit\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert queued.returncode == 0, queued.stderr[-1000:]
        assert len(captured) == before
        evidence["cases"]["queued_stop_discards_motion"] = {
            "model_requests": 0,
            "exit_code": queued.returncode,
        }
        before = len(captured)
        interrupted = subprocess.Popen(
            base + flags + ["--skill-chat"],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            interrupted.stdin.write("first turn\n")
            interrupted.stdin.flush()
            deadline = time.monotonic() + 15
            while len(captured) == before and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(captured) > before
            time.sleep(0.4)
            os.kill(interrupted.pid, signal.SIGINT)
            output, _ = interrupted.communicate(timeout=10)
            assert interrupted.returncode == 0
            report = json.loads(output.strip().splitlines()[-1])
            assert (
                report["result"]["status"] == "cancelled"
                and report["result"]["observation"]["stop_confirmed"]
            )
            assert len(report["result"]["steps"]) <= 1
            evidence["cases"]["chat_sigint"] = report
        finally:
            if interrupted.poll() is None:
                interrupted.kill()
                interrupted.wait()
    finally:
        server.shutdown()
        server.server_close()
    evidence["accepted"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {
                "accepted": True,
                "cases": list(evidence["cases"]),
                "report": str(args.report),
            }
        )
    )


if __name__ == "__main__":
    main()
