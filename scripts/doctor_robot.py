#!/usr/bin/env python3
"""Check installed pinned model, control mapping and initial state without rendering."""
import argparse
import json
from pathlib import Path
import sys
import mujoco
import numpy

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'python/robo_archon_sim_workers'))
from mujoco_worker import MujocoSession

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('robot',choices=['franka_panda','so101'])
    args=parser.parse_args()
    lock=json.loads((ROOT/'robots/assets.lock.json').read_text())['assets'][args.robot]
    profile=json.loads((ROOT/f'robots/profiles/{args.robot}.json').read_text())
    session=MujocoSession()
    session.load(dict(model_path=str(ROOT/'python/models/external'/lock['directory']/lock['entrypoint']),arm_profile=profile,render=False))
    print(json.dumps(dict(robot=args.robot,revision=profile['source_revision'],joint_names=session.joint_names,
                          positions=session._joint_positions(),gripper_open=session._measured_gripper_open(),
                          environment=dict(mujoco=mujoco.__version__,numpy=numpy.__version__),
                          checks=['source revision','content checksum','compiled joint limits','actuator mapping','home state'],
                          rendering='not checked'),indent=2))
if __name__=='__main__':main()
