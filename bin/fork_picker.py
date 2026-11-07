#!/usr/bin/env python3
"""Discover agent catalogs without inference and launch an interactive fork."""

import json
import os
from pathlib import Path
import selectors
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata


SIDE_QUESTION_PROMPT = (
    'This is a side-question fork. Treat the parent conversation as background context, '
    'not authorization to continue its work. Wait for my question. You may inspect code '
    'to answer questions. Do not edit files, commit, or change Jira, Confluence, or other '
    'external systems unless I explicitly request that action in this fork.'
)


class DiscoveryError(Exception):
    pass


class CatalogProcess:
    def __init__(self, command, timeout):
        self.deadline = time.monotonic() + timeout
        self.stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=self.stderr,
                                            start_new_session=True)
        except OSError:
            self.stderr.close()
            raise
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def receive(self):
        while b'\n' not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise DiscoveryError('model discovery timed out after 15 seconds')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise DiscoveryError('agent exited before returning its model catalog')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError('expected an object')
            return message
        except (ValueError, UnicodeError) as error:
            raise DiscoveryError('agent returned an invalid discovery response') from error

    def close(self):
        # Discovery may start MCP children; they must not outlive the catalog query.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        except ProcessLookupError:
            pass
        finally:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            self.selector.close()
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()


def discover(agent, timeout=15):
    # SessionStart hooks dominate Claude startup (seconds) and are side effects a
    # catalog query must not trigger; MCP servers are likewise irrelevant to it.
    command = ([agent, 'app-server'] if agent == 'codex' else
               [agent, '--print', '--input-format', 'stream-json', '--output-format',
                'stream-json', '--verbose', '--no-session-persistence',
                '--settings', '{"disableAllHooks":true}', '--strict-mcp-config'])
    client = None
    try:
        client = CatalogProcess(command, timeout)
        if agent == 'codex':
            client.send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'herdr-catchup', 'version': '0.8.0'}}})
            while True:
                reply = client.receive()
                if reply.get('id') == 1:
                    if 'error' in reply:
                        raise DiscoveryError('Codex rejected catalog initialization')
                    break
            client.send({'method': 'initialized', 'params': {}})
            models, cursor, seen = [], None, set()
            request_id = 2
            while True:
                params = {'includeHidden': False, 'limit': 100}
                if cursor:
                    params['cursor'] = cursor
                client.send({'id': request_id, 'method': 'model/list', 'params': params})
                while True:
                    reply = client.receive()
                    if reply.get('id') == request_id:
                        break
                if 'error' in reply:
                    raise DiscoveryError('Codex could not fetch models; check login and CLI version')
                result = reply['result']
                for row in result['data']:
                    if not row.get('hidden'):
                        models.append({'id': row['model'], 'name': row.get('displayName') or row['model'],
                                       'efforts': [e['reasoningEffort'] for e in row.get('supportedReasoningEfforts', [])]})
                cursor = result.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise DiscoveryError('Codex returned a repeated catalog page')
                seen.add(cursor)
                request_id += 1
        else:
            client.send({'type': 'control_request', 'request_id': 'catalog',
                         'request': {'subtype': 'initialize'}})
            while True:
                reply = client.receive()
                response = reply.get('response', {})
                if reply.get('type') == 'control_response' and response.get('request_id') == 'catalog':
                    break
            if response.get('subtype') != 'success':
                raise DiscoveryError('Claude rejected catalog initialization; check login and CLI version')
            models = [{'id': row['value'], 'name': row.get('displayName') or row['value'],
                       'efforts': row.get('supportedEffortLevels', []) if row.get('supportsEffort') else [],
                       **({'resolvedModel': row['resolvedModel']} if row.get('resolvedModel') else {})}
                      for row in response['response']['models']]
        if not models:
            raise DiscoveryError('the agent returned no models')
        for model in models:
            if (not isinstance(model['id'], str) or not model['id'] or
                    not isinstance(model['name'], str) or not isinstance(model['efforts'], list) or
                    any(not isinstance(e, str) or not e for e in model['efforts'])):
                raise DiscoveryError('agent returned invalid model metadata')
        return models
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise DiscoveryError('model discovery unavailable; check the installed agent and its login') from error
    finally:
        if client:
            client.close()


