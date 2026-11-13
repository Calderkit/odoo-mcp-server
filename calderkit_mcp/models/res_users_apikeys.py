from odoo import fields, models


class ResUsersApikeysDescription(models.TransientModel):
    _inherit = 'res.users.apikeys.description'

    scope = fields.Selection(
        selection_add=[('mcp', 'MCP (AI assistants only)')],
        ondelete={'mcp': 'set default'})
