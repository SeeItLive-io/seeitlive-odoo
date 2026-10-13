import hashlib
import hmac
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestApiClient(TransactionCase):

    def setUp(self):
        super().setUp()
        self.api = self.env['seeitlive.api']
        self.ICP = self.env['ir.config_parameter'].sudo()

    def test_base_url_is_fixed_and_ignores_a_stray_parameter(self):
        """There is one SeeItLive service, so there is one API host.

        The `seeitlive.base_url` parameter used to override this and is
        deleted on upgrade (migrations/17.0.1.1.0). A row can still be
        re-created by hand in Technical > System Parameters, so pin that it
        buys nothing — silently honouring it again would send a customer
        install at whatever address someone typed.
        """
        self.assertEqual(self.api._base_url(), 'https://api.seeitlive.io/v1')
        self.ICP.set_param('seeitlive.base_url', 'https://seeitlive.example.com/v1/')
        self.assertEqual(self.api._base_url(), 'https://api.seeitlive.io/v1')

    def test_missing_api_key_is_an_actionable_error(self):
        self.ICP.set_param('seeitlive.api_key', '')
        with self.assertRaises(UserError) as caught:
            self.api._api_key()
        self.assertIn('Settings', str(caught.exception))

    def test_no_signing_secret_means_no_signing_headers(self):
        self.ICP.set_param('seeitlive.signing_secret', '')
        self.assertEqual(self.api._signing_headers('POST', '/sessions', '{}'), {})

    def test_signing_base_string_matches_the_server(self):
        """Byte-for-byte agreement with apps/api/src/lib/api-key-signing.ts.

        The path is VERSION-RELATIVE (/sessions), unlike apertur's plugin which
        signs the full /api/v1/... path with X-Aptr-* headers. Getting this
        wrong fails closed with a 401 and no useful message, so it is pinned.
        """
        self.ICP.set_param('seeitlive.signing_secret', 's3cr3t')
        body = '{"channel":"link"}'
        headers = self.api._signing_headers('post', '/sessions', body, timestamp=1700000000)

        expected_base = '1700000000.POST./sessions.%s' % hashlib.sha256(body.encode()).hexdigest()
        expected_sig = hmac.new(b's3cr3t', expected_base.encode(), hashlib.sha256).hexdigest()

        self.assertEqual(headers['X-SeeItLive-Timestamp'], '1700000000')
        self.assertEqual(headers['X-SeeItLive-Signature'], 'sha256=%s' % expected_sig)

    def test_an_empty_body_hashes_as_sha256_of_empty_string(self):
        self.ICP.set_param('seeitlive.signing_secret', 's3cr3t')
        headers = self.api._signing_headers('GET', '/sessions/abc/status', '', timestamp=1700000000)
        expected_base = '1700000000.GET./sessions/abc/status.%s' % hashlib.sha256(b'').hexdigest()
        expected_sig = hmac.new(b's3cr3t', expected_base.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-SeeItLive-Signature'], 'sha256=%s' % expected_sig)

    def test_a_401_names_the_api_key_not_the_status_code(self):
        self.ICP.set_param('seeitlive.api_key', 'sil_live_deadbeef')
        with patch('odoo.addons.seeitlive_connect.models.seeitlive_api.requests.request') as req:
            req.return_value.status_code = 401
            req.return_value.content = b'{"message":"nope"}'
            req.return_value.json.return_value = {'message': 'nope'}
            with self.assertRaises(UserError) as caught:
                self.api._request('GET', '/sessions/abc/status')
        self.assertIn('API key', str(caught.exception))

    def test_ping_treats_404_as_authenticated(self):
        """There is no cheap 200 an API key can call: creating a session is a
        real side effect. A 404 on a non-existent session proves the key was
        accepted, which is exactly what "Test connection" needs to know —
        provided the 404 actually came from the SeeItLive API (see the next
        test for the case where it didn't).
        """
        self.ICP.set_param('seeitlive.api_key', 'sil_live_deadbeef')
        with patch('odoo.addons.seeitlive_connect.models.seeitlive_api.requests.request') as req:
            req.return_value.status_code = 404
            req.return_value.content = b'{}'
            req.return_value.json.return_value = {}
            ok, _message = self.api._ping()
        self.assertTrue(ok)

        with patch('odoo.addons.seeitlive_connect.models.seeitlive_api.requests.request') as req:
            req.return_value.status_code = 401
            req.return_value.content = b'{}'
            ok, _message = self.api._ping()
        self.assertFalse(ok)

    def test_ping_rejects_a_404_that_did_not_come_from_seeitlive(self):
        """A reachable-but-wrong host (typo'd domain, wrong port, or a load
        balancer's default backend) commonly blanket-404s any unmatched path.
        Status code alone would read that as "connected" — exactly the
        failure "Test connection" exists to catch — so a 404/200 must also
        parse as JSON, and the failure message must name the configured base
        URL so the admin knows what to fix.
        """
        self.ICP.set_param('seeitlive.api_key', 'sil_live_deadbeef')
        with patch('odoo.addons.seeitlive_connect.models.seeitlive_api.requests.request') as req:
            req.return_value.status_code = 404
            req.return_value.content = b'<html><body>404 Not Found</body></html>'
            req.return_value.json.side_effect = ValueError('not JSON')
            ok, message = self.api._ping()
        self.assertFalse(ok)
        self.assertIn(self.api._base_url(), message)
