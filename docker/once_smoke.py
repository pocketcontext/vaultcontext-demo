#!/usr/bin/env python3
"""Isolated image test for native ONCE bootstrap; never deploys an application."""
import argparse
import json
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class Failure(RuntimeError):
    pass


def command(*args, body=None, required=True):
    result = subprocess.run(['docker', *args], input=body, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60)
    if required and result.returncode:
        raise Failure('isolated container command failed: ' + args[0])
    return result


def http(base, path):
    try:
        with urlopen(base + path, timeout=2) as response:
            return response.status
    except HTTPError as error:
        result = error.code
        error.close()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    name = 'vaultcontext-once-bootstrap-' + secrets.token_hex(5)
    volume = name + '-storage'
    other = name + '-overlap'
    try:
        command('volume', 'create', volume)
        common = ['--volume', volume + ':/storage', '--env', 'VAULTCONTEXT_DEMO_ONCE=true',
                  '--env', 'BASE_URL=https://vault-demo.pocketcontext.com']
        command('run', '--detach', '--name', name, '--publish', '127.0.0.1::80', *common, args.image)
        address = command('port', name, '80/tcp').stdout.decode().strip()
        if not address.startswith('127.0.0.1:') or not address.rsplit(':', 1)[1].isdigit():
            raise Failure('unexpected isolated test listener')
        base = 'http://' + address
        prefix = ['exec', '--interactive', name, 'python3', '/usr/local/bin/vaultcontext-demo-once.py', 'control']
        deadline = time.monotonic() + 30
        while True:
            try:
                control = command(*prefix, 'status', required=False)
                if http(base, '/up') == 503 and control.returncode == 0:
                    break
            except (OSError, URLError):
                pass
            if time.monotonic() >= deadline:
                raise Failure('bootstrap control readiness timed out')
            time.sleep(.1)
        for path in ('/', '/demo/', '/api/demo/status'):
            if http(base, path) != 503:
                raise Failure('unconfigured bootstrap exposed public application')
        status = json.loads(command(*prefix, 'status').stdout)
        if status['ready'] is not False or status['configured'] is not False or status['databases'] != 0:
            raise Failure('bootstrap status differs')
        result = command(*prefix, 'configure', body=b'{"unknown":"synthetic"}\n', required=False)
        if result.returncode == 0:
            raise Failure('invalid bindings were accepted')
        if json.loads(command(*prefix, 'status').stdout)['configured'] is not False:
            raise Failure('invalid configuration persisted')
        # A second ONCE candidate must not steal the volume/control socket before
        # the old application has drained and released its lifetime writer lock.
        command('run', '--detach', '--name', other, *common, args.image)
        result = command('wait', other)
        if result.stdout.strip() == b'0':
            raise Failure('overlapping supervisor acquired the active volume')
        if http(base, '/up') != 503 or json.loads(command(*prefix, 'status').stdout)['configured'] is not False:
            raise Failure('overlapping candidate disturbed the active controller')
        command('stop', '--time', '30', name)
        code = command('inspect', '--format', '{{.State.ExitCode}}', name).stdout.strip()
        if code != b'0':
            raise Failure('bootstrap did not shut down cleanly')
        print('PASS: native ONCE bootstrap, private control, closed public gate, invalid bindings, exclusive volume and graceful shutdown')
    finally:
        for container in (other, name):
            command('rm', '--force', container, required=False)
        command('volume', 'rm', volume, required=False)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print raw container output or metadata.
        print('FAIL: ONCE bootstrap image check (' + type(error).__name__ + ')')
        raise SystemExit(1)
