from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEmbedOrigin(TransactionCase):

    def setUp(self):
        super().setUp()
        self.api = self.env['seeitlive.api']
        self.ICP = self.env['ir.config_parameter'].sudo()

    def test_origin_is_scheme_and_host_only(self):
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com/odoo/web')
        self.assertEqual(self.api._embed_origin(), 'https://odoo.acme.com')

    def test_origin_keeps_a_non_default_port_but_still_forces_https(self):
        self.ICP.set_param('web.base.url', 'http://odoo.acme.com:8069')
        self.assertEqual(self.api._embed_origin(), 'https://odoo.acme.com:8069')

    def test_origin_is_https_even_when_web_base_url_is_not(self):
        """The case that actually happens.

        Odoo records web.base.url from a request it served, so an instance
        behind a TLS-terminating proxy without `proxy_mode` records http
        while every browser reaches it over https. Echoing that back sent an
        admin to allowlist an origin no browser ever sends — and
        _check_embed_origin then reported it as allowed while the iframe
        stayed blank.
        """
        self.ICP.set_param('web.base.url', 'http://odoo.acme.com')
        self.assertEqual(self.api._embed_origin(), 'https://odoo.acme.com')

    def test_origin_drops_a_default_port(self):
        """80 belongs to the http being upgraded away from, and browsers omit
        443 from the Origin they send — either kept here can never match."""
        self.ICP.set_param('web.base.url', 'http://odoo.acme.com:80')
        self.assertEqual(self.api._embed_origin(), 'https://odoo.acme.com')
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com:443')
        self.assertEqual(self.api._embed_origin(), 'https://odoo.acme.com')

    def test_an_ipv6_literal_keeps_its_own_colons(self):
        """A naive split(':') would strip the last label off a bare address."""
        self.ICP.set_param('web.base.url', 'http://[::1]')
        self.assertEqual(self.api._embed_origin(), 'https://[::1]')
        self.ICP.set_param('web.base.url', 'http://[::1]:8069')
        self.assertEqual(self.api._embed_origin(), 'https://[::1]:8069')

    def test_an_http_origin_is_reported_missing_not_matched(self):
        """A key that allowlists the http origin the old code reported is NOT
        a match: the browser sends https, so confirming it would be the same
        false green this change exists to remove."""
        self.ICP.set_param('web.base.url', 'http://odoo.acme.com')
        with patch.object(
            type(self.api), '_request',
            return_value={'embedOrigins': ['http://odoo.acme.com']},
        ):
            ok, msg = self.api._check_embed_origin()
        self.assertFalse(ok)
        self.assertIn('https://odoo.acme.com', msg)

    def test_origin_drops_a_trailing_slash(self):
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com/')
        self.assertEqual(self.api._embed_origin(), 'https://odoo.acme.com')

    def test_exact_match_is_reported_as_allowed(self):
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://odoo.acme.com']},
        ):
            ok, _msg = self.api._check_embed_origin()
        self.assertTrue(ok)

    def test_wildcard_entry_matches_a_subdomain(self):
        self.ICP.set_param('web.base.url', 'https://acme.odoo.com')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://*.odoo.com']},
        ):
            ok, _msg = self.api._check_embed_origin()
        self.assertTrue(ok)

    def test_wildcard_matches_a_multi_level_subdomain(self):
        # CSP's *.odoo.com matches a.b.odoo.com. Being stricter here would tell
        # an admin their origin is missing when the browser would allow it.
        self.ICP.set_param('web.base.url', 'https://a.b.odoo.com')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://*.odoo.com']},
        ):
            ok, _msg = self.api._check_embed_origin()
        self.assertTrue(ok)

    def test_wildcard_does_not_match_the_bare_domain(self):
        self.ICP.set_param('web.base.url', 'https://odoo.com')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://*.odoo.com']},
        ):
            ok, _msg = self.api._check_embed_origin()
        self.assertFalse(ok)

    def test_missing_origin_names_the_host_to_add(self):
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://other.example.com']},
        ):
            ok, msg = self.api._check_embed_origin()
        self.assertFalse(ok)
        self.assertIn('https://odoo.acme.com', msg)

    def test_empty_list_is_reported_as_not_allowed(self):
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com')
        with patch.object(type(self.api), '_request', return_value={'embedOrigins': []}):
            ok, msg = self.api._check_embed_origin()
        self.assertFalse(ok)
        self.assertIn('https://odoo.acme.com', msg)

    def test_wildcard_does_not_match_a_hyphen_boundary(self):
        # 'evil-example.com' shares a suffix with 'example.com' but is not a
        # subdomain of it — there is no '.' boundary, only a '-'. CSP's
        # *.example.com must not match it, and neither must we.
        self.ICP.set_param('web.base.url', 'https://evil-example.com')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://*.example.com']},
        ):
            ok, _msg = self.api._check_embed_origin()
        self.assertFalse(ok)

    def test_wildcard_does_not_match_a_confusable_suffix(self):
        # 'example.com.attacker.net' contains 'example.com' as a substring
        # but 'attacker.net' is the actual registrable domain. A naive
        # substring/endswith-without-boundary-care check would wrongly
        # allow this.
        self.ICP.set_param('web.base.url', 'https://example.com.attacker.net')
        with patch.object(
            type(self.api), '_request', return_value={'embedOrigins': ['https://*.example.com']},
        ):
            ok, _msg = self.api._check_embed_origin()
        self.assertFalse(ok)

    def test_request_failure_is_reported_not_raised(self):
        """_check_embed_origin must always return (ok, message), the same
        contract _ping already honours — an unguarded raise here would
        replace the combined Test connection notification with an unrelated
        error modal and discard the successful ping result."""
        self.ICP.set_param('web.base.url', 'https://odoo.acme.com')
        with patch.object(
            type(self.api), '_request', side_effect=UserError('boom'),
        ):
            ok, msg = self.api._check_embed_origin()
        self.assertFalse(ok)
        self.assertIn('boom', msg)
