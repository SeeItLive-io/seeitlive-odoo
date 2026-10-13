"""HTTP client for the SeeItLive API.

An AbstractModel rather than helper functions, so it is reachable as
``self.env['seeitlive.api']`` from anywhere (models, controllers, tests) and
can be mocked or overridden the way Odoo expects.

Secrets live in ir.config_parameter and are read with sudo(). Nothing here is
ever exposed to the browser: the OWL panel calls Odoo, Odoo calls SeeItLive.
"""
import hashlib
import hmac
import json
import logging
import time

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The API host is fixed. There is one SeeItLive service, so letting an
# instance point elsewhere only ever produced misconfigured installs and a
# settings field customers had to be told to leave empty. Nothing reads
# ir.config_parameter for it any more; the retired `seeitlive.base_url` row
# is dropped by migrations/17.0.1.1.0/post-migrate.py.
API_BASE_URL = 'https://api.seeitlive.io/v1'
REQUEST_TIMEOUT = 15


class SeeItLiveApiError(UserError):
    """A ``UserError`` that also carries the HTTP status that raised it.

    Every existing ``except UserError`` still catches this (it IS one) — the
    ``.status`` attribute is additive, for callers that need to tell a
    permanent 4xx (e.g. a plan gate) apart from a transient failure without
    parsing the message text.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class SeeItLiveApi(models.AbstractModel):
    _name = 'seeitlive.api'
    _description = 'SeeItLive API Client'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @api.model
    def _config(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(
            'seeitlive.%s' % key, default=default,
        )

    @api.model
    def _base_url(self):
        """The one and only API host.

        Still a method rather than an inlined constant: the error copy below
        and the tests both ask for it, and a single seam keeps them honest.
        """
        return API_BASE_URL

    @api.model
    def _api_key(self):
        key = self._config('api_key')
        if not key:
            raise UserError(_(
                'No SeeItLive API key is configured. Open Settings > SeeItLive '
                'and paste a key (it starts with sil_live_ or sil_test_).'
            ))
        return key

    @api.model
    def _is_sandbox(self):
        return self._config('api_key').startswith('sil_test_')

    @api.model
    def _config_bool(self, key, default=True):
        """Read a Boolean setting. Odoo stores these as the strings
        'True'/'False', and an absent key must mean the default, not False.
        """
        raw = self._config(key, '')
        if raw == '':
            return default
        return raw not in ('False', 'false', '0')

    # ------------------------------------------------------------------
    # Request signing (opt-in, per API key)
    # ------------------------------------------------------------------

    @api.model
    def _signing_headers(self, method, path, body, timestamp=None):
        """HMAC headers for one request, or ``{}`` when signing is off.

        Signed string: ``<ts>.<METHOD>.<path>.<sha256hex(body)>`` where *path*
        is VERSION-RELATIVE (``/sessions``, never ``/api/v1/sessions``) and
        carries any query string. Mirrors
        ``apps/api/src/lib/api-key-signing.ts``; note this differs from
        apertur's plugin, which signs the full ``/api/v1/...`` path with
        ``X-Aptr-*`` headers.
        """
        secret = self._config('signing_secret')
        if not secret:
            return {}
        raw = body.encode('utf-8') if isinstance(body, str) else (body or b'')
        ts = str(int(time.time()) if timestamp is None else timestamp)
        base = '%s.%s.%s.%s' % (ts, method.upper(), path, hashlib.sha256(raw).hexdigest())
        signature = hmac.new(
            secret.encode('utf-8'), base.encode('utf-8'), hashlib.sha256,
        ).hexdigest()
        return {
            'X-SeeItLive-Timestamp': ts,
            'X-SeeItLive-Signature': 'sha256=%s' % signature,
        }

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @api.model
    def _send(self, method, path, payload=None):
        """Perform the HTTP call and return the raw ``requests`` response."""
        body = json.dumps(payload) if payload is not None else ''
        headers = {
            'Authorization': 'Bearer %s' % self._api_key(),
            'Accept': 'application/json',
        }
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        headers.update(self._signing_headers(method, path, body))
        try:
            return requests.request(
                method.upper(),
                '%s%s' % (self._base_url(), path),
                headers=headers,
                data=body if payload is not None else None,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            _logger.error('SeeItLive request failed: %s %s: %s', method, path, exc)
            raise UserError(_(
                'Could not reach SeeItLive at %(url)s.\n\n'
                'Check this server outbound network access — firewall, proxy '
                'or DNS.\n\n%(error)s'
            ) % {'url': self._base_url(), 'error': exc}) from exc

    @api.model
    def _request(self, method, path, payload=None, raw=False):
        """Call the API and return parsed JSON, or ``bytes`` when *raw*."""
        resp = self._send(method, path, payload)
        if resp.status_code >= 400:
            self._raise_api_error(resp, method, path)
        if raw:
            return resp.content
        return resp.json() if resp.content else {}

    @api.model
    def _ping(self):
        """Return ``(ok, message)`` for the Test connection button.

        No endpoint returns a cheap 200 for an API key — creating a session is
        a real side effect — so this asks for a session that cannot exist. A
        404 means the key authenticated, which is the whole question; a 401 or
        403 means it did not.

        The status code alone is not proof: a wrong-but-reachable host (a
        typo'd domain, wrong port, or a load balancer's default backend)
        commonly answers any unmatched path with a blanket HTTP 404, which
        would otherwise read as "connected". A real SeeItLive reply is always
        JSON, so the body must parse before success is reported.
        """
        try:
            resp = self._send('GET', '/sessions/00000000-0000-0000-0000-000000000000/status')
        except UserError as exc:
            return False, str(exc)
        if resp.status_code in (200, 404):
            try:
                resp.json()
            except ValueError:
                return False, _(
                    'Reached %(url)s, but the response does not look like the '
                    'SeeItLive API (not JSON). Something on this network — a '
                    'proxy or a captive portal — is likely answering in its '
                    'place.'
                ) % {'url': self._base_url()}
            env = _('test/sandbox') if self._is_sandbox() else _('live')
            return True, _('Connected to SeeItLive (%(env)s key).') % {'env': env}
        if resp.status_code in (401, 403):
            return False, _(
                'SeeItLive rejected the API key (HTTP %(status)s). Check that it '
                'is active and from the right environment.'
            ) % {'status': resp.status_code}
        return False, _('Unexpected reply from SeeItLive (HTTP %(status)s).') % {
            'status': resp.status_code,
        }

    @api.model
    def _embed_origin(self):
        """This instance's origin, as it must appear in the key's allowlist.

        The browser sends scheme://host[:port] as the framing origin — never a
        path — so web.base.url's path is dropped.

        The scheme is ALWAYS https, whatever web.base.url says. Odoo records
        that value from a request it served, so an instance behind a
        TLS-terminating proxy without `proxy_mode` records http even though
        every browser reaches it over https. Reporting that back would be
        worse than useless: the admin allowlists an origin no browser ever
        sends, this check then confirms it as allowed, and the viewer stays
        blank with the reason visible only in a browser console. Framing the
        agent viewer — and the single-session token in its URL — over plain
        http is not a deployment this integration supports, so assuming the
        secure scheme costs nothing real.

        A port survives only when it is significant: 80 belongs to the http
        being upgraded away from, and 443 is https's own default, which
        browsers leave out of the Origin they send. Either one kept here would
        produce an entry that can never match.
        """
        base = (
            self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        ).strip()
        if '://' not in base:
            return ''
        host = base.split('://', 1)[1].split('/', 1)[0].lower()
        # rpartition, not split, so an IPv6 literal keeps its own colons; the
        # isdigit() guard stops a bare "[::1]" losing its last label to a
        # "port" that is really part of the address.
        name, sep, port = host.rpartition(':')
        if sep and port.isdigit() and port in ('80', '443'):
            host = name
        return 'https://%s' % host if host else ''

    @api.model
    def _origin_allowed(self, origin, entries):
        """CSP host-source matching: exact, or one leading '*.' wildcard label."""
        # Every entry is lowercased below before comparing; do the same to
        # origin rather than relying on callers to always pass the already-
        # lowercased output of _embed_origin().
        origin = (origin or '').lower()
        for raw in entries:
            entry = raw.split('#', 1)[0].split(';', 1)[0].strip().lower()
            if not entry:
                continue
            if entry == origin:
                return True
            if '://*.' in entry:
                scheme, rest = entry.split('://', 1)
                suffix = rest[1:]  # drop the '*', keeping '.example.com'
                # CSP matches ANY number of subdomain levels against a
                # wildcard (a.b.example.com matches *.example.com), but never
                # the bare domain. Matching more strictly here would report a
                # false negative for an origin the browser would accept.
                if origin.startswith(scheme + '://') and origin.endswith(suffix):
                    label = origin[len(scheme) + 3:-len(suffix)]
                    if label:
                        return True
        return False

    @api.model
    def _check_embed_origin(self):
        """Return ``(ok, message)``: is this Odoo in the key's embed allowlist?

        Without this check the failure mode is a blank iframe and a CSP error
        visible only in the browser console — nothing server-side to act on.
        """
        origin = self._embed_origin()
        if not origin:
            return False, _(
                'Could not determine this instance\'s origin from web.base.url.'
            )
        # Mirrors _ping's contract: this must always return a graceful
        # (ok, message) tuple, never raise, so Test connection can combine
        # it with the ping result in one notification — an unguarded raise
        # here would instead surface as an unrelated, session-oriented error
        # (_raise_api_error's copy talks about sessions, not embed origins;
        # a 404 here is exactly what an addon newer than the deployed API
        # would get) and would discard the successful ping result entirely.
        try:
            data = self._request('GET', '/agent-view/embed-origins') or {}
        except UserError as exc:
            return False, _(
                'Could not check whether this instance may embed the live '
                'viewer: %(error)s'
            ) % {'error': str(exc)}
        entries = data.get('embedOrigins') or []
        if self._origin_allowed(origin, entries):
            return True, _('This instance (%(origin)s) may embed the live viewer.') % {
                'origin': origin,
            }
        return False, _(
            'This instance (%(origin)s) is not in the API key\'s embedding '
            'origins, so the live viewer will not load. Add it in SeeItLive '
            'under the key\'s Security tab.'
        ) % {'origin': origin}

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    @api.model
    def _raise_api_error(self, resp, method, path):
        """Turn an HTTP failure into something an Odoo user can act on."""
        try:
            message = (resp.json() or {}).get('message', '') or resp.text
        except ValueError:
            message = resp.text
        status = resp.status_code
        # 402 (a plan gate) and 404 (a deleted session) are expected, routine
        # replies, not operational problems — logging them at ERROR just
        # trains an admin to ignore the log. Everything else (auth failures,
        # validation errors this addon didn't itself prevent, 5xx) stays
        # ERROR.
        log = _logger.warning if status in (402, 404) else _logger.error
        log('SeeItLive API error %s %s -> HTTP %s: %s', method, path, status, message)

        if status in (401, 403):
            raise SeeItLiveApiError(_(
                'SeeItLive rejected your API key (HTTP %(status)s). Open '
                'Settings > SeeItLive and check the key is active and from the '
                'right environment (live vs test).\n\n%(message)s'
            ) % {'status': status, 'message': message}, status=status)
        if status == 402:
            raise SeeItLiveApiError(_(
                'Your SeeItLive plan does not include this feature.\n\n%(message)s'
            ) % {'message': message}, status=status)
        if status == 404:
            raise SeeItLiveApiError(
                _('SeeItLive could not find that session (it may have been deleted).'),
                status=status,
            )
        if status in (400, 422):
            raise SeeItLiveApiError(
                _('SeeItLive refused the request.\n\n%(message)s') % {'message': message},
                status=status,
            )
        raise SeeItLiveApiError(_(
            'SeeItLive returned an unexpected error (HTTP %(status)s).\n\n%(message)s'
        ) % {'status': status, 'message': message}, status=status)
