"""CPU MuJoCo physical regression tests; requires installed pinned models."""
import copy
import json
from pathlib import Path
import sys
import unittest
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'python/robo_archon_sim_workers'))
from mujoco_worker import MujocoSession

class ArmBindingTests(unittest.TestCase):
    def test_physical_home_gripper_reset_stop_and_revision(self):
        for rid,directory in [('franka_panda','franka_emika_panda'),('so101','so101')]:
            with self.subTest(robot=rid):
                profile=json.loads((ROOT/f'robots/profiles/{rid}.json').read_text())
                hello=dict(model_path=str(ROOT/f'python/models/external/{directory}/scene.xml'),render=False,arm_profile=profile)
                s=MujocoSession(); s.load(hello); s._poll_estop=lambda:False
                self.assertEqual(s.joint_names,profile['joint_names'])
                np.testing.assert_allclose(s._joint_positions(),profile['home'])
                # Actual dynamics must follow a non-zero arm target, then return home.
                target=list(profile['home']); target[0]+=0.15
                for _ in range(150): s.command(dict(names=s.joint_names,positions=target,gripper_open=1))
                self.assertAlmostEqual(s._joint_positions()[0],target[0],delta=.03)
                for openness in [0,1]:
                    for _ in range(150): s.command(dict(names=s.joint_names,positions=profile['home'],gripper_open=openness))
                    self.assertAlmostEqual(s.observation()['proprio']['gripper_open'],openness,delta=.03)
                s.apply_estop()
                np.testing.assert_allclose(s.data.ctrl[s.actuator_ids],s._joint_positions())
                s.reset(); np.testing.assert_allclose(s._joint_positions(),profile['home'])
                self.assertAlmostEqual(s._measured_gripper_open(),1)
                ctrl=s.data.ctrl.copy()
                for invalid in [dict(names=['wrong'],positions=[0]),dict(names=s.joint_names,positions=[float('nan')]*len(s.joint_names)),dict(names=s.joint_names,positions=profile['home'],gripper_open=2)]:
                    with self.assertRaises(RuntimeError): s.command(invalid)
                    np.testing.assert_array_equal(s.data.ctrl,ctrl)
                wrong=copy.deepcopy(hello);wrong['arm_profile']['source_revision']='0'*40
                with self.assertRaises(RuntimeError): MujocoSession().load(wrong)

if __name__=='__main__': unittest.main()
