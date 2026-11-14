import json
import logging
import re
import time

from odoo import api, fields, models, release
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

MODERN_VERSIONS = ('2026-07-28',)
LEGACY_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
META_VERSION = 'io.modelcontextprotocol/protocolVersion'
META_CLIENT = 'io.modelcontextprotocol/clientInfo'
META_SERVER = 'io.modelcontextprotocol/serverInfo'

# JSON-RPC / MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

SENSITIVE_FIELD_RE = re.compile(
    r'(passw|secret|token|api_?key|apikey|private_?key|totp|otp_secret|credential|'
    r'oauth|access_key|_hash$|signature)', re.I)
DOMAIN_OPERATORS = frozenset({
    '=', '!=', '<=', '<', '>', '>=', '=?', '=like', '=ilike', 'like', 'not like',
    'ilike', 'not ilike', 'in', 'not in', 'child_of', 'parent_of', 'any', 'not any',
})
ORDER_RE = re.compile(
    r'^\s*[a-z_][a-z0-9_]*(\s+(asc|desc))?(\s*,\s*[a-z_][a-z0-9_]*(\s+(asc|desc))?)*\s*$', re.I)
GROUPBY_RE = re.compile(r'^([a-z_][a-z0-9_]*)(:(day|week|month|quarter|year))?$')
AGGREGATE_RE = re.compile(r'^(__count|([a-z_][a-z0-9_]*):(sum|avg|min|max|count|count_distinct))$')
MAX_TEXT = 4000
MAX_IDS = 50
INSTRUCTIONS = (
    "This server gives access to an Odoo database as the signed-in user, with that user's "
    "access rights. Start with odoo_context (current user, company and date) and "
    "odoo_list_models. Call odoo_describe_model before filtering a model you have not used yet. "
    "Domains use Odoo syntax: a list of [field, operator, value] conditions, all combined with AND "
    "unless you add '|' (OR) or '!' (NOT) prefixes, e.g. "
    "[[\"state\", \"=\", \"sale\"], [\"date_order\", \">=\", \"2026-01-01\"]]. "
    "Dates are 'YYYY-MM-DD'; datetimes are UTC 'YYYY-MM-DD HH:MM:SS'. "
    "Use odoo_aggregate for totals and counts per group instead of reading many records. "
    "Results are paged: use offset when has_more is true."
)


def icp_get(env, key, default=''):
    """Read a system parameter as text (Odoo 20 get_str, Odoo 18-19 get_param)."""
    icp = env['ir.config_parameter'].sudo()
    if hasattr(icp, 'get_str'):
        return icp.get_str(key, default)
    return icp.get_param(key, default)