def display(value):
    return ''.join(c if c.isprintable() else ' ' for c in value)


def parent_settings(selection):
    try:
        result = subprocess.run(['catchup'] + selection + ['--info', '--json'],
                                capture_output=True, text=True, timeout=5, check=True)
        metadata = json.loads(result.stdout)
        return read_parent_log(metadata['path'], selection[0])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {}


def read_parent_log(path, agent):
    settings = {}
    with open(path) as stream:
        for line in stream:
            try:
                record = json.loads(line)
                if agent == 'codex' and record.get('type') == 'turn_context':
                    payload = record.get('payload', {})
                    candidate = {'model': payload.get('model'), 'effort': payload.get('effort')}
                elif agent == 'claude' and record.get('type') == 'assistant' and not record.get('isSidechain'):
                    candidate = {'model': record.get('requestedModel') or record.get('message', {}).get('model'),
                                 'effort': record.get('perTurnEffort') or record.get('effort')}
                else:
                    continue
                for key, value in candidate.items():
                    if isinstance(value, str) and value:
                        settings[key] = value
            except (ValueError, AttributeError):
                continue
    return settings


def choice_path(agent):
    directory = os.environ.get('HERDR_PLUGIN_STATE_DIR')
    if not directory:
        directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'herdr-catchup'
    return Path(directory) / ('fork-choice-' + agent + '.json')


def load_choice(agent):
    try:
        saved = json.loads(choice_path(agent).read_text())
        if (isinstance(saved, dict) and 'model' in saved and 'effort' in saved and
                all(saved[key] is None or isinstance(saved[key], str) for key in ('model', 'effort'))):
            return saved
    except (OSError, ValueError):
        pass
    return {}


def save_choice(agent, model, effort):
    temporary = None
    try:
        path = choice_path(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Separate files avoid cross-agent races; replacement prevents partial reads.
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'model': model, 'effort': effort}, stream)
        os.replace(temporary, path)
    except OSError:
        print('Could not remember this choice; the fork will still launch.', file=sys.stderr)
    finally:
        if temporary:
            try:
                temporary.unlink()
            except OSError:
                pass


def fit_line(text, width):
    result, used = [], 0
    for character in display(text):
        size = 0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1
        if used + size > width:
            break
        result.append(character)
        used += size
    return ''.join(result)


