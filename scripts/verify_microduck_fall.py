#!/usr/bin/env python3
"""Integration fault injection: physical trunk push in an isolated test process.
No test/injection command is exposed by the production worker protocol.
"""

import json
import subprocess
import sys
import time
import select
from pathlib import Path

root = Path(__file__).resolve().parents[1]
wrapper = """import sys,runpy,mujoco
step=mujoco.mj_step
def pushed_step(model,data,*args,**kwargs):
    if .5 < data.time < .9:
        data.xfrc_applied[1,0] = 8
    else:
        data.xfrc_applied[1,0] = 0
    return step(model,data,*args,**kwargs)
mujoco.mj_step=pushed_step
script=sys.argv.pop(1)
runpy.run_path(script,run_name="__main__")
"""
p = subprocess.Popen(
    [
        sys.executable,
        "-u",
        "-c",
        wrapper,
        str(root / "python/robo_archon_sim_workers/microduck_worker.py"),
        "--package",
        str(root / "python/models/external/microduck"),
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
)
i = 0
reports = []


def ask(op, **kw):
    global i
    i += 1
    p.stdin.write(
        json.dumps(dict(op=op, id=i, protocol="archon.continuous.v1", **kw)) + "\n"
    )
    p.stdin.flush()
    assert select.select([p.stdout], [], [], 30)[0]
    return json.loads(p.stdout.readline())


try:
    ask("hello")
    for _ in range(40):
        time.sleep(0.1)
        r = ask("observe")
        if r["observation"]["fault"]:
            break
    assert r["observation"]["fault"] == "fallen", r
    reports.append(r)
    rejected = ask("command", twist=[0.3, 0, 0], duration_ms=1000, lease_ms=600)
    assert rejected["ok"] is False and rejected["observation"]["command"] == [
        0,
        0,
        0,
    ], rejected
    time.sleep(0.5)
    latched = ask("observe")
    assert latched["observation"]["fault"] == "fallen", latched
    ask("shutdown")
    assert p.wait(timeout=3) == 0
    report = root / "tmp-episodes/microduck/fall-result.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            dict(
                perturbation="8 N horizontal trunk force for simulation 0.5–0.9 s; no teleport",
                fault=r,
                rejected=rejected,
                latched=latched,
            ),
            indent=2,
        )
        + "\n"
    )
    print("physical push/fall latch/reject passed")
finally:
    if p.poll() is None:
        p.kill()
        p.wait()
