#!/usr/bin/env python3
"""Bounded integration checks for jail.py. Run on the target node.

  python3 redteam.py --venv /path/to/shared/venv --module numpy

Only private temporary canaries are modified. Signals use signal 0. Resource
probes are finite even if isolation fails (192 MiB, 24 children, 8 seconds).
A nonzero exit alone never proves a boundary held. Exit 1 means a failed
check; exit 2 means setup failed. Without --venv, that check is skipped.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid

PREFIX = 'REDTEAM:'
PRELUDE = '''
import errno, json, os, pathlib, socket, sys, time
print('REDTEAM:started', flush=True)
def denied(fn):
    try:
        fn()
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EPERM):
            raise
    else:
        raise AssertionError('operation unexpectedly allowed')
'''


def probes(root: Path, ws: Path, ro: Path, pid: int, module: str):
    outside = str(root / 'canary')
    readonly = str(ro / 'canary')
    yield 'workspace', "p = pathlib.Path('own'); p.write_text('ok'); assert p.read_text() == 'ok'", 'ok'
    yield 'compute', 'assert sum(i*i for i in range(1000)) == 332833500', 'ok'
    yield 'venv-import', f'import importlib; importlib.import_module({module!r}); assert sys.prefix == os.environ["VIRTUAL_ENV"]', 'ok'
    yield 'outside-read', f'denied(lambda: pathlib.Path({outside!r}).read_bytes())', 'ok'
    yield 'outside-write', f'denied(lambda: pathlib.Path({outside!r}).write_text("changed"))', 'ok'
    yield 'symlink-read', "denied(lambda: pathlib.Path('link').read_bytes())", 'ok'
    yield 'readonly-read', f'assert pathlib.Path({readonly!r}).read_text() == "canary"', 'ok'
    yield 'readonly-write', f'denied(lambda: pathlib.Path({readonly!r}).write_text("changed"))', 'ok'
    for name, family, kind in [('tcp', 'AF_INET', 'SOCK_STREAM'), ('udp', 'AF_INET', 'SOCK_DGRAM'), ('ipv6', 'AF_INET6', 'SOCK_STREAM'), ('dbus-socket', 'AF_UNIX', 'SOCK_STREAM')]:
        # Socket creation itself is forbidden; no traffic or D-Bus request sent.
        yield name, f'denied(lambda: socket.socket(socket.{family}, socket.{kind}))', 'ok'
    yield 'real-devshm-closed', 'denied(lambda: os.listdir("/dev/shm"))', 'ok'
    yield 'process-list', 'denied(lambda: os.listdir("/proc"))', 'ok'
    yield 'outside-process', f'denied(lambda: pathlib.Path("/proc/{pid}/environ").read_bytes())', 'ok'
    yield 'negative-pid-signal', 'denied(lambda: os.kill(-1, 0))', 'ok'
    yield 'outside-pid-signal', f'denied(lambda: os.kill({pid}, 0))', 'ok'
    yield 'environment', 'assert "REDTEAM_SECRET" not in os.environ; assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ; assert "SSH_AUTH_SOCK" not in os.environ', 'ok'
    yield 'file-limit', '''
import signal
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
try:
    with open('large', 'wb', buffering=0) as f:
        for _ in range(20):
            f.write(b'x' * 65536)
except OSError as e:
    assert e.errno == errno.EFBIG, e
else:
    raise AssertionError('file limit not enforced')
''', 'ok'
    yield 'memory-limit', "chunks = [bytearray(1024*1024) for _ in range(192)]", 'oom'
    yield 'task-limit', '''
children = []
blocked = False
try:
    for _ in range(24):
        try:
            child = os.fork()
        except OSError as e:
            assert e.errno == errno.EAGAIN, e
            blocked = True
            break
        if child == 0:
            time.sleep(2)
            os._exit(0)
        children.append(child)
finally:
    for child in children:
        os.waitpid(child, 0)
assert blocked, '24 children created despite TasksMax=16'
''', 'ok'
    yield 'time-limit', 'time.sleep(8)', 'timeout'


def passed(kind: str, rc: int, stdout: str, elapsed: float, unit_result: str | None = None) -> bool:
    lines = stdout.splitlines()
    if PREFIX + 'started' not in lines:
        return False
    if kind == 'ok':
        return rc == 0 and PREFIX + 'ok' in lines
    if PREFIX + 'armed' not in lines or PREFIX + 'ok' in lines:
        return False
    if kind == 'oom':
        return rc != 0 and unit_result == 'oom-kill'
    return rc != 0 and unit_result == 'timeout' and 2 <= elapsed < 7


def run_probe(jail: Path, ws: Path, ro: Path, name: str,
              code: str, kind: str, venv: Path | None, torch_ipc: str = "default") -> dict:
    unit = 'jail-redteam-' + uuid.uuid4().hex
    gate = ws / 'gate'
    gate.unlink(missing_ok=True)
    # Wait for the host to verify effective cgroup properties before allocating.
    guard = '''
print('REDTEAM:ready', flush=True)
for _ in range(100):
    if pathlib.Path('gate').exists():
        break
    time.sleep(.02)
else:
    raise RuntimeError('host did not verify unit limits')
print('REDTEAM:armed', flush=True)
''' if name in ('memory-limit', 'task-limit', 'time-limit') else ''
    source = PRELUDE + guard + textwrap.dedent(code) + "\nprint('REDTEAM:ok', flush=True)\n"
    python = str(venv / 'bin/python') if venv else '/usr/bin/python3'
    cmd = [sys.executable, str(jail), '--ws', str(ws), '--ro', str(ro),
           '--mem', '96M', '--tasks', '16', '--timeout', '3' if kind == 'timeout' else '12',
           '--fsize', '1M', '--unit', unit, '--torch-ipc', torch_ipc]
    if venv:
        cmd += ['--venv', str(venv)]
    cmd += ['--', python, '-c', source]
    start = time.monotonic()
    # Files avoid pipe deadlocks if a faulty sandbox leaves descendants behind.
    with tempfile.TemporaryFile(mode='w+') as out, tempfile.TemporaryFile(mode='w+') as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err,
                                env={**os.environ, 'REDTEAM_SECRET': 'nonsecret-test-marker'})
        problem = None
        unit_result = None
        try:
            if guard:
                verified = False
                for _ in range(40):
                    result = subprocess.run(['systemctl', '--user', 'show', unit,
                                             '-p', 'MemoryMax', '-p', 'MemorySwapMax',
                                             '-p', 'TasksMax', '-p', 'RuntimeMaxUSec',
                                             '-p', 'KillMode'], capture_output=True, text=True, timeout=2)
                    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
                    expected = {'MemoryMax': str(96*1024*1024), 'MemorySwapMax': '0',
                                'TasksMax': '16', 'KillMode': 'control-group',
                                'RuntimeMaxUSec': '3s' if kind == 'timeout' else '12s'}
                    if result.returncode == 0 and all(props.get(k) == v for k, v in expected.items()):
                        verified = True
                        gate.touch()
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(.025)
                if not verified:
                    problem = 'could not verify effective systemd limits'
            proc.wait(timeout=18)
            elapsed = time.monotonic() - start
            if kind in ('oom', 'timeout'):
                # --collect removes the unit, so query the durable structured
                # manager event, scoped to our unique name and current boot.
                for _ in range(10):
                    journal = subprocess.run(
                        ['journalctl', '--user', '-b', '--no-pager', '-o', 'json',
                         'USER_UNIT=' + unit + '.service'],
                        capture_output=True, text=True, timeout=3)
                    for line in journal.stdout.splitlines():
                        event = json.loads(line)
                        if event.get('SYSLOG_IDENTIFIER') == 'systemd':
                            unit_result = event.get('UNIT_RESULT', unit_result)
                    if unit_result:
                        break
                    time.sleep(.1)
                if not unit_result:
                    problem = 'missing systemd termination evidence; resource check inconclusive'
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            problem = 'harness deadline exceeded (not a successful jail timeout)'
        finally:
            subprocess.run(['systemctl', '--user', 'stop', unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        out.seek(0); stdout = out.read()
        err.seek(0); stderr = err.read()
    return {'name': name, 'passed': problem is None and passed(kind, proc.returncode, stdout, elapsed, unit_result),
            'unit_result': unit_result, 'returncode': proc.returncode, 'seconds': round(elapsed, 3),
            'stdout': stdout, 'stderr': stderr, 'error': problem}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--venv', type=Path)
    ap.add_argument('--torch-ipc', choices=('default', 'copy'), default='default')
    ap.add_argument('--module', default='numpy', help='module to import from the shared venv')
    ap.add_argument('--jail', type=Path, default=Path(__file__).with_name('jail.py'))
    ap.add_argument('--json', type=Path, help='save full results')
    args = ap.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        ap.error('jail.py requires x86_64 Linux')
    jail = args.jail.resolve()
    venv = args.venv.absolute() if args.venv else None
    if not jail.is_file() or (venv and not (venv / 'bin/python').is_file()):
        ap.error('jail or venv Python does not exist')
    results = []
    with tempfile.TemporaryDirectory(prefix='redteam-', dir=jail.parent) as tmp:
        root = Path(tmp)
        ws, ro = root / 'workspace', root / 'readonly'
        ws.mkdir(); ro.mkdir()
        (root / 'canary').write_text('canary')
        (ro / 'canary').write_text('canary')
        (ws / 'link').symlink_to(root / 'canary')
        for name, code, kind in probes(root, ws, ro, os.getpid(), args.module):
            if name == 'venv-import' and venv is None:
                print('SKIP venv-import: supply --venv')
                results.append({'name': name, 'skipped': True})
                continue
            result = run_probe(jail, ws, ro, name, code, kind, venv, args.torch_ipc)
            results.append(result)
            print(('PASS' if result['passed'] else 'FAIL') + ' ' + name, flush=True)
            if not result['passed']:
                print(result['error'] or result['stderr'][-2000:] or result['stdout'])
            if name == 'workspace' and not result['passed']:
                print('Setup failed; remaining checks were not run.')
                break
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + '\n')
    if results and not results[0].get('passed'):
        return 2
    return int(any(not r.get('passed', r.get('skipped', False)) for r in results))


if __name__ == '__main__':
    sys.exit(main())