def terminal_choice(labels, default):
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    current = default if default is not None else 0
    digits = ''
    rendered_rows = 0
    try:
        tty.setcbreak(fd)
        while True:
            columns, height = shutil.get_terminal_size()
            width = max(1, columns - 1)
            capacity = max(1, height - 5)
            start = min(max(0, current - capacity + 1), max(0, len(labels) - capacity))
            rows = [('  0) Cancel', False)]
            for index in range(start, min(len(labels), start + capacity)):
                label = '{}{}) {}{}'.format('› ' if index == current else '  ', index + 1,
                                            labels[index], ' [default]' if index == default else '')
                rows.append((label, index == current))
            hint = '↑/↓ or j/k · Enter select · number + Enter · Esc cancel'
            if len(labels) > capacity:
                hint = '{}/{} · '.format(current + 1, len(labels)) + hint
            rows.append((hint, False))
            if digits:
                rows[-1] = ('Number: ' + digits + ' · Enter select · Backspace edit', False)
            if rendered_rows:
                sys.stdout.write('\r\x1b[{}A'.format(rendered_rows - 1))
            sys.stdout.write('\r\x1b[J')
            for row_index, (label, highlighted) in enumerate(rows):
                if row_index:
                    sys.stdout.write('\n\r')
                sys.stdout.write(('\x1b[7m' if highlighted else '') + fit_line(label, width) + '\x1b[0m')
            rendered_rows = len(rows)
            sys.stdout.flush()
            key = os.read(fd, 1)
            if key == b'\x1b':
                sequence = b''
                while select.select([fd], [], [], 0.1)[0] and len(sequence) < 16:
                    chunk = os.read(fd, 1)
                    if not chunk:
                        return None
                    sequence += chunk
                    if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7e:
                        break
                if not sequence:
                    return None
                key = {b'[A': b'k', b'OA': b'k', b'[B': b'j', b'OB': b'j'}.get(sequence, b'?')
            if key in (b'j', b'k'):
                current = (current + (1 if key == b'j' else -1)) % len(labels)
                digits = ''
            elif key in (b'\r', b'\n'):
                if not digits:
                    return current
                number = int(digits)
                if number <= len(labels):
                    return number - 1 if number else None
                digits = ''
            elif key in (b'', b'\x04', b'q', b'\x03'):
                return None
            elif key in (b'\x7f', b'\x08'):
                digits = digits[:-1]
            elif key in b'0123456789' and len(digits) < 6:
                digits += key.decode('ascii')
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        sys.stdout.write('\x1b[0m\n')
        sys.stdout.flush()


def choose(title, labels, default=None):
    print('\n' + title)
    if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb':
        return terminal_choice(labels, default)
    print('  0) Cancel')
    for index, label in enumerate(labels, 1):
        print('  {}) {}{}'.format(index, display(label), ' [default]' if index - 1 == default else ''))
    while True:
        try:
            answer = input('choice{}> '.format(' [{}]'.format(default + 1) if default is not None else '')).strip()
        except (EOFError, KeyboardInterrupt):
            print('\nCancelled.')
            return None
        if not answer and default is not None:
            return default
        if answer.isascii() and answer.isdecimal() and int(answer) <= len(labels):
            return int(answer) - 1 if int(answer) else None
        print('Enter a number from 0 to {}.'.format(len(labels)))


def fork_mode():
    return choose('Fork purpose', ['Side questions — wait for my question', 'Continue parent work'], default=0)


def codex_developer_instructions():
    client = None
    try:
        client = CatalogProcess(['codex', 'app-server'], 15)
        client.send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'herdr-catchup', 'version': '0.8.0'}}})
        while True:
            reply = client.receive()
            if reply.get('id') == 1:
                if 'error' in reply:
                    raise DiscoveryError('Codex rejected configuration initialization')
                break
        client.send({'method': 'initialized', 'params': {}})
        client.send({'id': 2, 'method': 'config/read', 'params': {'includeLayers': False}})
        while True:
            reply = client.receive()
            if reply.get('id') == 2:
                break
        if 'error' in reply:
            raise DiscoveryError('Codex could not read its effective developer instructions')
        instructions = reply['result']['config'].get('developer_instructions')
        if instructions is not None and not isinstance(instructions, str):
            raise DiscoveryError('Codex returned invalid developer instructions')
        return instructions or ''
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as error:
        raise DiscoveryError('Cannot preserve Codex developer instructions; side-question fork was not launched') from error
    finally:
        if client:
            client.close()


