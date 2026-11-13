import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .mcp_server import icp_get

_logger = logging.getLogger(__name__)
SEEN_PARAM = 'calderkit_mcp.defaults_seen'

try:  # Odoo 20+
    from odoo.api import ormcache
except ImportError:  # Odoo 18-19
    from odoo.tools import ormcache

# Models that are never exposed, whatever the configuration says.
BLOCKED_MODEL_PREFIXES = (
    'ir.', 'base.', 'bus.', 'res.users.apikeys', 'res.users.log', 'res.users.settings',
    'res.config', 'auth_', 'auth.', 'calderkit.mcp.', 'iap.', 'fetchmail.', 'mail.mail',
    'mail.message', 'mail.notification', 'payment.provider', 'payment.token',
    'base_import.', 'web_editor.', 'html_editor.', 'res.device',
)

# Exposed by default when the model is installed, with a hint for the AI.
DEFAULT_MODELS = {
    'res.partner': 'Customers, vendors and their contacts (is_company tells companies apart).',
    'res.partner.category': 'Contact tags.',
    'res.company': 'Companies of this database.',
    'res.currency': 'Currencies.',
    'res.country': 'Countries.',
    'product.template': 'Products as sold and purchased (one per product, variants share it).',
    'product.product': 'Product variants; order lines and stock refer to these.',
    'product.category': 'Product categories.',
    'uom.uom': 'Units of measure.',
    'sale.order': 'Quotations and sales orders (state: draft, sent, sale, cancel).',
    'sale.order.line': 'Lines of quotations and sales orders.',
    'purchase.order': 'Requests for quotation and purchase orders.',
    'purchase.order.line': 'Lines of purchase orders.',
    'account.move': 'Invoices, credit notes, vendor bills and journal entries '
                    '(move_type: out_invoice, out_refund, in_invoice, in_refund, entry).',
    'account.move.line': 'Lines of invoices, bills and journal entries.',
    'crm.lead': 'Leads and opportunities (type: lead or opportunity).',
    'crm.stage': 'Pipeline stages.',
    'project.project': 'Projects.',
    'project.task': 'Project tasks.',
    'stock.picking': 'Receipts, deliveries and internal transfers.',
    'stock.move': 'Stock moves (product quantities moved by transfers).',
    'stock.quant': 'Current stock on hand per product and location.',
    'stock.warehouse': 'Warehouses.',
    'stock.location': 'Stock locations.',
    'mrp.production': 'Manufacturing orders.',
    'calendar.event': 'Meetings and calendar events.',
}


class CalderkitMcpModel(models.Model):
    _name = 'calderkit.mcp.model'
    _description = 'MCP Model Access'
    _order = 'model_name'

    active = fields.Boolean(default=True)
    model_id = fields.Many2one(
        'ir.model', string='Model', required=True, ondelete='cascade',
        domain="[('transient', '=', False)]")
    model_name = fields.Char(
        string='Technical name', related='model_id.model', store=True, index=True)
    hint = fields.Char(
        string='Hint for the AI',
        help='A short description of what this model holds. It is shown to the AI assistant.')
    max_records = fields.Integer(
        string='Max records per call', default=200,
        help='Upper limit of records returned by one search.')
    excluded_field_ids = fields.Many2many(
        'ir.model.fields', string='Hidden fields',
        domain="[('model_id', '=', model_id)]",
        help='Fields never shown to the AI, in addition to passwords, tokens, keys and files.')

    @api.constrains('model_id')
    def _check_model(self):
        for rec in self:
            name = rec.model_id.model or ''
            if name.startswith(BLOCKED_MODEL_PREFIXES) or name in ('res.users', 'res.groups'):
                raise ValidationError(_('The model %s cannot be exposed to AI assistants.', name))
            if self.with_context(active_test=False).search_count([
                    ('model_id', '=', rec.model_id.id), ('id', '!=', rec.id)]):
                raise ValidationError(_('%s is already in the list.', name))

    @api.constrains('max_records')
    def _check_max_records(self):
        if any(rec.max_records < 1 or rec.max_records > 1000 for rec in self):
            raise ValidationError(_('Max records per call must be between 1 and 1000.'))

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    @api.model
    @ormcache()
    def _mcp_allowed_map(self):
        """{model_name: settings} of active entries; cached, do not mutate."""
        result = {}
        for rec in self.sudo().search([]):
            result[rec.model_name] = rec._mcp_settings()
        return result

    def _mcp_settings(self):
        self.ensure_one()
        return {
            'hint': self.hint or '',
            'max': self.max_records,
            'excluded': frozenset(self.excluded_field_ids.mapped('name')),
        }

    def _mcp_invalidate(self):
        registry = self.env.registry
        if hasattr(registry, 'clear_cache'):  # Odoo 18-19
            registry.clear_cache()
        else:  # Odoo 20+
            self.env.transaction.invalidate_ormcache('default')

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._mcp_invalidate()
        return records

    def write(self, vals):
        result = super().write(vals)
        self._mcp_invalidate()
        return result

    def unlink(self):
        result = super().unlink()
        self._mcp_invalidate()
        return result

    @api.model
    def _mcp_add_defaults(self, only_new=False):
        """Add the default models that are installed and not listed yet.

        With only_new, a model is proposed once: if an administrator removes
        it from the list later, it is not added back automatically.
        """
        icp = self.env['ir.config_parameter'].sudo()
        seen = set(filter(None, (icp_get(self.env, SEEN_PARAM, '') or '').split(',')))
        existing = set(self.with_context(active_test=False).search([]).mapped('model_name'))
        vals_list = []
        for name, hint in DEFAULT_MODELS.items():
            if name not in self.env or (only_new and name in seen):
                continue
            seen.add(name)
            if name in existing:
                continue
            model = self.env['ir.model']._get(name)
            if model:
                vals_list.append({'model_id': model.id, 'hint': hint})
        if vals_list:
            self.sudo().create(vals_list)
        value = ','.join(sorted(seen))
        if value != (icp_get(self.env, SEEN_PARAM, '') or ''):
            if hasattr(icp, 'set_str'):
                icp.set_str(SEEN_PARAM, value)
            else:
                icp.set_param(SEEN_PARAM, value)
        return True

    def _register_hook(self):
        """Propose the default models of apps installed after this one."""
        super()._register_hook()
        if self.env.registry.ready or 'calderkit.mcp.model' not in self.env.registry:
            return
        try:
            with self.env.cr.savepoint():
                # one worker at a time; the others skip
                self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', (7311426011,))
                if self.env.cr.fetchone()[0]:
                    self._mcp_add_defaults(only_new=True)
        except Exception:  # never block the server start
            _logger.warning('Could not update the MCP default models', exc_info=True)

    def action_add_defaults(self):
        self._mcp_add_defaults()
        return {'type': 'ir.actions.client', 'tag': 'reload'}
