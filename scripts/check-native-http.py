#!/usr/bin/env python3
"""Install into a disposable DSH web profile and exercise real authenticated HTTP.

Requires the installed DSH 0.2.1-rc.3 runtime. All writes and synthetic credentials
stay in a fresh DSH_HOME. --plugin-src can point to an installed source snapshot.
"""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--plugin-src', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--runtime', type=Path, default=Path('/home/dhz/.local/share/deepseek-harness/runtime/0.2.1-rc.3'))
args = parser.parse_args()
node = os.environ.get('DSH_NODE', 'node')
cli = args.runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js'
work = Path(tempfile.mkdtemp(prefix='memory-native-cli-'))
os.chmod(work, 0o700)
env = dict(os.environ, DSH_HOME=str(work / 'home'), DSH_AGENTS_HOME=str(work / 'agents'), CI='true', OTEL_SDK_DISABLED='true')
proc = None
log = work / 'native.log'
log.touch(mode=0o600)
try:
    with log.open('w') as output:
        result = subprocess.run([node, str(cli), 'plugin', '--profile', 'web', 'add', 'link:' + str(args.plugin_src.resolve())],
                                env=env, cwd=work, stdout=output, stderr=output, timeout=70)
        assert result.returncode == 0, 'isolated plugin installation failed'
        patch = work / 'home/profiles/web/cordis.patch.yml'
        patch.write_text('- id: dsh-memory-evolve\n  config:\n' +
                         f'    memoryDir: {work / "memory"}\n    skillDir: {work / "skills"}\n' +
                         '    coiEnabled: true\n    coiSyncSkills: false\n    channelSendEnabled: false\n')
        with socket.socket() as lease:
            lease.bind(('127.0.0.1', 0))
            port = lease.getsockname()[1]
        proc = subprocess.Popen([node, str(cli), 'web', '--no-open', '--host', '127.0.0.1', '--port', str(port)],
                                env=env, cwd=work, stdout=output, stderr=output, start_new_session=True)
        deadline = time.monotonic() + 30
        auth_url = None
        while time.monotonic() < deadline:
            match = re.search(r'http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_%-]+', log.read_text())
            if match:
                auth_url = match.group()
                break
            assert proc.poll() is None, 'native DSH exited before readiness'
            time.sleep(0.1)
        assert auth_url, 'native DSH did not publish readiness'
        base = f'http://127.0.0.1:{port}'
        owner = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        anonymous = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def call(path, method='GET', body=None, authenticated=True, origin=None):
            request = urllib.request.Request(base + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={'Origin': origin or base, 'Content-Type': 'application/json'})
            try:
                response = (owner if authenticated else anonymous).open(request, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.loads(response.read())

        status, _ = call('/memory-evolve/api/config', authenticated=False)
        assert status == 401, f'anonymous config status {status}'
        with owner.open(auth_url, timeout=8) as response:
            assert response.status == 200
        assert call('/memory-evolve/api/config')[0] == 200
        assert call('/memory-evolve/api/config', origin='https://foreign.invalid')[0] == 403
        assert call('/memory-evolve/api/config', 'POST', {'patch': {'coiEnabled': False}}, False)[0] == 401
        assert call('/memory-evolve/api/memory/memory', 'POST', {'content': 'Full native CLI owner memory roundtrip'})[0] == 200
        status, files = call('/memory-evolve/api/memory-files')
        assert status == 200 and 'Full native CLI owner memory roundtrip' in json.dumps(files)
        adapter = {'id': 'bounded-echo', 'name': 'Bounded echo', 'type': 'plain-cli', 'binary': node,
                   'args': ['-e', 'console.log("bounded CLI task")'],
                   'testCmd': ['-e', 'console.log("bounded CLI success")'], 'defaults': {'timeoutMs': 2000}}
        assert call('/memory-evolve/api/coi/adapters', 'POST', {'def': adapter}, False)[0] == 401
        assert call('/memory-evolve/api/coi/adapters', 'POST', {'def': adapter})[0] == 200
        assert call('/memory-evolve/api/coi/adapters/test', 'POST', {'id': adapter['id']}, False)[0] == 401
        status, started = call('/memory-evolve/api/coi/adapters/test', 'POST', {'id': adapter['id']})
        assert status == 200 and started['ok']
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            status, detail = call('/memory-evolve/api/coi/tasks/' + started['taskId'])
            if detail['task']['status'] in ('completed', 'failed', 'killed'):
                break
            time.sleep(0.05)
        assert status == 200 and detail['task']['status'] == 'completed', 'bounded adapter must settle successfully'
        status, result = call('/memory-evolve/api/coi/tasks/' + started['taskId'] + '/log')
        assert status == 200 and 'bounded CLI success' in json.dumps(result)
        print('PASS full DSH CLI install/start + native authentication + memory write/read + owner adapter command')
finally:
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
    # Move only this invocation's data and synthetic credential into host Trash.
    subprocess.run(['trash-put', str(work)], check=True, timeout=10)
    print('CLEANUP native process group stopped; isolated DSH_HOME/logs moved to Trash')