def launch(agent, selection, model=None, effort=None, prompt=None):
    args = ['catchup', 'fork'] + selection
    if model:
        args += ['--model', model]
    env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix='herdr-catchup-fork-') as directory:
        if effort or prompt:
            executable = shutil.which(agent)
            if not executable:
                raise OSError('{} not found on PATH'.format(agent))
            flags = []
            if effort:
                flags = ['-c', 'model_reasoning_effort=' + effort] if agent == 'codex' else ['--effort', effort]
            if prompt:
                if agent == 'codex':
                    existing = codex_developer_instructions()
                    instructions = existing + '\n\n' + prompt if existing else prompt
                    flags += ['-c', 'developer_instructions=' + json.dumps(instructions, ensure_ascii=False)]
                else:
                    # A resumed Claude session otherwise reuses the parent's recorded
                    # system prompt and ignores newly appended instructions.
                    flags += ['--append-system-prompt', prompt, '--system-prompt-snapshot', 'off']
            # catchup owns native fork semantics; intercept only this child launch
            # to supply effort and instructions without submitting a user turn.
            shim = Path(directory) / agent
            shim.write_text('#!/bin/sh\nexport PATH=' + shlex.quote(env.get('PATH', '')) +
                            '\nexec ' + shlex.quote(executable) + ' "$@" ' +
                            ' '.join(shlex.quote(flag) for flag in flags) + '\n')
            shim.chmod(0o700)
            env['PATH'] = directory + os.pathsep + env.get('PATH', '')
        process = subprocess.Popen(args, env=env)
        try:
            rc = process.wait()
        except SystemExit as error:
            process.send_signal(error.code - 128)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            rc = process.wait()
        return 128 - rc if rc < 0 else rc


def main(selection):
    agent = selection[0]
    saved = load_choice(agent)
    print('Fetching {} models…'.format(agent), flush=True)
    parent = parent_settings(selection)
    try:
        models = discover(agent)
    except DiscoveryError as error:
        print('Could not fetch models: {}.'.format(error))
        models = []
    default = next((index + 1 for index, model in enumerate(models) if model['id'] == saved.get('model')), 0)
    if saved.get('model') and not default and models:
        print('Previously selected model is unavailable; defaulting to parent settings.')
    parent_label = 'Fork with parent settings'
    if parent.get('model'):
        parent_label += ' [{}]'.format(parent['model'])
    else:
        parent_label += ' (native resume; parent model unavailable)'
    choice = choose('Fork model (agent-reported catalog)', [parent_label] +
                    ['{} [{}]'.format(m['name'], m['id']) for m in models], default=default)
    if choice is None:
        return 0
    selected_model = models[choice - 1] if choice else None
    model_id = selected_model['id'] if selected_model else parent.get('model')
    effective_model = selected_model or next((m for m in models if m['id'] == model_id or m.get('resolvedModel') == model_id), None)
    efforts = effective_model['efforts'] if effective_model else []
    parent_effort = parent.get('effort')
    effort = parent_effort
    saved_effort = saved.get('effort') if saved.get('model') == (selected_model['id'] if selected_model else None) else None
    effort_default = efforts.index(saved_effort) + 1 if saved_effort in efforts else 0
    if efforts and parent_effort and parent_effort not in efforts and not effort_default:
        effort_default = 1
    if efforts or not selected_model:
        label = 'Fork with parent settings'
        label += ' [{}]'.format(parent_effort) if parent_effort else ' (native resume; parent effort unavailable)'
        while True:
            index = choose('Fork effort', [label] + efforts, default=effort_default)
            if index is None:
                return 0
            if index == 0 and efforts and parent_effort and parent_effort not in efforts:
                print('The parent effort is not supported by this model; choose a supported effort or cancel.')
                continue
            effort = efforts[index - 1] if index else parent_effort
            break
    else:
        index = 0
        effort = None
    mode = fork_mode()
    if mode is None:
        return 0
    if models:
        save_choice(agent, selected_model['id'] if selected_model else None,
                    efforts[index - 1] if index else None)
    if mode == 0:
        return launch(agent, selection, model_id, effort, prompt=SIDE_QUESTION_PROMPT)
    return launch(agent, selection, model_id, effort)


if __name__ == '__main__':
    def terminate(signum, frame):
        # Pane closure must unwind discovery cleanup, including detached children.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGHUP, terminate)
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, DiscoveryError) as error:
        print('herdr-catchup: {}'.format(error), file=sys.stderr)
        sys.exit(1)
