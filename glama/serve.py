#!/usr/bin/env python3
"""Stdio front for MCP Server for Odoo, used by directory checks such as Glama's.

The module answers MCP over Streamable HTTP at /mcp inside Odoo. Directories start servers as
stdio processes, so this script:

1. starts the PostgreSQL and Odoo prepared by glama/build.sh (127.0.0.1 only, inside the container);
2. reads JSON-RPC messages from stdin, one per line, posts each one to /mcp with the API key,
   and writes the answers to stdout, one per line. Logs go to stderr.

    python3 glama/serve.py                     # in the image built by glama/build.sh
    python3 glama/serve.py --url http://127.0.0.1:8069/mcp --key-file KEYFILE --no-start

Standard library only. Not needed to use the module: MCP clients connect to /mcp directly.
"""
import argparse
import base64
import glob
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ODOO_BIN = '/opt/odoo/odoo-bin'
CONF = '/etc/odoo-glama.conf'
KEY_FILE = '/var/lib/odoo-glama/mcp_key'
URL = 'http://127.0.0.1:8069/mcp'
META_VERSION = 'io.modelcontextprotocol/protocolVersion'
NAMED = {'tools/call': 'name', 'prompts/get': 'name', 'resources/read': 'uri'}


def log(*args):
    print('[serve]', *args, file=sys.stderr, flush=True)


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


