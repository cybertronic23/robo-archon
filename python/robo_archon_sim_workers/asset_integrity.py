"""Content fingerprint of installed model files (not a publisher signature)."""
import hashlib

def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob('*') if p.is_file() and p.name != '.robo-archon-install.json'):
        if path.is_symlink():
            raise ValueError('asset symlinks are not supported')
        digest.update(path.relative_to(root).as_posix().encode() + b'\0')
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()
