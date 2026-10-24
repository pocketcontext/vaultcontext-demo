"""Detached, fresh-interpreter memory sessions; no key-bearing files."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import resource
import socket
import stat
import subprocess
import sys
import time
import threading

from . import auth, crypto

START_TIMEOUT = 15
STOP_TIMEOUT = 10
BOOTSTRAP_LIMIT = 65536


def _cli():
    from . import cli
    return cli


def _same(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def socket_info(path):
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise auth.Fail(1, 'Unsafe unlock session socket.')
    return info


def unlink_owned(path, original):
    """Never let an exiting old worker remove a replacement socket."""
    try:
        current = socket_info(path)
    except FileNotFoundError:
        return
    if not _same(current, original):
        raise auth.Fail(1, 'Unlock session socket changed; retry after inspection.')
    path.unlink()


@contextmanager
def lifecycle(cfg):
    path = Path(os.path.abspath(_cli().socket_path(cfg)))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Anchor lock operations to a directory opened without symlink traversal.
    with crypto._parent(path) as (directory, _):
        info = os.fstat(directory)
        if info.st_uid != os.getuid():
            raise auth.Fail(1, 'Unsafe session directory.')
        os.fchmod(directory, 0o700)
        name = path.with_suffix('.session-lock').name
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise auth.Fail(1, 'Unsafe session lifecycle lock.')
            deadline = time.monotonic() + START_TIMEOUT + STOP_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise auth.Fail(1, 'Another session operation is busy; retry later.') from None
                    time.sleep(.05)
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not _same(info, current):
                raise auth.Fail(1, 'Session lifecycle lock changed; retry after inspection.')
            yield path
        finally:
            os.close(fd)
            # The lock inode is persistent. Unlinking it would split contenders.


def _stop_locked(cfg, path):
    try:
        original = socket_info(path)
    except FileNotFoundError:
        return
    try:
        result = _cli().session_call(cfg, {'command': 'lock'}, timeout=STOP_TIMEOUT)
    except ConnectionRefusedError:
        unlink_owned(path, original)
        return
    except FileNotFoundError:
        # A session may have completed its own cleanup while we connected.
        if not os.path.lexists(path):
            return
        raise auth.Fail(1, 'Unlock session socket changed; retry after inspection.') from None
    except (OSError, auth.Fail, ValueError):
        raise auth.Fail(1, 'Existing session cannot be safely closed; retry or inspect it.') from None
    if result != {'locked': True}:
        raise auth.Fail(1, 'Unexpected session response; socket left unchanged.')
    deadline = time.monotonic() + STOP_TIMEOUT
    while os.path.lexists(path):
        try:
            current = socket_info(path)
        except FileNotFoundError:
            break
        if not _same(current, original):
            raise auth.Fail(1, 'Unlock session socket changed; retry after inspection.')
        if time.monotonic() >= deadline:
            raise auth.Fail(1, 'Previous session is still closing; retry later.')
        time.sleep(.01)


def stop(cfg, logout=False):
    with lifecycle(cfg) as path:
        _stop_locked(cfg, path)
        if logout:
            auth.session_file(cfg).unlink(missing_ok=True)
            auth.legacy_session_file(cfg).unlink(missing_ok=True)
    return {'signed_out': True} if logout else {'locked': True}


def _receive(conn):
    data = bytearray()
    while b'\n' not in data:
        part = conn.recv(4096)
        if not part:
            raise ValueError('Incomplete session startup')
        data.extend(part)
        if len(data) > BOOTSTRAP_LIMIT:
            raise ValueError('Session startup too large')
    line, remainder = data.split(b'\n', 1)
    if remainder:
        raise ValueError('Unexpected session startup data')
    return json.loads(line)


def _send(conn, value):
    data = _cli().encode(value).encode() + b'\n'
    if len(data) > BOOTSTRAP_LIMIT:
        raise ValueError('Session startup too large')
    conn.sendall(data)


def start(cfg, identity, account, timeout):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with lifecycle(cfg) as path:
        _stop_locked(cfg, path)
        with socket.socket(socket.AF_UNIX) as server:
            previous = os.umask(0o177)
            try:
                server.bind(str(path))
            finally:
                os.umask(previous)
            original = socket_info(path)
            process = None
            success = False
            try:
                server.listen(4)
                parent, child = socket.socketpair()
                with parent, child:
                    parent.settimeout(START_TIMEOUT)
                    environment = os.environ.copy()
                    environment.pop('VAULTCONTEXT_USER_PASSWORD', None)
                    process = subprocess.Popen(
                        [sys.executable, '-m', 'vaultcontext_client.session',
                         str(child.fileno()), str(server.fileno())],
                        pass_fds=(child.fileno(), server.fileno()),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        close_fds=True, env=environment,
                    )
                    child.close()
                    _send(parent, {'cfg': cfg, 'identity': identity, 'account': account,
                                   'timeout': timeout, 'path': str(path)})
                    if _receive(parent) != {'ready': True}:
                        raise ValueError('Session startup failed')
                    _send(parent, {'start': True})
                    if _receive(parent) != {'started': True}:
                        raise ValueError('Session startup failed')
                    # Reap in long-lived embedding processes without holding up CLI exit.
                    threading.Thread(target=process.wait, daemon=True).start()
                    success = True
                return {'unlocked': True, 'expires_in': timeout}
            except (OSError, ValueError, auth.Fail, subprocess.SubprocessError):
                raise auth.Fail(1, 'Unlock session could not start; retry after inspection.') from None
            finally:
                if not success:
                    if process is not None:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                        process.wait()
                    unlink_owned(path, original)


def serve(server, cfg, identity, account, deadline, peer_uid=None):
    cli = _cli()
    peer_uid = peer_uid or cli.peer_uid_reader()
    server.settimeout(1)
    while time.monotonic() < deadline:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        with conn:
            if time.monotonic() >= deadline:
                break
            conn.settimeout(5)
            try:
                peer = peer_uid(conn)
            except OSError:
                continue
            if peer != os.getuid():
                continue
            stop_requested = False
            try:
                payload = cli.recv_frame(conn)
                stop_requested = payload.get('command') == 'lock'
                result = ({'locked': True} if stop_requested
                          else cli.execute(cfg, identity, account, payload))
                response = {'result': result}
            except Exception as error:
                response = {
                    'error': str(error) if isinstance(error, auth.Fail)
                             else 'Vault operation failed; sensitive error details suppressed.',
                    'code': getattr(error, 'code', 1),
                }
            try:
                conn.sendall((cli.encode(response) + '\n').encode())
            except OSError:
                # Disconnected/stalled clients must not end the session.
                pass
            if stop_requested:
                break


def worker(control_fd, server_fd):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.set_inheritable(control_fd, False)
    os.set_inheritable(server_fd, False)
    path = original = None
    with socket.socket(fileno=control_fd) as control, socket.socket(fileno=server_fd) as server:
        try:
            control.settimeout(START_TIMEOUT)
            data = _receive(control)
            if not isinstance(data, dict) or set(data) != {'cfg', 'identity', 'account', 'timeout', 'path'}:
                raise ValueError('Invalid session startup')
            timeout = data['timeout']
            if type(timeout) is not int or not 30 <= timeout <= 3600:
                raise ValueError('Invalid session lifetime')
            path = Path(data['path'])
            if str(path) != server.getsockname():
                raise ValueError('Invalid session path')
            original = socket_info(path)
            identity, cfg, account = data['identity'], data['cfg'], data['account']
            crypto.public_identity(identity)  # Validate key shape before reporting readiness.
            peer_uid = _cli().peer_uid_reader()
            del data
            _send(control, {'ready': True})
            if _receive(control) != {'start': True}:
                raise ValueError('Session startup not acknowledged')
            deadline = time.monotonic() + timeout
            _send(control, {'started': True})
            control.close()
            serve(server, cfg, identity, account, deadline, peer_uid)
            return 0
        except Exception:
            # Never serialize bootstrap errors: they may contain key material.
            return 1
        finally:
            if path is not None and original is not None:
                try:
                    unlink_owned(path, original)
                except (OSError, auth.Fail):
                    pass


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('Private worker entry point')
        raise SystemExit(worker(int(sys.argv[1]), int(sys.argv[2])))
    except (OSError, ValueError):
        raise SystemExit(1) from None
