import json

from odoo import _, api, fields, models

from .mcp_server import icp_get


class CalderkitMcpSetup(models.TransientModel):
    _name = 'calderkit.mcp.setup'
    _description = 'Connect an AI assistant'

    endpoint_url = fields.Char(string='Server URL', compute='_compute_snippets')
    database = fields.Char(compute='_compute_snippets')
    snippet_claude_code = fields.Text(string='Claude Code', compute='_compute_snippets')
    snippet_json = fields.Text(string='JSON configuration', compute='_compute_snippets')
    model_count = fields.Integer(string='Models available', compute='_compute_snippets')

    @api.depends_context('uid')
    def _compute_snippets(self):
        base = (icp_get(self.env, 'web.base.url', '') or '').rstrip('/')
        url = base + '/mcp'
        config = {'mcpServers': {'odoo': {
            'type': 'http',
            'url': url,
            'headers': {'Authorization': 'Bearer YOUR_API_KEY'},
        }}}
        count = len(self.env['calderkit.mcp.model']._mcp_allowed_map())
        for rec in self:
            rec.endpoint_url = url
            rec.database = self.env.cr.dbname
            rec.snippet_claude_code = (
                'claude mcp add --transport http odoo %s '
                '--header "Authorization: Bearer YOUR_API_KEY"' % url)
            rec.snippet_json = json.dumps(config, indent=2)
            rec.model_count = count

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = _('Connect an AI assistant')

    @api.model
    def action_open_setup(self):
        setup = self.create({})
        return {
            'type': 'ir.actions.act_window',
            'name': _('Connect'),
            'res_model': self._name,
            'res_id': setup.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_open_models(self):
        return self.env['ir.actions.act_window']._for_xml_id('calderkit_mcp.action_mcp_model')
