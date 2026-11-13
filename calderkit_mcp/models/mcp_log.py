from datetime import timedelta

from odoo import api, fields, models

from .mcp_server import icp_get


class CalderkitMcpLog(models.Model):
    _name = 'calderkit.mcp.log'
    _description = 'MCP Call'
    _order = 'id desc'
    _rec_name = 'tool'

    create_date = fields.Datetime(string='Date', readonly=True, index=True)
    user_id = fields.Many2one('res.users', string='User', readonly=True, index=True,
                              ondelete='set null')
    client = fields.Char(readonly=True, help='The AI client or connection that made the call.')
    tool = fields.Char(readonly=True, index=True)
    model_name = fields.Char(string='Model', readonly=True)
    arguments = fields.Text(readonly=True)
    result_count = fields.Integer(string='Records', readonly=True)
    duration_ms = fields.Integer(string='Duration (ms)', readonly=True)
    status = fields.Selection([('ok', 'OK'), ('error', 'Error')], readonly=True, index=True)
    error = fields.Text(readonly=True)

    @api.model
    def _cron_purge(self):
        days = int(icp_get(self.env, 'calderkit_mcp.log_days', '90') or 90)
        if days > 0:
            limit = fields.Datetime.now() - timedelta(days=days)
            self.sudo().search([('create_date', '<', limit)], limit=50000).unlink()
