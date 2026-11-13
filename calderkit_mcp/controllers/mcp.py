import base64
import binascii
import json
import logging
import re
from urllib.parse import urlparse

from odoo import http
from odoo.http import request

from ..models.mcp_server import (
    HEADER_MISMATCH, INTERNAL_ERROR, INVALID_REQUEST, LEGACY_VERSIONS, META_CLIENT, META_VERSION,
    PARSE_ERROR, SUPPORTED_VERSIONS, UNSUPPORTED_VERSION, McpProtocolError, icp_get,
)

_logger = logging.getLogger(__name__)
BEARER_RE = re.compile(r'^\s*bearer\s+(\S+)\s*$', re.I)
B64_RE = re.compile(r'^=\?base64\?(.*)\?=$')
NAMED_METHODS = ('tools/call', 'resources/read', 'prompts/get')


def _decode_header(value):
    match = B64_RE.match(value or '')
    if not match:
        return value
    try:
        return base64.b64decode(match.group(1)).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        return None


class CalderkitMcpController(http.Controller):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mcp_json(self, payload, status=200, headers=None):
        body = '' if payload is None else json.dumps(payload, ensure_ascii=False, default=str)
        all_headers = [('Content-Type', 'application/json'), ('Cache-Control', 'no-store')]
        all_headers += headers or []
        return request.make_response(body, headers=all_headers, status=status)

    def _mcp_error(self, msg_id, code, message, status=200, data=None, headers=None):
        error = {'code': code, 'message': message}
        if data is not None:
            error['data'] = data
        payload = {'jsonrpc': '2.0', 'error': error}
        if msg_id is not None:
            payload['id'] = msg_id
        return self._mcp_json(payload, status=status, headers=headers)

    def _mcp_base_url(self):
        return request.httprequest.host_url.rstrip('/')

    def _mcp_origin_allowed(self):
        origin = request.httprequest.headers.get('Origin')
        if not origin:
            return True
        if urlparse(origin).netloc == request.httprequest.host:
            return True
        allowed = icp_get(request.env, 'calderkit_mcp.allowed_origins', '') or ''
        return origin.rstrip('/') in {o.strip().rstrip('/') for o in allowed.split(',') if o.strip()}

    def _mcp_unauthorized(self, msg_id, message):
        server = request.env['calderkit.mcp.server'].sudo()
        header = server._mcp_www_authenticate(self._mcp_base_url())
        return self._mcp_error(msg_id, -32001, message, status=401,
                               headers=[('WWW-Authenticate', header)])

    def _mcp_client_label(self, params, auth_label):
        meta = params.get('_meta') if isinstance(params, dict) else None
        info = (meta or {}).get(META_CLIENT) or params.get('clientInfo') if isinstance(params, dict) else None
        if isinstance(info, dict) and info.get('name'):
            label = '%s %s' % (info.get('name'), info.get('version') or '')
        else:
            label = request.httprequest.headers.get('User-Agent') or ''
        label = label.strip()[:80]
        return ('%s (%s)' % (label, auth_label)) if auth_label and label else (label or auth_label or '')

    def _mcp_era(self, message):
        """Return (era, version) or raise McpProtocolError for header problems."""
        headers = request.httprequest.headers
        method = message.get('method')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        meta_version = (params.get('_meta') or {}).get(META_VERSION) \
            if isinstance(params.get('_meta'), dict) else None
        header_version = headers.get('MCP-Protocol-Version')
        if meta_version:
            if header_version != meta_version:
                raise McpProtocolError(
                    HEADER_MISMATCH, 'Header mismatch: MCP-Protocol-Version header %r does not match '
                    'body value %r' % (header_version, meta_version), http_status=400)
            if meta_version not in SUPPORTED_VERSIONS:
                raise McpProtocolError(
                    UNSUPPORTED_VERSION, 'Unsupported protocol version', http_status=400,
                    data={'supported': list(SUPPORTED_VERSIONS), 'requested': meta_version})
            if headers.get('Mcp-Method') != method:
                raise McpProtocolError(
                    HEADER_MISMATCH, 'Header mismatch: Mcp-Method header %r does not match body '
                    'value %r' % (headers.get('Mcp-Method'), method), http_status=400)
            if method in NAMED_METHODS:
                body_name = params.get('name') if method != 'resources/read' else params.get('uri')
                header_name = _decode_header(headers.get('Mcp-Name'))
                if header_name != body_name:
                    raise McpProtocolError(
                        HEADER_MISMATCH, 'Header mismatch: Mcp-Name header does not match body '
                        'value %r' % body_name, http_status=400)
            return 'modern', meta_version
        if method == 'initialize':
            return 'legacy', params.get('protocolVersion')
        if header_version and header_version not in LEGACY_VERSIONS:
            if header_version in SUPPORTED_VERSIONS:
                raise McpProtocolError(
                    HEADER_MISMATCH, 'Requests for protocol version %s must carry %s in params._meta'
                    % (header_version, META_VERSION), http_status=400)
            raise McpProtocolError(
                UNSUPPORTED_VERSION, 'Unsupported protocol version', http_status=400,
                data={'supported': list(SUPPORTED_VERSIONS), 'requested': header_version})
        return 'legacy', header_version or '2025-03-26'

    # ------------------------------------------------------------------
    # Endpoint
    # ------------------------------------------------------------------

    @http.route('/mcp', type='http', auth='none', methods=['POST', 'GET', 'DELETE'],
                csrf=False, save_session=False, readonly=False)
    def mcp_endpoint(self, **kwargs):
        httpreq = request.httprequest
        if not self._mcp_origin_allowed():
            return self._mcp_error(None, INVALID_REQUEST, 'Origin not allowed', status=403)
        if httpreq.method != 'POST':
            return self._mcp_json(None, status=405, headers=[('Allow', 'POST')])
        try:
            message = json.loads(httpreq.get_data(as_text=True) or 'null')
        except ValueError:
            return self._mcp_error(None, PARSE_ERROR, 'Parse error', status=400)
        if isinstance(message, list):  # JSON-RPC batch (2025-03-26 only)
            responses = [self._mcp_dispatch(item) for item in message]
            bodies = [json.loads(r.get_data(as_text=True)) for r in responses
                      if r.status_code != 202 and r.get_data()]
            return self._mcp_json(bodies) if bodies else self._mcp_json(None, status=202)
        return self._mcp_dispatch(message)

    def _mcp_dispatch(self, message):
        httpreq = request.httprequest
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' \
                or not isinstance(message.get('method'), str):
            if isinstance(message, dict) and ('result' in message or 'error' in message):
                return self._mcp_json(None, status=202)  # client responses: nothing to do
            return self._mcp_error(None, INVALID_REQUEST, 'Invalid Request', status=400)
        msg_id = message.get('id')
        is_notification = 'id' not in message

        token_match = BEARER_RE.match(httpreq.headers.get('Authorization') or '')
        if not token_match:
            return self._mcp_unauthorized(msg_id, 'Authentication required')
        server = request.env['calderkit.mcp.server'].sudo()
        uid, auth_label = server._mcp_authenticate(token_match.group(1))
        if not uid:
            return self._mcp_unauthorized(msg_id, 'Invalid or expired token')
        user = request.env['res.users'].sudo().browse(uid)
        if not user.active or not user.has_group('calderkit_mcp.group_mcp_user'):
            return self._mcp_error(msg_id, -32001, 'This user is not allowed to use the MCP server. '
                                   'Ask an administrator to grant the "MCP Server / User" role.',
                                   status=403)

        if is_notification:
            return self._mcp_json(None, status=202)

        try:
            era, _version = self._mcp_era(message)
        except McpProtocolError as e:
            return self._mcp_error(msg_id, e.code, e.message, status=e.http_status, data=e.data)

        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        client = self._mcp_client_label(params, auth_label)
        context = dict(request.env.context, lang=user.lang or 'en_US', tz=user.tz or 'UTC',
                       allowed_company_ids=user.company_ids.ids,
                       calderkit_audit_origin='mcp')
        request.update_env(user=uid, context=context)
        try:
            result = request.env['calderkit.mcp.server']._mcp_handle(message, era, client)
        except McpProtocolError as e:
            return self._mcp_error(msg_id, e.code, e.message, status=e.http_status, data=e.data)
        except Exception:
            _logger.exception('MCP request failed')
            return self._mcp_error(msg_id, INTERNAL_ERROR, 'Internal error')
        return self._mcp_json({'jsonrpc': '2.0', 'id': msg_id, 'result': result})
