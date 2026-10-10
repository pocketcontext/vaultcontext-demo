#!/usr/bin/env python3
"""Verify the packaged schema compatibility declaration matches build inputs."""
import hashlib
import json
from pathlib import Path
import re
import sys


def verify(root, policy):
    value = json.loads(Path(policy).read_text())
    digest = hashlib.sha256()
    for relative in [Path('POCKETCONTEXT_VERSION'), *[path.relative_to(root) for path in sorted((root / 'pb_migrations').glob('*.js'))]]:
        # Caller runs in root so only explicit build inputs enter the fingerprint.
        digest.update(str(relative).encode() + b'\0' + (root / relative).read_bytes() + b'\0')
    if value.get('format') != 1 or value.get('schema') != digest.hexdigest():
        raise ValueError('compatibility policy does not match server and migrations')
    for key, size in [('acceptSchemas', 64), ('acceptLegacyRevisions', 40)]:
        if not isinstance(value.get(key), list) or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{'+str(size)+'}', v) for v in value[key]):
            raise ValueError('invalid compatibility declaration')


if __name__ == '__main__':
    verify(Path.cwd(), sys.argv[1])
