import json
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged

MODERN = '2026-07-28'


@tagged('post_install', '-at_install')
class TestMcpServer(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env, login='mcp_user', name='MCP User',
            groups='base.group_user,base.group_partner_manager,calderkit_mcp.group_mcp_user')
        cls.key = cls.env['res.users.apikeys'].with_user(cls.user)._generate(
            'mcp', 'test', fields.Datetime.now() + timedelta(days=1))
        cls.outsider = new_test_user(cls.env, login='mcp_outsider', groups='base.group_user')
        cls.outsider_key = cls.env['res.users.apikeys'].with_user(cls.outsider)._generate(
            'mcp', 'test', fields.Datetime.now() + timedelta(days=1))
        cls.env['calderkit.mcp.model']._mcp_add_defaults()
        cls.partner = cls.env['res.partner'].create({
            'name': 'MCP Test Customer', 'email': 'mcp@example.com', 'is_company': True})

    # helpers -----------------------------------------------------------

    def _post(self, payload, key=None, headers=None, raw=None):
        all_headers = {'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream',
                       'X-Odoo-Database': self.env.cr.dbname}
        if key is not False:
            all_headers['Authorization'] = 'Bearer %s' % (key or self.key)
        all_headers.update(headers or {})
        data = raw if raw is not None else json.dumps(payload)
        return self.url_open('/mcp', data=data, headers=all_headers)

    def _modern(self, method, params=None, name=None, msg_id=1, key=None, extra_headers=None):
        params = dict(params or {})
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': MODERN,
                           'io.modelcontextprotocol/clientInfo': {'name': 'test-client', 'version': '1'}}
        headers = {'MCP-Protocol-Version': MODERN, 'Mcp-Method': method}
        if name is not None:
            headers['Mcp-Name'] = name
        headers.update(extra_headers or {})
        return self._post({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params},
                          key=key, headers=headers)

    def _call(self, tool, arguments):
        response = self._modern('tools/call', {'name': tool, 'arguments': arguments}, name=tool)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['result']

    # protocol ----------------------------------------------------------

    def test_legacy_initialize_and_list(self):
        response = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'legacy', 'version': '1'}}})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['result']
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        self.assertIn('tools', result['capabilities'])
        response = self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                              headers={'MCP-Protocol-Version': '2025-06-18'})
        self.assertEqual(response.status_code, 202)
        response = self._post({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                              headers={'MCP-Protocol-Version': '2025-06-18'})
        names = [t['name'] for t in response.json()['result']['tools']]
        self.assertIn('odoo_search', names)
        self.assertNotIn('resultType', response.json()['result'])

    def test_legacy_unknown_version_falls_back(self):
        response = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'protocolVersion': '2024-11-05'}})
        self.assertEqual(response.json()['result']['protocolVersion'], '2025-11-25')

    def test_modern_discover(self):
        response = self._modern('server/discover')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertIn(MODERN, result['supportedVersions'])
        self.assertIn('io.modelcontextprotocol/serverInfo', result['_meta'])
        self.assertIn('ttlMs', result)

    def test_modern_tools_list(self):
        result = self._modern('tools/list').json()['result']
        self.assertEqual(result['cacheScope'], 'private')
        tool = next(t for t in result['tools'] if t['name'] == 'odoo_search')
        self.assertTrue(tool['annotations']['readOnlyHint'])

    def test_unsupported_version(self):
        response = self._post(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
             'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '1900-01-01'}}},
            headers={'MCP-Protocol-Version': '1900-01-01', 'Mcp-Method': 'tools/list'})
        self.assertEqual(response.status_code, 400)
        error = response.json()['error']
        self.assertEqual(error['code'], -32022)
        self.assertIn(MODERN, error['data']['supported'])

    def test_header_mismatch(self):
        response = self._modern('tools/list', extra_headers={'Mcp-Method': 'tools/call'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['code'], -32020)
        response = self._modern('tools/call', {'name': 'odoo_context', 'arguments': {}},
                                name='odoo_search')
        self.assertEqual(response.json()['error']['code'], -32020)

    def test_base64_name_header(self):
        response = self._modern('tools/call', {'name': 'odoo_context', 'arguments': {}},
                                name='=?base64?b2Rvb19jb250ZXh0?=')
        self.assertEqual(response.status_code, 200, response.text)

    def test_unknown_method(self):
        response = self._modern('nothing/here')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], -32601)

    def test_get_not_allowed(self):
        response = self.url_open('/mcp', headers={'X-Odoo-Database': self.env.cr.dbname,
                                                  'Authorization': 'Bearer %s' % self.key})
        self.assertEqual(response.status_code, 405)

    def test_authentication(self):
        response = self._modern('tools/list', key=False)
        self.assertEqual(response.status_code, 401)
        self.assertIn('Bearer', response.headers.get('WWW-Authenticate', ''))
        response = self._modern('tools/list', key='not-a-real-key')
        self.assertEqual(response.status_code, 401)
        response = self._modern('tools/list', key=self.outsider_key)
        self.assertEqual(response.status_code, 403)

    def test_rpc_scope_key(self):
        rpc_key = self.env['res.users.apikeys'].with_user(self.user)._generate(
            'rpc', 'rpc key', fields.Datetime.now() + timedelta(days=1))
        response = self._modern('tools/list', key=rpc_key)
        self.assertEqual(response.status_code, 200, response.text)

    def test_origin_check(self):
        response = self._modern('tools/list', extra_headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    # tools -------------------------------------------------------------

    def test_context(self):
        result = self._call('odoo_context', {})
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['user']['login'], 'mcp_user')

    def test_list_and_describe(self):
        models = self._call('odoo_list_models', {})['structuredContent']['models']
        self.assertIn('res.partner', [m['model'] for m in models])
        described = self._call('odoo_describe_model', {'model': 'res.partner'})['structuredContent']
        self.assertIn('email', described['fields'])
        self.assertNotIn('signature', described['fields'])

    def test_search_read_count_aggregate(self):
        found = self._call('odoo_search', {
            'model': 'res.partner', 'domain': [['name', '=', 'MCP Test Customer']],
            'fields': ['name', 'email', 'country_id']})
        self.assertFalse(found['isError'], found)
        data = found['structuredContent']
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['records'][0]['email'], 'mcp@example.com')
        read = self._call('odoo_read', {'model': 'res.partner', 'ids': [self.partner.id, 999999999]})
        self.assertEqual(read['structuredContent']['missing_ids'], [999999999])
        count = self._call('odoo_count', {'model': 'res.partner',
                                          'domain': [['name', 'ilike', 'MCP Test Customer']]})
        self.assertEqual(count['structuredContent']['count'], 1)
        agg = self._call('odoo_aggregate', {'model': 'res.partner', 'groupby': ['is_company'],
                                            'measures': ['__count'], 'order': '__count desc'})
        self.assertFalse(agg['isError'], agg)
        rows = agg['structuredContent']['rows']
        self.assertTrue(rows)
        self.assertEqual(rows, sorted(rows, key=lambda r: -r['__count']))
        bad = self._call('odoo_aggregate', {'model': 'res.partner', 'groupby': ['is_company'],
                                            'order': 'name; select 1'})
        self.assertTrue(bad['isError'])

    def test_restrictions(self):
        result = self._call('odoo_search', {'model': 'res.users'})
        self.assertTrue(result['isError'])
        result = self._call('odoo_search', {'model': 'ir.config_parameter'})
        self.assertTrue(result['isError'])
        result = self._call('odoo_search', {'model': 'res.partner',
                                            'domain': [['user_ids.password', '!=', False]]})
        self.assertTrue(result['isError'])
        result = self._call('odoo_search', {'model': 'res.partner', 'fields': ['signature']})
        self.assertTrue(result['isError'])
        result = self._call('odoo_search', {'model': 'res.partner', 'order': 'name; drop table'})
        self.assertTrue(result['isError'])
        result = self._call('odoo_search', {'model': 'res.partner', 'domain': 'not a list'})
        self.assertTrue(result['isError'])

    def test_unknown_tool(self):
        response = self._modern('tools/call', {'name': 'odoo_drop_everything', 'arguments': {}},
                                name='odoo_drop_everything')
        self.assertEqual(response.json()['error']['code'], -32602)

    def test_calls_are_logged(self):
        self._call('odoo_count', {'model': 'res.partner'})
        log = self.env['calderkit.mcp.log'].search([('user_id', '=', self.user.id)], limit=1)
        self.assertEqual(log.tool, 'odoo_count')
        self.assertEqual(log.status, 'ok')
        self.assertIn('test-client', log.client)

    def test_blocked_model_configuration(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.env['calderkit.mcp.model'].create(
                {'model_id': self.env['ir.model']._get('res.users').id})