class LocalOdoo:
    """PostgreSQL and Odoo as set up by glama/build.sh."""

    def __init__(self):
        self.cluster = None
        self.odoo = None

    def start(self):
        clusters = sorted(glob.glob('/etc/postgresql/*/main'),
                          key=lambda p: float(p.split('/')[3]))
        if not clusters:
            sys.exit('No PostgreSQL cluster found: run glama/build.sh first.')
        self.cluster = clusters[-1].split('/')[3]
        # Some container runtimes start with an empty /run: recreate the socket directory.
        subprocess.run(['install', '-d', '-m', '2775', '-o', 'postgres', '-g', 'postgres',
                        '/run/postgresql'], check=True)
        subprocess.run(['pg_ctlcluster', self.cluster, 'main', 'start'], check=True,
                       stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
        self.odoo = subprocess.Popen(
            ['runuser', '-u', 'odoo', '--', '/usr/bin/python3', ODOO_BIN, '-c', CONF],
            stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)

    def alive(self):
        return self.odoo is None or self.odoo.poll() is None

    def stop(self):
        if self.odoo and self.odoo.poll() is None:
            self.odoo.terminate()
            try:
                self.odoo.wait(15)
            except subprocess.TimeoutExpired:
                self.odoo.kill()
        if self.cluster:
            subprocess.run(['pg_ctlcluster', self.cluster, 'main', 'stop', '-m', 'fast'],
                           stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
            self.cluster = None


def wait_ready(url, local, timeout):
    """Wait until Odoo answers, then make one request so the database registry is loaded."""
    base = url.rsplit('/mcp', 1)[0]
    deadline = time.monotonic() + timeout
    while True:
        if not local.alive():
            sys.exit('Odoo stopped while starting; see the log above.')
        try:
            with urllib.request.urlopen(base + '/web/health', timeout=5) as resp:
                if resp.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() > deadline:
            sys.exit('Odoo did not start within %d seconds.' % timeout)
        time.sleep(0.5)
    warm = urllib.request.Request(url, data=b'{"jsonrpc":"2.0","id":0,"method":"ping"}',
                                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(warm, timeout=timeout).close()
    except urllib.error.HTTPError:
        pass  # 401 without a key is the expected answer; the registry is now loaded
    except (urllib.error.URLError, OSError) as exc:
        log('warm-up request failed:', exc)


def header_value(value):
    try:
        value.encode('ascii')
        return value
    except UnicodeEncodeError:
        return '=?base64?%s?=' % base64.b64encode(value.encode('utf-8')).decode('ascii')


class Bridge:
    def __init__(self, url, key, database=None):
        self.url = url
        self.key = key
        self.database = database
        self.legacy_version = None  # negotiated by an initialize handshake

    def headers_for(self, message):
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
            'Authorization': 'Bearer ' + self.key,
        }
        if self.database:
            headers['X-Odoo-Database'] = self.database
        if not isinstance(message, dict):  # JSON-RPC batch: 2025-03-26 only
            if self.legacy_version:
                headers['MCP-Protocol-Version'] = self.legacy_version
            return headers
        method = message.get('method')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        meta = params.get('_meta') if isinstance(params.get('_meta'), dict) else {}
        version = meta.get(META_VERSION)
        if version:  # MCP 2026-07-28 and later: version, method and name travel as headers too
            headers['MCP-Protocol-Version'] = str(version)
            if method:
                headers['Mcp-Method'] = method
            name = params.get(NAMED.get(method, ''))
            if isinstance(name, str):
                headers['Mcp-Name'] = header_value(name)
        elif method != 'initialize' and self.legacy_version:
            headers['MCP-Protocol-Version'] = self.legacy_version
        return headers

    def post(self, message):
        body = json.dumps(message, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(self.url, data=body, method='POST',
                                         headers=self.headers_for(message))
        try:
            with urllib.request.urlopen(request, timeout=300) as resp:
                return resp.status, resp.headers.get('Content-Type', ''), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get('Content-Type', ''), exc.read()

    def handle(self, message):
        msg_id = message.get('id') if isinstance(message, dict) else None
        try:
            status, ctype, raw = self.post(message)
        except (urllib.error.URLError, OSError) as exc:
            log('request failed:', exc)
            if msg_id is not None:
                emit({'jsonrpc': '2.0', 'id': msg_id,
                      'error': {'code': -32603, 'message': 'Odoo is not reachable'}})
            return
        if status == 202 or not raw.strip():
            if msg_id is not None and status >= 400:
                emit({'jsonrpc': '2.0', 'id': msg_id,
                      'error': {'code': -32603, 'message': 'HTTP %d from Odoo' % status}})
            return
        answers = []
        text = raw.decode('utf-8', 'replace')
        if 'text/event-stream' in ctype:
            for line in text.splitlines():
                if line.startswith('data:') and line[5:].strip():
                    answers.append(line[5:].strip())
        else:
            answers.append(text)
        for answer in answers:
            try:
                payload = json.loads(answer)
            except ValueError:
                log('non-JSON answer with HTTP %d: %s' % (status, answer[:200]))
                if msg_id is not None:
                    emit({'jsonrpc': '2.0', 'id': msg_id,
                          'error': {'code': -32603, 'message': 'HTTP %d from Odoo' % status}})
                continue
            if isinstance(message, dict) and message.get('method') == 'initialize':
                result = payload.get('result') if isinstance(payload, dict) else None
                if isinstance(result, dict) and result.get('protocolVersion'):
                    self.legacy_version = str(result['protocolVersion'])
            emit(payload)

    def run(self):
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                emit({'jsonrpc': '2.0', 'id': None,
                      'error': {'code': -32700, 'message': 'Parse error'}})
                continue
            self.handle(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--url', default=URL, help='MCP endpoint of the Odoo server')
    parser.add_argument('--key-file', default=KEY_FILE, help='file holding the Odoo API key')
    parser.add_argument('--database', help='send X-Odoo-Database (Odoo 19 and later)')
    parser.add_argument('--no-start', action='store_true',
                        help='do not start PostgreSQL and Odoo; use a server that is already running')
    parser.add_argument('--timeout', type=int, default=180, help='seconds to wait for Odoo')
    args = parser.parse_args()

    key = (os.environ.get('ODOO_MCP_API_KEY') or '').strip()
    if not key:
        with open(args.key_file) as handle:
            key = handle.read().strip()

    local = LocalOdoo()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        if not args.no_start:
            local.start()
        wait_ready(args.url, local, args.timeout)
        log('ready: forwarding stdio to', args.url)
        Bridge(args.url, key, args.database).run()
    except KeyboardInterrupt:
        pass
    finally:
        local.stop()


if __name__ == '__main__':
    main()
