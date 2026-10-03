#!/usr/bin/env python3
"""Real PTY + MuJoCo, local mock HTTP only. Never counts as cloud model acceptance."""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'target/debug/robo-archon'
TOKEN = 'local-tui-test-token'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--viewer', action='store_true', help='Also test native MuJoCo viewer in the Ctrl-C case')
    parser.add_argument('--report', type=Path, default=ROOT / 'tmp-episodes/m2g5-tui-acceptance.json')
    args = parser.parse_args()
    directory = ROOT / 'tmp-episodes/m2g5-tui-tests'
    directory.mkdir(parents=True, exist_ok=True)
    requests = []
    slow_started = threading.Event()
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                assert self.path == '/v1/chat/completions'
                assert self.headers['Authorization'] == f'Bearer {TOKEN}'
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert all('_archon_trace' not in m for m in data['messages'])
                request_id = f'tui-response-{len(requests)+1}'
                names = [t['function']['name'] for t in data.get('tools', [])]
                requests.append({'id': request_id, 'tools': names, 'model': data['model']})
                if names:
                    assert names == ['microduck_walk'], names
                    instruction = json.loads(data['messages'][-1]['content'])['instruction']
                    if instruction == 'slow':
                        slow_started.set()
                        time.sleep(2)
                    if instruction == 'http failure':
                        self.send_error(503)
                        return
                    tool = 'microduck_stand' if instruction == 'unloaded skill' else 'microduck_walk'
                    message = {'role': 'assistant', 'tool_calls': [{'id': request_id+'-call',
                        'type': 'function', 'function': {'name': tool,
                        'arguments': json.dumps({'vx': 0.4, 'duration_ms': 3000 if instruction == 'cancel motion' else 1000})}}]}
                else:
                    assert data['messages'][-1]['role'] == 'tool'
                    message = {'role': 'assistant', 'content': 'LOCAL MOCK: measured result received.'}
                payload = json.dumps({'id': request_id, 'model': 'local-tui-response-model',
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
                    'choices': [{'message': message}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when planning is cancelled.
            except Exception as error:
                errors.append(str(error))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ, TERM='xterm-256color')
    env['ROBO_ARCHON_PYTHON'] = str(ROOT / '.venv-microduck/bin/python')
    bodies = json.loads((ROOT / 'robots/catalog.json').read_text())['bodies']
    body_index = next(i for i, b in enumerate(bodies) if b['id'] == 'microduck')
    inventory = json.loads(subprocess.check_output([str(CLI), '--robot', 'microduck',
        '--backend', 'mujoco', '--list-skill-tools'], env=env, cwd=ROOT))
    manifests = {json.loads(p.read_text())['tool_name']: json.loads(p.read_text())['id']
        for p in (ROOT / 'skills').glob('*/skill.json')}
    skill_ids = [manifests[t['function']['name']] for t in inventory['tools']
        if t['function']['name'] != 'archon_sequence']
    children = []
    evidence = []

    class Session:
        def __init__(self, name):
            self.report = directory / f'{name}.json'
            self.report.unlink(missing_ok=True)
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(ROOT)
                os.execve(CLI, [str(CLI), '--tui', '--llm-api-key', TOKEN,
                    '--llm-base-url', f'http://127.0.0.1:{server.server_port}/v1',
                    '--llm-model', 'local-tui-request-model', '--skill-report', str(self.report)], env)
            self.pid, self.fd = pid, fd
            children.append(self)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 36, 110, 0, 0))
            self.output = b''
            self.exited = False

        def pump(self):
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    self.output += os.read(self.fd, 65536)
                except OSError:
                    pass

        def wait(self, predicate, timeout=20):
            deadline = time.monotonic()+timeout
            while time.monotonic() < deadline:
                self.pump()
                if predicate():
                    return
            raise AssertionError('TUI timeout; diagnostic output kept locally')

        def send(self, text):
            os.write(self.fd, text.encode())
            time.sleep(0.12)
            self.pump()

        def text(self, needle):
            self.wait(lambda: re.sub(r'\s+', '', needle) in re.sub(r'\s+', '',
                re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.output.decode('utf-8', errors='replace'))))

        def turns(self):
            try:
                return json.loads(self.report.read_text())['turns']
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                return []

        def turn(self, count, feedback=False):
            self.wait(lambda: len(self.turns()) >= count and
                (not feedback or self.turns()[count-1].get('feedback') is not None))
            # The report is saved before the UI consumes its completion event.
            # Drain PTY output so the UI can finish drawing before the next input.
            for _ in range(6):
                self.pump()
            return self.turns()[count-1]

        def configure(self, unavailable=False, viewer=False):
            self.text('请选择本体资料包')
            if unavailable:
                self.send('\x1b[A'*body_index+'\r')
                self.text('仿真窗口')
                self.send('\r')
                self.text('当前 Skill 会话 Runner 未适配')
                self.send('\x1b')
                self.send('\x1b[B'*body_index)
            self.send('\r')
            self.text('仿真窗口')
            self.send(('' if viewer else 'v')+'\r')
            self.text('microduck.walk')
            for i, skill_id in enumerate(skill_ids):
                if skill_id != 'microduck.walk':
                    self.send(' ')
                if i+1 < len(skill_ids):
                    self.send('\x1b[B')
            self.send('\r')
            self.text('microduck-tui-worker.log')

        def finish(self, key='/quit\r'):
            self.send(key)
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                self.pump()
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid:
                    self.exited = True
                    assert os.waitstatus_to_exitcode(status) == 0, status
                    assert b'\x1b[?1049l' in self.output, 'alternate screen not restored'
                    assert TOKEN.encode() not in self.output, 'credential appeared in UI'
                    return
            raise AssertionError('TUI did not shut down')

    try:
        session = Session('selection-chat')
        session.configure(unavailable=True)
        session.send('unloaded skill\r')
        rejected = session.turn(1)
        assert rejected['executed'] is False and 'error' in rejected
        session.send('walk\r')
        walked = session.turn(2, feedback=True)
        assert walked['result']['status'] == 'succeeded'
        assert walked['result']['observation']['stop_confirmed'] is True
        assert walked['planning']['trace']['response_id'].startswith('tui-response-')
        assert walked['planning']['trace']['response_model'] == 'local-tui-response-model'
        assert walked['planning']['trace']['usage']['total_tokens'] == 120
        assert walked['feedback']['trace']['response_id'] != walked['planning']['trace']['response_id']
        session.send('/reset\r')
        reset = session.turn(3)
        assert reset['observation']['episode_id'] == 1
        session.send('http failure\r')
        failed = session.turn(4)
        assert failed['executed'] is False and 'transport failed' in failed['error']
        session.send('slow\r')
        session.wait(slow_started.is_set, timeout=5)
        session.send('/stop\r')
        cancelled = session.turn(5)
        assert cancelled['executed'] is False and 'cancelled' in cancelled['error']
        session.text('已停止')
        session.send('walk\r')
        second_walk = session.turn(6, feedback=True)
        assert second_walk['result']['status'] == 'succeeded'
        assert second_walk['result']['observation']['episode_id'] == 1
        session.finish()
        evidence.append({'case':'selection_whitelist_api_trace_reset_cancel_and_resume','accepted':True,
            'planning':walked['planning'], 'feedback_trace':walked['feedback']['trace'],
            'motion_status':walked['result']['status'], 'reset_episode':reset['observation']['episode_id']})

        session2 = Session('ctrl-c')
        session2.configure(viewer=args.viewer)
        session2.send('cancel motion\r')
        session2.text('执行 Skill')
        session2.finish('\x03')
        outcome = session2.turns()[0]
        assert outcome['result']['status'] == 'cancelled'
        assert outcome['result']['observation']['stop_confirmed'] is True
        evidence.append({'case':'ctrl_c_during_motion_stops_and_restores_terminal','accepted':True,'viewer':args.viewer})
        assert not errors, errors
        for session in children:
            assert TOKEN not in session.report.read_text()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({'accepted':True,'model_source':'LOCAL MOCK, not cloud',
            'cases':evidence,'requests':requests},ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'accepted':True,'report':str(args.report),'cases':len(evidence)}))
    finally:
        for session in children:
            (directory / f'{session.report.stem}.terminal.txt').write_bytes(session.output)
            if not session.exited:
                try:
                    os.killpg(session.pid, signal.SIGKILL)
                    for _ in range(100):
                        if os.waitpid(session.pid, os.WNOHANG)[0]:
                            break
                        time.sleep(0.05)
                except ProcessLookupError:
                    pass
            os.close(session.fd)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
