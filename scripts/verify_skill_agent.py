#!/usr/bin/env python3
"""End-to-end LOCAL MOCK HTTP acceptance. No real LLM inference/API key is used."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / "tmp-episodes/m2g3-fixture"
    )
    args = parser.parse_args()
    captured = []
    mode = "valid"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(data)
            if "tools" in data:
                assert [t["function"]["name"] for t in data["tools"]] == [
                    "example_walk"
                ]
                if mode == "refuse":
                    message = {
                        "role": "assistant",
                        "content": "Requested skill is unavailable.",
                    }
                else:
                    arguments = {"vx": 0.4, "duration_ms": 2000}
                    if mode == "invalid":
                        arguments["runner"] = "shell"
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fixture_call",
                                "type": "function",
                                "function": {
                                    "name": "example_walk"
                                    if mode != "unknown"
                                    else "run_shell",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
            else:
                assert data["messages"][-1]["role"] == "tool"
                result = json.loads(data["messages"][-1]["content"])
                assert (
                    result["status"] == "succeeded"
                    and result["observation"]["policy_id"] == "example.velstand"
                    and result["observation"]["stop_confirmed"]
                )
                if mode == "feedback_fail":
                    self.send_response(503)
                    self.end_headers()
                    return
                message = {
                    "role": "assistant",
                    "content": "Mock acknowledgement of measured result; not real LLM inference.",
                }
            encoded = json.dumps({"choices": [{"message": message}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    evidence = []
    try:
        for mode in ["valid", "feedback_fail", "invalid", "unknown", "refuse"]:
            frames = args.fixture / (mode + "-must-not-start-frames")
            report = args.fixture / (mode + "-agent-result.json")
            command = [
                str(ROOT / "target/debug/robo-archon"),
                "--robot",
                "microduck",
                "--backend",
                "mujoco",
                "--skills-dir",
                str(args.fixture / "skills"),
                "--skill-instruction",
                "Use the registered fixture skill to move forward briefly.",
                "--llm-api-key",
                "fixture-token",
                "--llm-base-url",
                f"http://127.0.0.1:{server.server_port}/v1",
                "--llm-model",
                "local-fixture",
                "--skill-report",
                str(report),
            ]
            if mode not in ("valid", "feedback_fail"):
                command.extend(["--skill-record-dir", str(frames)])
            begin = len(captured)
            run = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=60
            )
            if mode in ("valid", "feedback_fail"):
                assert run.returncode == 0, run.stderr
                result = json.loads(report.read_text())
                assert result["executed"] is True
                if mode == "feedback_fail":
                    assert "error" in result["feedback"]
                motion = result["result"]["observation"]["motion"]
                xyz = result["result"]["observation"]["xyz"]
                assert (
                    sum((a - b) ** 2 for a, b in zip(xyz[:2], motion["start_xyz"][:2]))
                    ** 0.5
                    > 0.05
                )
            elif mode == "refuse":
                assert (
                    run.returncode == 0
                    and json.loads(report.read_text())["executed"] is False
                ), run.stderr
            else:
                assert run.returncode != 0, run.stdout
            if mode not in ("valid", "feedback_fail"):
                assert not frames.exists(), (
                    "worker started before call validation/refusal"
                )
            evidence.append(
                {
                    "mode": mode,
                    "exit_code": run.returncode,
                    "requests": captured[begin:],
                    "result": json.loads(report.read_text())
                    if report.exists()
                    else None,
                    "stderr": run.stderr if mode != "valid" else None,
                }
            )
        destination = args.fixture / "agent-acceptance.json"
        destination.write_text(
            json.dumps(
                {
                    "acceptance": "passed",
                    "transport": "local mock only; no genuine LLM inference",
                    "cases": evidence,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"Local mock SkillAgent acceptance passed: {destination}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
