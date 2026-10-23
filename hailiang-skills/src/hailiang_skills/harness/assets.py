"""Content-addressed Skill assets travel with the durable configuration snapshot."""
from __future__ import annotations
import base64
import hashlib
from io import BytesIO
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from hailiang_skills.core.deployment import state_root
from .contracts import HarnessError


def snapshot_assets(root):
    root = Path(root)
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if not path.is_file() or any(part in {'__pycache__', '.git', '.cache', '.DS_Store', '.pytest_cache'} for part in relative.parts):
                continue
            info = ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    data = buffer.getvalue()
    return {'digest': hashlib.sha256(data).hexdigest(), 'archive': base64.b64encode(data).decode('ascii')}


def materialize_assets(snapshot):
    data = base64.b64decode(snapshot['archive'])
    digest = hashlib.sha256(data).hexdigest()
    if digest != snapshot['digest']:
        raise HarnessError('SKILL_SNAPSHOT_INTEGRITY_FAILED')
    cache = state_root() / 'harness-assets'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / digest
    if target.is_dir():
        return target
    staging = Path(tempfile.mkdtemp(prefix='extract-', dir=cache))
    try:
        with ZipFile(BytesIO(data)) as archive:
            for item in archive.infolist():
                relative = PurePosixPath(item.filename)
                if relative.is_absolute() or '..' in relative.parts:
                    raise HarnessError('SKILL_SNAPSHOT_PATH_INVALID')
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
        try:
            staging.rename(target)
        except OSError:
            if not target.is_dir():
                raise
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)
