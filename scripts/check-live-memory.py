#!/usr/bin/env python3
"""Opt-in native DeepSeek memory write/new-session recall, at most six stream entries.

The explicit trusted helper supplies only this driver's child environment. No
browser credentials, existing DSH_HOME, provider file or global environment changes.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--live', action='store_true')
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--secret-helper', type=Path, required=True)
parser.add_argument('--plugin-src', type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
if not args.live:
    parser.error('--live is required; this check spends authorized model requests')
runtime = args.runtime.resolve()
source = args.plugin_src.resolve()
helper = args.secret_helper.resolve()
assert helper.is_file() and os.access(helper, os.X_OK), 'explicit trusted secret helper must be executable'
work = Path(tempfile.mkdtemp(prefix='memory-live-acceptance-'))
os.chmod(work, 0o700)
os.umask(0o077)
home = work / 'home'
home.mkdir()
env = dict(os.environ, DSH_HOME=str(home), DSH_AGENTS_HOME=str(work / '.agents'), CI='true', OTEL_SDK_DISABLED='true')
node = os.environ.get('DSH_NODE') or shutil.which('node')
assert node, 'Node.js is required'
cli = str(runtime / 'node_modules/@deepseek-ai/dsh/lib/bin.js')
processes = []

def run(command, name, limit=90, data=None):
    with (work / (name + '.out')).open('w') as out, (work / (name + '.err')).open('w') as err:
        process = subprocess.Popen(command, env=env, cwd=work, stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
        processes.append(process)
        try:
            process.communicate(input=data, timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            raise RuntimeError(name + ' timed out')
        assert process.returncode == 0, name + ' failed; private diagnostics retained until cleanup'
    return (work / (name + '.out')).read_text()

try:
    run([node, cli, 'plugin', '--profile', 'memory-live', 'add', 'link:' + str(source)], 'install', 70)
    package = home / 'profiles/memory-live/package.json'
    data = json.loads(package.read_text())
    data['dsh']['profile']['bundles'] = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless', 'dsh-memory-evolve']
    package.write_text(json.dumps(data, indent=2) + '\n')
    budget = work / 'budget.mjs'
    budget.write_text('''import {readFileSync,writeFileSync,existsSync} from 'node:fs';
import {join} from 'node:path';
export const name='memory-live-budget'; export const inject=['llm'];
export function apply(ctx){
ctx.on('llm/stream',function(options,next){
const file=join(process.env.DSH_HOME,'request-count');
const count=existsSync(file)?Number(readFileSync(file,'utf8')):0;
if(count>=6)throw new Error('Authorized live request budget exhausted');
writeFileSync(file,String(count+1),{mode:0o600});return next();});}
''')
    patch = home / 'profiles/memory-live/cordis.patch.yml'
    patch.write_text(f'''- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-flash
    maxTokens: 1024
- id: llm-deepseek
  config:
    apiKeyEnv: DEEPSEEK_API_KEY
    baseURL: https://api.deepseek.com/anthropic
    thinking: disabled
    maxTokens: 4096
    retryPolicy: {{mode: normal, maxRetries: 0}}
- id: dsh-memory-evolve
  config:
    memoryDir: {work / 'memory'}
    skillDir: {work / 'skills'}
    coiSyncSkills: false
    reviewEnabled: false
    channelSendEnabled: false
- insert:
    - id: memory-live-budget
      name: {budget}
''')
    key = subprocess.run([str(helper), 'PI_DEEPSEEK_API_KEY'], capture_output=True, check=True, timeout=15).stdout.strip()
    assert key and b'\n' not in key
    env = dict(env, DEEPSEEK_API_KEY=key.decode())
    del key
    marker = 'MEMP-' + secrets.token_hex(6)
    prompt1 = '这是已授权的隔离记忆验收。请使用 memory 工具 action=add target=memory，保存以下长期事实：隔离实验项目蓝鹭的实验代号是 ' + marker + '。写入成功后简短确认。不要运行命令、修改其他文件或调用外部代理。'
    output1 = run([node, cli, '--profile', 'memory-live', '--json', prompt1], 'turn1', 100)
    events1 = [json.loads(line) for line in output1.splitlines() if line.startswith('{')]
    calls = [event for event in events1 if event.get('type') == 'tool_call' and event.get('tool') == 'memory']
    assert any(marker in json.dumps(event) for event in calls), 'model must actually call memory tool with synthetic fact'
    assert any(event.get('type') == 'tool_result' and event.get('status') == 'completed' for event in events1)
    prompt2 = '隔离实验项目蓝鹭的实验代号是什么？请从已有记忆回答，只输出代号。如果没有找到请说不知道。不要写入记忆、运行命令或修改文件。'
    assert marker not in prompt2
    output2 = run([node, cli, '--profile', 'memory-live', '--json', prompt2], 'turn2', 100)
    events2 = [json.loads(line) for line in output2.splitlines() if line.startswith('{')]
    session1 = next(event['sessionId'] for event in events1 if event.get('type') == 'session')
    session2 = next(event['sessionId'] for event in events2 if event.get('type') == 'session')
    assert session1 != session2, 'recall must happen in a genuinely new session'
    final = next(event['text'] for event in events2 if event.get('type') == 'final')
    assert marker in final, 'new session must recall value absent from its prompt'
    requests = int((home / 'request-count').read_text())
    assert requests <= 6
    completed_steps = sum(event.get('type') == 'status' and event.get('phase') == 'step_end' for event in events1 + events2)
    print(json.dumps({'result': 'PASS', 'journey': 'model memory tool write -> new session recall', 'turns': 2, 'llm_stream_entries': requests, 'completed_model_steps': completed_steps, 'sessions_distinct': True}))
except Exception as error:
    # Preserve a sanitized error code before disposing the synthetic data.
    print(json.dumps({'result': 'FAIL', 'error': str(error), 'private_work': str(work)}))
    raise
finally:
    env.pop('DEEPSEEK_API_KEY', None)
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=4)
    subprocess.run(['trash-put', str(work)], check=True, timeout=10)
    print('CLEANUP bounded native model processes stopped; synthetic memory and credentials moved to Trash')
