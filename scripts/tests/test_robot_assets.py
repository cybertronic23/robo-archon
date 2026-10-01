"""Offline installer regression tests using a tiny local Git upstream."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installer', ROOT/'scripts/install_robot_assets.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root/'upstream'
        self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
        self.git = git
        git('init','-q')
        git('config','user.email','fixture@example.com')
        git('config','user.name','Fixture')
        (self.repo/'robot').mkdir()
        (self.repo/'robot/scene.xml').write_text('<mujoco/>')
        (self.repo/'LICENSE').write_text('Fixture license')
        git('add','.')
        git('commit','-qm','fixture')
        self.revision=git('rev-parse','HEAD')
        self.registry={'assets':{'fixture':dict(repository=str(self.repo),revision=self.revision,subdirectory='robot',directory='robot',entrypoint='scene.xml')}}
        self.dest=self.root/'installed'
    def tearDown(self): self.temp.cleanup()
    def test_pinned_install_license_and_idempotence(self):
        # Change upstream HEAD: installation must retain pinned content.
        (self.repo/'robot/scene.xml').write_text('changed')
        self.git('commit','-am','changed','-q')
        installer.install('fixture',self.registry,self.dest)
        self.assertEqual((self.dest/'robot/scene.xml').read_text(),'<mujoco/>')
        self.assertTrue((self.dest/'robot/LICENSE').exists())
        installer.install('fixture',self.registry,self.dest)
        (self.dest/'robot/scene.xml').write_text('tampered')
        with self.assertRaises(RuntimeError): installer.install('fixture',self.registry,self.dest)
    def test_missing_entrypoint_does_not_publish_partial_install(self):
        registry=copy.deepcopy(self.registry)
        registry['assets']['fixture']['entrypoint']='missing.xml'
        with self.assertRaises(RuntimeError): installer.install('fixture',registry,self.dest)
        self.assertFalse((self.dest/'robot').exists())
    def test_requires_full_commit(self):
        self.registry['assets']['fixture']['revision']='main'
        with self.assertRaises(ValueError): installer.install('fixture',self.registry,self.dest)

if __name__=='__main__': unittest.main()
