#!/usr/bin/env python3
"""Discover agent catalogs without inference and launch an interactive fork."""

import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


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
    command = ([agent, 'app-server'] if agent == 'codex' else
               [agent, '--print', '--input-format', 'stream-json', '--output-format',
                'stream-json', '--verbose', '--no-session-persistence'])
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
                       'efforts': row.get('supportedEffortLevels', []) if row.get('supportsEffort') else []}
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


def choose(title, labels):
    print('\n' + title)
    print('  0) Cancel')
    for index, label in enumerate(labels, 1):
        print('  {}) {}'.format(index, display(label)))
    while True:
        try:
            answer = input('choice> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\nCancelled.')
            return None
        if answer.isascii() and answer.isdecimal() and int(answer) <= len(labels):
            return int(answer) - 1 if int(answer) else None
        print('Enter a number from 0 to {}.'.format(len(labels)))


def launch(agent, selection, model=None, effort=None):
    args = ['catchup', 'fork'] + selection
    if model:
        args += ['--model', model]
    env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix='herdr-catchup-fork-') as directory:
        if effort:
            executable = shutil.which(agent)
            if not executable:
                raise OSError('{} not found on PATH'.format(agent))
            flags = ['-c', 'model_reasoning_effort=' + effort] if agent == 'codex' else ['--effort', effort]
            # catchup owns native fork semantics; intercept only this child launch
            # to supply effort, which catchup does not expose as an argument.
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
    print('Fetching {} models…'.format(agent), flush=True)
    try:
        models = discover(agent)
    except DiscoveryError as error:
        print('Could not fetch models: {}.'.format(error))
        models = []
    choice = choose('Fork model (agent-reported catalog)', ['Fork with existing settings'] +
                    ['{} [{}]'.format(m['name'], m['id']) for m in models])
    if choice is None:
        return 0
    if choice == 0:
        return launch(agent, selection)
    model = models[choice - 1]
    effort = None
    if model['efforts']:
        index = choose('Effort for ' + model['name'], model['efforts'])
        if index is None:
            return 0
        effort = model['efforts'][index]
    return launch(agent, selection, model['id'], effort)


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
    except OSError as error:
        print('herdr-catchup: {}'.format(error), file=sys.stderr)
        sys.exit(1)
