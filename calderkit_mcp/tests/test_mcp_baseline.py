"""F01: without add-on modules, the public MCP surface must not change.

The baseline file is recorded once on the unmodified module
(CALDERKIT_RECORD_BASELINE=1) and compared on every later run, byte for byte on the
raw response text. Only the server version string is replaced, because a new module
version is the one allowed change.
"""
import json
import os
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged

BASELINE_FILE = os.path.join(os.path.dirname(__file__), 'baseline_mcp.json')
LEGACY = '2025-11-25'
MODERN = '2026-07-28'
UI_URI = 'ui://calderkit/view.html'


@tagged('post_install', '-at_install')
class TestMcpBaseline(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env, login='mcp_baseline', name='MCP Baseline',
            groups='base.group_user,base.group_partner_manager,calderkit_mcp.group_mcp_user')
        cls.key = cls.env['res.users.apikeys'].with_user(cls.user)._generate(
            'mcp', 'baseline', fields.Datetime.now() + timedelta(days=1))
        cls.env['calderkit.mcp.model']._mcp_add_defaults()

    def _installed(self, name):
        return bool(self.env['ir.module.module'].search_count(
            [('name', '=', name), ('state', '=', 'installed')]))

    def _post(self, payload, headers):
        all_headers = {'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream',
                       'Authorization': 'Bearer %s' % self.key,
                       'X-Odoo-Database': self.env.cr.dbname}
        all_headers.update(headers)
        response = self.url_open('/mcp', data=json.dumps(payload), headers=all_headers)
        version = self.env['ir.module.module']._get('calderkit_mcp').latest_version
        text = response.text.replace('"%s"' % version, '"<version>"')
        return {'status': response.status_code, 'content_type': response.headers.get('Content-Type'),
                'text': text}

    def _legacy(self, method, params=None, msg_id=1):
        payload = {'jsonrpc': '2.0', 'id': msg_id, 'method': method}
        if params is not None:
            payload['params'] = params
        headers = {} if method == 'initialize' else {'MCP-Protocol-Version': LEGACY}
        return self._post(payload, headers)

    def _modern(self, method, params=None, name=None, msg_id=1):
        params = dict(params or {})
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': MODERN,
                           'io.modelcontextprotocol/clientInfo': {'name': 'baseline', 'version': '1'}}
        headers = {'MCP-Protocol-Version': MODERN, 'Mcp-Method': method}
        if name is not None:
            headers['Mcp-Name'] = name
        return self._post({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params}, headers)

    def _surface(self):
        return {
            'legacy/initialize': self._legacy('initialize', {
                'protocolVersion': LEGACY, 'capabilities': {},
                'clientInfo': {'name': 'baseline', 'version': '1'}}),
            'legacy/tools/list': self._legacy('tools/list'),
            'legacy/resources/list': self._legacy('resources/list'),
            'legacy/resources/templates/list': self._legacy('resources/templates/list'),
            'legacy/prompts/list': self._legacy('prompts/list'),
            'legacy/resources/read': self._legacy('resources/read', {'uri': UI_URI}),
            'legacy/resources/read-no-uri': self._legacy('resources/read', {}),
            'modern/server/discover': self._modern('server/discover'),
            'modern/tools/list': self._modern('tools/list'),
            'modern/resources/list': self._modern('resources/list'),
            'modern/resources/templates/list': self._modern('resources/templates/list'),
            'modern/prompts/list': self._modern('prompts/list'),
            'modern/resources/read': self._modern('resources/read', {'uri': UI_URI}, name=UI_URI),
            'modern/resources/read-no-uri': self._modern('resources/read', {}),
            'legacy/server/discover': self._legacy('server/discover'),
            'legacy/ping': self._legacy('ping'),
            'modern/initialize': self._modern('initialize', {
                'protocolVersion': MODERN, 'capabilities': {},
                'clientInfo': {'name': 'baseline', 'version': '1'}}),
            'modern/ping': self._modern('ping'),
        }

    def test_f01_surface_unchanged_without_addons(self):
        if self._installed('calderkit_mcp_pro') or self._installed('calderkit_mcp_apps'):
            self.skipTest('F01 compares the free module alone; an add-on module is installed.')
        current = self._surface()
        if os.environ.get('CALDERKIT_RECORD_BASELINE'):
            with open(BASELINE_FILE, 'w', encoding='utf-8') as handle:
                json.dump(current, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.write('\n')
            self.skipTest('Baseline recorded.')
        with open(BASELINE_FILE, encoding='utf-8') as handle:
            expected = json.load(handle)
        self.assertEqual(sorted(current), sorted(expected))
        for key in sorted(expected):
            self.assertEqual(current[key]['status'], expected[key]['status'], key)
            self.assertEqual(current[key]['content_type'], expected[key]['content_type'], key)
            self.assertEqual(current[key]['text'], expected[key]['text'], key)  # byte for byte
