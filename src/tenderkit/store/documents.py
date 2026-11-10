"""Verified SHA-256 publication shared with pharma, plus the standalone sharded store."""
import hashlib
import os
from pathlib import Path
import re
import json
import tempfile
import shutil

from tenderkit.limits import MAX_BYTES

STORAGE_NAME = re.compile(r'([a-f0-9]{64})\.[a-z0-9]+')

class StoreError(ValueError):
    pass

def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()

def state(store, storage_name, digest, size=None):
    """'OK', 'MISSING' or 'MISMATCH' (other content or size) for one registered document."""
    path = Path(store)/storage_name
    if not path.is_file():
        return 'MISSING'
    if (size is not None and path.stat().st_size != size) or sha256_file(path) != digest:
        return 'MISMATCH'
    return 'OK'

def publish(store, source, digest, storage_name):
    """Copy a verified source into the store; True when written, False when the same content is already there.

    The copy goes to a temporary file in the store, is checked and only then renamed, so a reader never sees a partial
    file. An existing file with other content is an error and is never overwritten.
    """
    found = STORAGE_NAME.fullmatch(storage_name or '')
    if not found or found[1] != digest:
        raise StoreError(f'Storage name does not match its SHA-256: {storage_name}')
    if sha256_file(source) != digest:
        raise StoreError(f'Source does not match its SHA-256: {Path(source).name}')
    store = Path(store)
    target = store/storage_name
    current = state(store, storage_name, digest)
    if current == 'OK':
        return False
    if current == 'MISMATCH':
        raise StoreError(f'Store holds other content under {storage_name}')
    store.mkdir(parents=True, exist_ok=True)
    temporary = store/f'.{storage_name}.{os.getpid()}.tmp'
    try:
        with Path(source).open('rb') as reader, temporary.open('xb') as writer:
            for block in iter(lambda: reader.read(1 << 20), b''):
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        if sha256_file(temporary) != digest:
            raise StoreError(f'Copy does not match its SHA-256: {storage_name}')
        # Content-addressed: a concurrent writer of the same name wrote the same bytes.
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


class DocumentStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def path(self, storage_name):
        if not STORAGE_NAME.fullmatch(storage_name or ''):
            raise StoreError('Invalid storage name')
        path = self.root / 'objects' / storage_name[:2] / storage_name
        if not path.resolve().is_relative_to(self.root):
            raise StoreError('Document escapes store')
        return path

    def put(self, source):
        source = Path(source)
        if source.stat().st_size > MAX_BYTES:
            raise StoreError('Document exceeds file size limit')
        digest = sha256_file(source)
        ext = re.sub(r'[^a-z0-9]', '', source.suffix.lower())[:10] or 'bin'
        name = f'{digest}.{ext}'
        target = self.path(name)
        publish(target.parent, source, digest, name)
        return dict(sha256=digest, storage_name=name, byte_size=target.stat().st_size)

    def verified(self, record):
        path = self.path(record['storage_name'])
        if state(path.parent, path.name, record['sha256'], record['byte_size']) != 'OK':
            raise StoreError('Stored document is missing or corrupt')
        return path

    def manifest(self, source, number, value):
        key = hashlib.sha256(json.dumps([source, number], ensure_ascii=False).encode('utf-8')).hexdigest()
        directory = self.root / 'procedures' / key / 'raw'
        if not directory.resolve().is_relative_to(self.root):
            raise StoreError('Manifest escapes store')
        directory.mkdir(parents=True, exist_ok=True)
        for item in value['documents']:
            if not item.get('localPath'):
                continue
            source = Path(item['localPath'])
            digest = item['sha256']
            if sha256_file(source) != digest:
                raise StoreError('Manifest source does not match its SHA-256')
            # Existing pharma readers resolve raw/Path(localPath).name. Keep
            # that layout as hard links to the canonical sharded objects.
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', item['name']).strip(' .')[:80] or 'document'
            if Path(safe).suffix.lower() != source.suffix.lower():
                safe += source.suffix
            target = directory / (digest[:16] + '_' + safe)
            if target.exists():
                if sha256_file(target) != digest:
                    raise StoreError('Manifest file holds other content')
            else:
                try:
                    os.link(source, target)
                except OSError:
                    with tempfile.NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as stream:
                        temporary_copy = Path(stream.name)
                    try:
                        shutil.copyfile(source, temporary_copy)
                        if sha256_file(temporary_copy) != digest:
                            raise StoreError('Manifest copy does not match SHA-256')
                        os.replace(temporary_copy, target)
                    finally:
                        temporary_copy.unlink(missing_ok=True)
            item['localPath'] = str(target)
        path = directory / 'manifest.json'
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