class McpProtocolError(Exception):
    """A JSON-RPC level error (not a tool execution error)."""

    def __init__(self, code, message, http_status=200, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data


class McpToolError(Exception):
    """A tool execution error, reported to the model with isError=true."""


class CalderkitMcpServer(models.AbstractModel):
    _name = 'calderkit.mcp.server'
    _description = 'MCP Server'

    # ------------------------------------------------------------------
    # Extension points (overridden by calderkit_mcp_pro)
    # ------------------------------------------------------------------

    @api.model
    def _mcp_authenticate(self, token):
        """Return (uid, client_label) for a bearer token, or (None, None).

        Accepts API keys with the dedicated 'mcp' scope and regular 'rpc' keys.
        Odoo 18-19 also match keys without scope.
        """
        keys = self.env['res.users.apikeys'].sudo()
        for scope in ('mcp', 'rpc'):
            uid = keys._check_credentials(scope=scope, key=token)
            if uid:
                return uid, 'API key'
        return None, None

    @api.model
    def _mcp_www_authenticate(self, base_url):
        return 'Bearer realm="Odoo MCP"'

    @api.model
    def _mcp_before_tool(self, name, arguments):
        """Hook called before every tool call (quotas, policies)."""
        return True

    @api.model
    def _mcp_server_info(self):
        module = self.env['ir.module.module'].sudo().search(
            [('name', '=', 'calderkit_mcp')], limit=1)
        return {
            'name': 'odoo-mcp-calderkit',
            'title': 'Odoo MCP Server',
            'version': module.latest_version or release.version,
        }

    @api.model
    def _mcp_capabilities(self):
        capabilities = {'tools': {'listChanged': False}}
        if self._mcp_resources():
            capabilities['resources'] = {'listChanged': False}
        return capabilities

    @api.model
    def _mcp_resources(self):
        """{uri: (definition, method name)} of the available resources."""
        return {}

    @api.model
    def _mcp_tools(self):
        """{name: (definition, method name)} of the available tools."""
        readonly = {'readOnlyHint': True, 'destructiveHint': False,
                    'idempotentHint': True, 'openWorldHint': False}
        model_prop = {
            'type': 'string',
            'description': "Technical model name from odoo_list_models, e.g. 'res.partner' or "
                           "'sale.order'.",
        }
        domain_prop = {
            'type': 'array', 'default': [],
            'description': "Odoo domain: a list of [field, operator, value] conditions, combined with "
                           "AND unless prefixed by '|' (OR) or '!' (NOT). A dotted path follows a "
                           "relation to another available model, e.g. 'country_id.code'. Omit or [] "
                           "to match every record you can see; archived records are left out unless "
                           "the domain has a condition on active. Example: [[\"customer_rank\", \">\", 0], "
                           "[\"country_id.code\", \"=\", \"BE\"]].",
        }
        fields_prop = {
            'type': 'array', 'items': {'type': 'string'},
            'description': "Field names to return, from odoo_describe_model. Omit for a default "
                           "selection.",
        }
        return {
            'odoo_context': ({
                'name': 'odoo_context',
                'title': 'Current user and date',
                'description': "Return the session context: who you are connected as (user id, name, "
                               "login), the current company and its currency, the companies you can "
                               "access, language, time zone, today's date in that time zone "
                               "('YYYY-MM-DD'), the current UTC date and time ('YYYY-MM-DD HH:MM:SS') and "
                               "the Odoo version. Use it to resolve relative dates such as 'last "
                               "quarter', the current user or company, or the current company's currency. It "
                               "reads no business records. No arguments.",
                'inputSchema': {'type': 'object', 'additionalProperties': False},
                'annotations': readonly,
            }, '_tool_context'),
            'odoo_list_models': ({
                'name': 'odoo_list_models',
                'title': 'List available models',
                'description': "List the Odoo models (tables) you may query, all in one response: "
                               "technical name, label, the administrator's hint about what each one "
                               "holds, and can_write (whether changes are allowed on it). Only models "
                               "enabled by the administrator that you can read are included. Use it to "
                               "find a model name you do not know yet; use odoo_describe_model for a "
                               "model's fields and odoo_search for its records.",
                'inputSchema': {
                    'type': 'object',
                    'properties': {'search': {
                        'type': 'string',
                        'description': "Case-insensitive text matched against the technical name, "
                                       "label and hint, e.g. 'invoice'. Omit to list every model."}},
                    'additionalProperties': False,
                },
                'annotations': readonly,
            }, '_tool_list_models'),
            'odoo_describe_model': ({
                'name': 'odoo_describe_model',
                'title': 'Describe a model',
                'description': "Describe the fields of one model: for each field its type, label, "
                               "related model (and whether that model is available too), selection "
                               "values, help text, and required, readonly or not-stored flags. Also "
                               "returns the model's hint, default_fields and max_records_per_call. Use "
                               "it before filtering, sorting, grouping or choosing fields on a model you "
                               "have not used yet; it returns no records (use odoo_search for data). "
                               "Passwords, tokens, keys, files and fields hidden by the administrator "
                               "are never listed and cannot be used.",
                'inputSchema': {
                    'type': 'object',
                    'properties': {'model': model_prop},
                    'required': ['model'],
                    'additionalProperties': False,
                },
                'annotations': readonly,
            }, '_tool_describe_model'),
            'odoo_search': ({
                'name': 'odoo_search',
                'title': 'Search records',
                'description': "Find the records of one model that match a domain and return one page "
                               "of them with the requested fields, plus total (all matches), offset and "
                               "has_more; page with offset while has_more is true. Use it when you need "
                               "the records themselves. For known ids use odoo_read, for a number only "
                               "odoo_count, for totals per group odoo_aggregate. You only see records "
                               "your access rights allow. Many2one values come as {id, name}, one2many "
                               "and many2many as {count, ids} (first 50 ids), long texts are cut at "
                               "4,000 characters.",
                'inputSchema': {
                    'type': 'object',
                    'properties': {
                        'model': model_prop,
                        'domain': domain_prop,
                        'fields': fields_prop,
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 20,
                                  'description': "Records per page. Values above the model's "
                                                 "max_records_per_call (usually 200) are reduced to it."},
                        'offset': {'type': 'integer', 'minimum': 0, 'default': 0,
                                   'description': 'Number of matching records to skip, for paging.'},
                        'order': {'type': 'string',
                                  'description': "Sort order on fields of this model, e.g. "
                                                 "'date_order desc, id desc'. Default: the model's own "
                                                 "order."},
                    },
                    'required': ['model'],
                    'additionalProperties': False,
                },
                'annotations': readonly,
            }, '_tool_search'),
            'odoo_read': ({
                'name': 'odoo_read',
                'title': 'Read records',
                'description': "Read specific records of one model by id, at most 100 per call, and "
                               "return their fields as {model, count, records}. Use it when you already "
                               "have ids (from odoo_search, a relation field or the user); to find "
                               "records by criteria use odoo_search, for a number only odoo_count. Ids "
                               "that do not exist are listed in missing_ids instead of failing; if one "
                               "of the records is outside your access rights, the call fails with an "
                               "access error. Values are formatted as in odoo_search.",
                'inputSchema': {
                    'type': 'object',
                    'properties': {
                        'model': model_prop,
                        'ids': {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 100,
                                'description': 'Record ids to read, e.g. [7, 12]. At most 100.'},
                        'fields': fields_prop,
                    },
                    'required': ['model', 'ids'],
                    'additionalProperties': False,
                },
                'annotations': readonly,
            }, '_tool_read'),
            'odoo_count': ({
                'name': 'odoo_count',
                'title': 'Count records',
                'description': "Count the records of one model that match a domain and return {model, "
                               "count}. Use it when you need only the number: it reads no records, so it "
                               "is cheaper than odoo_search. Without a domain it counts every record you "
                               "can see, archived ones excluded. For counts per group use odoo_aggregate with '__count'; to get "
                               "the records use odoo_search.",
                'inputSchema': {
                    'type': 'object',
                    'properties': {'model': model_prop, 'domain': domain_prop},
                    'required': ['model'],
                    'additionalProperties': False,
                },
                'annotations': readonly,
            }, '_tool_count'),
            'odoo_aggregate': ({
                'name': 'odoo_aggregate',
                'title': 'Totals by group',
                'description': "Group the records of one model that match a domain and compute totals "
                               "per group, e.g. sales per customer per month, and return {model, "
                               "groupby, measures, count, rows} with one key per groupby item and "
                               "measure in each row; many2one groups come as {id, name}. Without groupby "
                               "it returns one row for all matching records. Use it for sums, averages "
                               "or counts per group instead of reading many records; use odoo_count for "
                               "a single number and odoo_search for the records themselves.",
                'inputSchema': {
                    'type': 'object',
                    'properties': {
                        'model': model_prop,
                        'domain': domain_prop,
                        'groupby': {'type': 'array', 'items': {'type': 'string'}, 'default': [],
                                    'description': "Fields to group by. Date and datetime fields need "
                                                   ":day, :week, :month, :quarter or :year, e.g. "
                                                   "['partner_id', 'date_order:month']."},
                        'measures': {'type': 'array', 'items': {'type': 'string'},
                                     'default': ['__count'],
                                     'description': "'__count' for the number of records, or "
                                                    "'field:function' with sum, avg, min, max, count or "
                                                    "count_distinct, e.g. 'amount_total:sum'."},
                        'order': {'type': 'string',
                                  'description': "Sort groups by a groupby item or measure given above, "
                                                 "e.g. 'amount_total:sum desc' or '__count desc'. "
                                                 "Default: by group."},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 100,
                                  'description': "Maximum number of groups. Values above the model's "
                                                 "max_records_per_call (usually 200) are reduced to it."},
                    },
                    'required': ['model'],
                    'additionalProperties': False,
                },
                'annotations': readonly,
            }, '_tool_aggregate'),
        }

    # ------------------------------------------------------------------
    # JSON-RPC dispatch (env is the authenticated user)
    # ------------------------------------------------------------------

    @api.model
    def _mcp_handle(self, message, era, client):
        method = message.get('method')
        params = message.get('params') or {}
        if not isinstance(params, dict):
            raise McpProtocolError(INVALID_PARAMS, 'params must be an object')
        handlers = {
            'initialize': self._mcp_initialize,
            'server/discover': self._mcp_discover,
            'ping': lambda params, era, client: {},
            'tools/list': self._mcp_tools_list,
            'tools/call': self._mcp_tools_call,
            'resources/list': self._mcp_resources_list,
            'resources/templates/list': lambda params, era, client: self._mcp_cacheable(
                {'resourceTemplates': []}, era),
            'prompts/list': lambda params, era, client: self._mcp_cacheable({'prompts': []}, era),
        }
        if self._mcp_resources():
            handlers['resources/read'] = self._mcp_resources_read
        handler = handlers.get(method)
        if handler is None:
            raise McpProtocolError(METHOD_NOT_FOUND, 'Method not found: %s' % method,
                                   http_status=404 if era == 'modern' else 200)
        result = handler(params, era, client)
        if era == 'modern':
            result['resultType'] = 'complete'
            result.setdefault('_meta', {})[META_SERVER] = self._mcp_server_info()
        return result

    def _mcp_cacheable(self, result, era):
        if era == 'modern':
            result.update({'ttlMs': 300000, 'cacheScope': 'private'})
        return result

    def _mcp_initialize(self, params, era, client):
        requested = params.get('protocolVersion')
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {
            'protocolVersion': version,
            'capabilities': self._mcp_capabilities(),
            'serverInfo': self._mcp_server_info(),
            'instructions': INSTRUCTIONS,
        }

    def _mcp_discover(self, params, era, client):
        return self._mcp_cacheable({
            'supportedVersions': list(SUPPORTED_VERSIONS),
            'capabilities': self._mcp_capabilities(),
            'instructions': INSTRUCTIONS,
        }, era)

    def _mcp_tools_list(self, params, era, client):
        tools = [definition for definition, _method in self._mcp_tools().values()]
        return self._mcp_cacheable({'tools': tools}, era)

    def _mcp_resources_list(self, params, era, client):
        resources = [definition for definition, _method in self._mcp_resources().values()]
        return self._mcp_cacheable({'resources': resources}, era)

    def _mcp_resources_read(self, params, era, client):
        uri = params.get('uri')
        if not isinstance(uri, str):
            raise McpProtocolError(INVALID_PARAMS, 'uri must be a string')
        resources = self._mcp_resources()
        if uri not in resources:
            code = INVALID_PARAMS if era == 'modern' else -32002
            raise McpProtocolError(code, 'Unknown resource: %s' % uri)
        result = getattr(self, resources[uri][1])(uri)
        return self._mcp_cacheable(result, era)

    def _mcp_tools_call(self, params, era, client):
        name = params.get('name')
        arguments = params.get('arguments') or {}
        tools = self._mcp_tools()
        if name not in tools:
            raise McpProtocolError(INVALID_PARAMS, 'Unknown tool: %s' % name)
        if not isinstance(arguments, dict):
            raise McpProtocolError(INVALID_PARAMS, 'arguments must be an object')
        method = getattr(self.with_context(calderkit_mcp_client=client), tools[name][1])
        started = time.monotonic()
        error = None
        payload = None
        try:
            with self.env.cr.savepoint():
                self._mcp_before_tool(name, arguments)
                payload = method(arguments)
        except McpToolError as e:
            error = str(e)
        except (AccessError, UserError, ValidationError, MissingError) as e:
            error = getattr(e, 'args', [str(e)])[0] if e.args else str(e)
        except (ValueError, TypeError, KeyError) as e:
            error = 'Invalid arguments: %s' % e
        except Exception:  # never leak a traceback to the client
            _logger.exception('MCP tool %s failed', name)
            error = 'Internal error while running %s. The administrator can find details in the server log.' % name
        duration = int((time.monotonic() - started) * 1000)
        self._mcp_log_call(client, name, arguments, payload, error, duration)
        if error:
            return {'content': [{'type': 'text', 'text': error}], 'isError': True}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return {
            'content': [{'type': 'text', 'text': text}],
            'structuredContent': payload,
            'isError': False,
        }

    def _mcp_log_call(self, client, name, arguments, payload, error, duration):
        count = None
        if isinstance(payload, dict):
            if isinstance(payload.get('records'), list):
                count = len(payload['records'])
            elif isinstance(payload.get('rows'), list):
                count = len(payload['rows'])
            elif isinstance(payload.get('count'), int):
                count = payload['count']
        self.env['calderkit.mcp.log'].sudo().create({
            'user_id': self.env.uid,
            'client': (client or '')[:120],
            'tool': name,
            'model_name': (arguments.get('model') or '')[:120] if isinstance(arguments.get('model'), str) else False,
            'arguments': json.dumps(arguments, ensure_ascii=False, default=str)[:2000],
            'result_count': count,
            'duration_ms': duration,
            'status': 'error' if error else 'ok',
            'error': error and error[:2000],
        })

    # ------------------------------------------------------------------
    # Access helpers
    # ------------------------------------------------------------------

    def _mcp_allowed(self):
        return self.env['calderkit.mcp.model']._mcp_allowed_map()

    def _mcp_model(self, model_name, operation='read'):
        """The env model for an allowed model name, checking access rights."""
        if not isinstance(model_name, str) or not model_name:
            raise McpToolError('model is required, e.g. "res.partner". Use odoo_list_models.')
        allowed = self._mcp_allowed()
        if model_name not in allowed or model_name not in self.env:
            raise McpToolError(
                'Model %s is not available. Use odoo_list_models to see the models you can use.'
                % model_name)
        Model = self.env[model_name]
        if not Model.has_access(operation):
            raise McpToolError('You do not have %s access to %s.' % (operation, model_name))
        return Model

    def _mcp_fields(self, Model):
        """{field: description} of the fields exposed for a model."""
        excluded = self._mcp_allowed().get(Model._name, {}).get('excluded', frozenset())
        described = Model.fields_get(attributes=[
            'type', 'string', 'relation', 'required', 'readonly', 'selection', 'help', 'store',
        ])
        result = {}
        for name, desc in described.items():
            if name in excluded or SENSITIVE_FIELD_RE.search(name):
                continue
            if desc.get('type') == 'binary':
                continue
            if name.startswith(('message_', 'activity_', 'website_message')) and name != 'message_ids':
                continue
            if name == 'message_ids':
                continue
            result[name] = desc
        return result

    def _mcp_default_fields(self, Model, exposed):
        preferred = ['display_name']
        skip_types = ('one2many', 'many2many', 'html', 'text', 'properties', 'properties_definition', 'json')
        for name, desc in exposed.items():
            if len(preferred) >= 25:
                break
            if name in preferred or desc.get('type') in skip_types or not desc.get('store'):
                continue
            if name in ('create_uid', 'write_uid', 'write_date', 'company_ids'):
                continue
            preferred.append(name)
        return [f for f in preferred if f in exposed]

    def _mcp_check_fields(self, Model, names):
        exposed = self._mcp_fields(Model)
        if not names:
            return self._mcp_default_fields(Model, exposed)
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise McpToolError('fields must be a list of field names.')
        unknown = [n for n in names if n not in exposed]
        if unknown:
            raise McpToolError('Unknown or unavailable fields on %s: %s. Use odoo_describe_model.'
                               % (Model._name, ', '.join(unknown)))
        return names

    def _mcp_check_path(self, Model, path):
        allowed = self._mcp_allowed()
        current = Model
        segments = path.split('.')
        for index, segment in enumerate(segments):
            exposed = self._mcp_fields(current)
            if segment != 'id' and segment not in exposed:
                raise McpToolError('Field %s is not available on %s. Use odoo_describe_model.'
                                   % (segment, current._name))
            if index < len(segments) - 1:
                field = current._fields[segment]
                if not field.relational:
                    raise McpToolError('%s on %s is not a relation; it cannot be followed with a dot.'
                                       % (segment, current._name))
                if field.comodel_name not in allowed:
                    raise McpToolError('Filtering through %s is not available.' % field.comodel_name)
                current = self.env[field.comodel_name]
        return current

    def _mcp_check_domain(self, Model, domain):
        if domain in (None, ''):
            return []
        if not isinstance(domain, list):
            raise McpToolError('domain must be a list, e.g. [["name", "ilike", "acme"]].')
        for leaf in domain:
            if leaf in ('&', '|', '!'):
                continue
            if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
                raise McpToolError('Each domain condition must be [field, operator, value]; got %s.'
                                   % json.dumps(leaf, default=str))
            path, operator, value = leaf
            if not isinstance(path, str):
                raise McpToolError('Field names in a domain must be strings.')
            if operator not in DOMAIN_OPERATORS:
                raise McpToolError('Unsupported operator %r.' % operator)
            target = self._mcp_check_path(Model, path)
            if operator in ('any', 'not any'):
                last = path.split('.')[-1]
                comodel = target._fields[last].comodel_name if last in target._fields else None
                if not comodel or comodel not in self._mcp_allowed():
                    raise McpToolError('%s cannot be used with %s.' % (path, operator))
                self._mcp_check_domain(self.env[comodel], value)
        return [list(leaf) if isinstance(leaf, tuple) else leaf for leaf in domain]

    def _mcp_limit(self, Model, value, default=20):
        maximum = self._mcp_allowed().get(Model._name, {}).get('max', 200)
        try:
            value = int(value if value is not None else default)
        except (TypeError, ValueError):
            raise McpToolError('limit must be a number.')
        return max(1, min(value, maximum))

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def _mcp_value(self, field, value):
        ftype = field.get('type')
        if value is False and ftype != 'boolean':
            return None
        if ftype == 'many2one':
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return {'id': value[0], 'name': value[1]}
            return value
        if ftype in ('one2many', 'many2many'):
            ids = list(value or [])
            return {'count': len(ids), 'ids': ids[:MAX_IDS]}
        if ftype == 'datetime':
            return fields.Datetime.to_string(value)
        if ftype == 'date':
            return fields.Date.to_string(value)
        if ftype == 'html':
            return html2plaintext(value or '')[:MAX_TEXT]
        if isinstance(value, str) and len(value) > MAX_TEXT:
            return value[:MAX_TEXT] + '…'
        return value

    def _mcp_serialize(self, records, names):
        exposed = self._mcp_fields(records)
        rows = []
        for vals in records.read(names):
            row = {'id': vals['id']}
            for name in names:
                if name == 'id':
                    continue
                row[name] = self._mcp_value(exposed.get(name, {}), vals.get(name))
            rows.append(row)
        return rows

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _tool_context(self, args):
        user = self.env.user
        now = fields.Datetime.now()
        today_local = fields.Date.context_today(user)
        return {
            'user': {'id': user.id, 'name': user.name, 'login': user.login},
            'company': {'id': self.env.company.id, 'name': self.env.company.name,
                        'currency': self.env.company.currency_id.name},
            'companies': [{'id': c.id, 'name': c.name} for c in self.env.companies],
            'lang': user.lang,
            'tz': user.tz or 'UTC',
            'now_utc': fields.Datetime.to_string(now),
            'today': fields.Date.to_string(today_local),
            'odoo_version': release.version,
        }

    def _tool_list_models(self, args):
        search = (args.get('search') or '').lower()
        result = []
        for name, info in sorted(self._mcp_allowed().items()):
            if name not in self.env:
                continue
            Model = self.env[name]
            if not Model.has_access('read'):
                continue
            label = self.env['ir.model']._get(name).name
            if search and search not in name.lower() and search not in (label or '').lower() \
                    and search not in (info.get('hint') or '').lower():
                continue
            result.append({'model': name, 'name': label, 'hint': info.get('hint') or None,
                           'can_write': info.get('write_mode', 'off') != 'off'
                           and Model.has_access('write')})
        return {'models': result, 'count': len(result)}

    def _tool_describe_model(self, args):
        Model = self._mcp_model(args.get('model'))
        exposed = self._mcp_fields(Model)
        out = {}
        for name, desc in sorted(exposed.items()):
            item = {'type': desc.get('type'), 'label': desc.get('string')}
            if desc.get('relation'):
                item['relation'] = desc['relation']
                item['relation_available'] = desc['relation'] in self._mcp_allowed()
            if desc.get('selection'):
                item['selection'] = [list(option) for option in desc['selection']]
            if desc.get('required'):
                item['required'] = True
            if desc.get('readonly'):
                item['readonly'] = True
            if not desc.get('store'):
                item['stored'] = False
            if desc.get('help'):
                item['help'] = desc['help'][:300]
            out[name] = item
        info = self._mcp_allowed().get(Model._name, {})
        return {
            'model': Model._name,
            'name': self.env['ir.model']._get(Model._name).name,
            'hint': info.get('hint') or None,
            'max_records_per_call': info.get('max', 200),
            'default_fields': self._mcp_default_fields(Model, exposed),
            'fields': out,
        }

    def _tool_search(self, args):
        Model = self._mcp_model(args.get('model'))
        domain = self._mcp_check_domain(Model, args.get('domain'))
        names = self._mcp_check_fields(Model, args.get('fields'))
        limit = self._mcp_limit(Model, args.get('limit'))
        offset = max(0, int(args.get('offset') or 0))
        order = args.get('order') or None
        if order and not ORDER_RE.match(order):
            raise McpToolError("order must look like 'field desc, other_field asc'.")
        if order:
            for item in order.split(','):
                fname = item.strip().split()[0]
                if fname != 'id' and fname not in self._mcp_fields(Model):
                    raise McpToolError('Cannot sort on %s.' % fname)
        total = Model.search_count(domain)
        records = Model.search(domain, limit=limit, offset=offset, order=order)
        rows = self._mcp_serialize(records, names)
        return {
            'model': Model._name, 'total': total, 'offset': offset, 'count': len(rows),
            'has_more': offset + len(rows) < total, 'records': rows,
        }

    def _tool_read(self, args):
        Model = self._mcp_model(args.get('model'))
        ids = args.get('ids') or []
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise McpToolError('ids must be a list of integers.')
        if len(ids) > 100:
            raise McpToolError('Read at most 100 records at a time.')
        names = self._mcp_check_fields(Model, args.get('fields'))
        records = Model.browse(ids).exists()
        missing = sorted(set(ids) - set(records.ids))
        rows = self._mcp_serialize(records, names)
        result = {'model': Model._name, 'count': len(rows), 'records': rows}
        if missing:
            result['missing_ids'] = missing
        return result

    def _tool_count(self, args):
        Model = self._mcp_model(args.get('model'))
        domain = self._mcp_check_domain(Model, args.get('domain'))
        return {'model': Model._name, 'count': Model.search_count(domain)}

    def _tool_aggregate(self, args):
        Model = self._mcp_model(args.get('model'))
        domain = self._mcp_check_domain(Model, args.get('domain'))
        groupby = args.get('groupby') or []
        measures = args.get('measures') or ['__count']
        if not isinstance(groupby, list) or not isinstance(measures, list):
            raise McpToolError('groupby and measures must be lists.')
        exposed = self._mcp_fields(Model)
        for spec in groupby:
            match = GROUPBY_RE.match(spec or '')
            if not match or match.group(1) not in exposed:
                raise McpToolError("Invalid groupby %r. Use a field name, optionally with "
                                   ":day/:week/:month/:quarter/:year for dates." % spec)
        for spec in measures:
            match = AGGREGATE_RE.match(spec or '')
            if not match or (match.group(2) and match.group(2) not in exposed):
                raise McpToolError("Invalid measure %r. Use '__count' or 'field:sum' "
                                   "(sum, avg, min, max, count, count_distinct)." % spec)
        limit = self._mcp_limit(Model, args.get('limit'), default=100)
        order = args.get('order') or None
        if order:
            allowed_keys = set(groupby) | set(measures)
            for item in order.split(','):
                parts = item.strip().split()
                if not parts or len(parts) > 2 or parts[0] not in allowed_keys \
                        or (len(parts) == 2 and parts[1].lower() not in ('asc', 'desc')):
                    raise McpToolError("order must use the groupby or measures given, e.g. "
                                       "'amount_total:sum desc'.")
        groups = Model._read_group(domain, groupby, measures, limit=limit, order=order)
        rows = []
        for group in groups:
            row = {}
            for key, value in zip(list(groupby) + list(measures), group):
                row[key] = self._mcp_group_value(value)
            rows.append(row)
        return {'model': Model._name, 'groupby': groupby, 'measures': measures,
                'count': len(rows), 'rows': rows}

    def _mcp_group_value(self, value):
        if isinstance(value, models.BaseModel):
            if len(value) == 1:
                return {'id': value.id, 'name': value.display_name}
            return [{'id': r.id, 'name': r.display_name} for r in value] or None
        if value is False:
            return None
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return value
