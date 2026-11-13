"""F02-F04: the resource extension point used by add-on modules (MCP Apps)."""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged

LEGACY = '2025-11-25'
MODERN = '2026-07-28'
META_SERVER = 'io.modelcontextprotocol/serverInfo'
TEST_URI = 'ui://calderkit-test/resource.html'
TEST_HTML = '<!DOCTYPE html><html><body><p>test</p></body></html>'


def _fake_resources(self):
    return {TEST_URI: ({'uri': TEST_URI, 'name': 'test-resource', 'title': 'Test resource',
                        'mimeType': 'text/html'}, '_ck_test_read_resource')}


def _fake_read(self, uri):
    return {'contents': [{'uri': uri, 'mimeType': 'text/html', 'text': TEST_HTML}]}


@tagged('post_install', '-at_install')
class TestMcpResources(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env, login='mcp_res_user', name='MCP Resource User',
            groups='base.group_user,calderkit_mcp.group_mcp_user')
        cls.key = cls.env['res.users.apikeys'].with_user(cls.user)._generate(
            'mcp', 'resources', fields.Datetime.now() + timedelta(days=1))

    def setUp(self):
        super().setUp()
        server_class = type(self.env['calderkit.mcp.server'])
        patches = [
            patch.object(server_class, '_mcp_resources', _fake_resources, create=True),
            patch.object(server_class, '_ck_test_read_resource', _fake_read, create=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    # helpers -----------------------------------------------------------

    def _post(self, payload, headers):
        all_headers = {'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream',
                       'Authorization': 'Bearer %s' % self.key,
                       'X-Odoo-Database': self.env.cr.dbname}
        all_headers.update(headers)
        return self.url_open('/mcp', data=json.dumps(payload), headers=all_headers)

    def _legacy(self, method, params=None):
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        headers = {} if method == 'initialize' else {'MCP-Protocol-Version': LEGACY}
        return self._post(payload, headers)

    def _modern(self, method, params=None, name=None):
        params = dict(params or {})
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': MODERN,
                           'io.modelcontextprotocol/clientInfo': {'name': 'test', 'version': '1'}}
        headers = {'MCP-Protocol-Version': MODERN, 'Mcp-Method': method}
        if name is not None:
            headers['Mcp-Name'] = name
        return self._post({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, headers)

    def _unknown_tool_status(self, era):
        """HTTP status the server uses for an INVALID_PARAMS error in this era."""
        if era == 'modern':
            response = self._modern('tools/call', {'name': 'no_such_tool', 'arguments': {}},
                                    name='no_such_tool')
        else:
            response = self._legacy('tools/call', {'name': 'no_such_tool', 'arguments': {}})
        self.assertEqual(response.json()['error']['code'], -32602)
        return response.status_code

    # F02 ---------------------------------------------------------------

    def test_f02_list_and_capabilities(self):
        for response in (self._legacy('resources/list'), self._modern('resources/list')):
            self.assertEqual(response.status_code, 200, response.text)
            resources = response.json()['result']['resources']
            self.assertEqual([r['uri'] for r in resources], [TEST_URI])
            self.assertEqual(resources[0]['mimeType'], 'text/html')
        init = self._legacy('initialize', {'protocolVersion': LEGACY, 'capabilities': {},
                                           'clientInfo': {'name': 't', 'version': '1'}})
        self.assertIn('resources', init.json()['result']['capabilities'])
        discover = self._modern('server/discover')
        self.assertIn('resources', discover.json()['result']['capabilities'])

    def test_f02_read_success(self):
        legacy = self._legacy('resources/read', {'uri': TEST_URI})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()['result']['contents'][0]['text'], TEST_HTML)
        modern = self._modern('resources/read', {'uri': TEST_URI}, name=TEST_URI)
        self.assertEqual(modern.status_code, 200, modern.text)
        self.assertEqual(modern.json()['result']['contents'][0]['text'], TEST_HTML)

    def test_f02_read_missing_uri(self):
        for era in ('legacy', 'modern'):
            if era == 'legacy':
                response = self._legacy('resources/read', {})
            else:
                response = self._modern('resources/read', {})  # no uri and no Mcp-Name header
            self.assertEqual(response.json()['error']['code'], -32602, (era, response.text))
            self.assertEqual(response.status_code, self._unknown_tool_status(era), era)

    def test_f02_read_unknown_uri(self):
        unknown = 'ui://calderkit-test/missing.html'
        legacy = self._legacy('resources/read', {'uri': unknown})
        self.assertEqual(legacy.json()['error']['code'], -32002, legacy.text)
        self.assertEqual(legacy.status_code, self._unknown_tool_status('legacy'))
        modern = self._modern('resources/read', {'uri': unknown}, name=unknown)
        self.assertEqual(modern.json()['error']['code'], -32602, modern.text)
        self.assertEqual(modern.status_code, self._unknown_tool_status('modern'))

    # F03 ---------------------------------------------------------------

    def test_f03_result_shape_per_era(self):
        modern = self._modern('resources/read', {'uri': TEST_URI}, name=TEST_URI).json()['result']
        self.assertEqual(set(modern), {'contents', 'resultType', 'ttlMs', 'cacheScope', '_meta'})
        self.assertEqual(modern['resultType'], 'complete')
        self.assertIs(type(modern['ttlMs']), int)
        self.assertEqual(modern['ttlMs'], 300000)
        self.assertEqual(modern['cacheScope'], 'private')
        self.assertEqual(set(modern['_meta'][META_SERVER]), {'name', 'title', 'version'})
        self.assertEqual(modern['contents'], [{'uri': TEST_URI, 'mimeType': 'text/html', 'text': TEST_HTML}])
        legacy = self._legacy('resources/read', {'uri': TEST_URI}).json()['result']
        self.assertEqual(legacy, {'contents': [{'uri': TEST_URI, 'mimeType': 'text/html', 'text': TEST_HTML}]})

    # F04 ---------------------------------------------------------------

    def test_f04_name_header_must_match_uri(self):
        response = self._modern('resources/read', {'uri': TEST_URI}, name='ui://other')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['error']['code'], -32020)
