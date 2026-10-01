#!/usr/bin/env python3
"""Install pinned upstream robot assets atomically; never execute upstream scripts."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT/'python/robo_archon_sim_workers'))
from asset_integrity import tree_hash

def install(robot, registry, destination):
    entry = registry['assets'][robot]
    revision = entry['revision']
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('upstream revision must be a full commit SHA')
    for field in ('directory', 'subdirectory', 'entrypoint'):
        path = Path(entry[field])
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError(f'{field} must be a non-empty relative path without parent traversal')
    target = destination / entry['directory']
    stamp = target / '.robo-archon-install.json'
    if target.exists():
        if stamp.exists():
            metadata = json.loads(stamp.read_text())
            if metadata['revision'] == revision and metadata['sha256'] == tree_hash(target):
                print(f'{robot}: installed and verified ({revision})')
                return
        raise RuntimeError(f'{target} exists with different or unverified content; preserve/remove it explicitly before reinstalling')
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='robot-install-', dir=destination) as temporary:
        temporary = Path(temporary)
        repo = temporary / 'upstream'
        def git(*args):
            subprocess.run(['git', *args], check=True)
        git('init', '-q', str(repo))
        git('-C', str(repo), 'remote', 'add', 'origin', entry['repository'])
        git('-C', str(repo), 'config', 'core.sparseCheckout', 'true')
        (repo / '.git/info/sparse-checkout').write_text(entry['subdirectory'] + '/\n/LICENSE*\n')
        git('-C', str(repo), 'fetch', '--depth', '1', '--filter=blob:none', 'origin', revision)
        git('-C', str(repo), 'checkout', '--detach', '-q', 'FETCH_HEAD')
        source = repo / entry['subdirectory']
        if not (source / entry['entrypoint']).is_file():
            raise RuntimeError('upstream model entrypoint missing')
        if any(path.is_symlink() for path in source.rglob('*')):
            raise RuntimeError('upstream asset symlinks are not supported')
        package = temporary / 'package'
        shutil.copytree(source, package)
        # Preserve repository license when the selected model subtree has no license.
        for name in ['LICENSE', 'LICENSE.md', 'LICENSE.txt']:
            if (repo / name).is_file() and not (package / name).exists():
                shutil.copy2(repo / name, package / name)
        unresolved = []
        for path in package.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    if stream.read(100).startswith(b'version https://git-lfs.github.com/spec/v1'):
                        unresolved.append(path)
        if unresolved:
            raise RuntimeError('unresolved Git LFS assets; install git-lfs and retry')
        metadata = dict(robot=robot, repository=entry['repository'], revision=revision,
                        entrypoint=entry['entrypoint'], sha256=tree_hash(package))
        (package / '.robo-archon-install.json').write_text(json.dumps(metadata, indent=2)+'\n')
        package.rename(target)
    print(f'{robot}: installed {target} ({revision})')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('robot', choices=['franka_panda', 'so101'])
    parser.add_argument('--registry', type=Path, default=ROOT/'robots/assets.lock.json')
    parser.add_argument('--destination', type=Path, default=ROOT/'python/models/external')
    args = parser.parse_args()
    install(args.robot, json.loads(args.registry.read_text()), args.destination)

if __name__ == '__main__':
    main()
