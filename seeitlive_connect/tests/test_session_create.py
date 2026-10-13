from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

CREATE_RESPONSE = {
    'id': 'ses_abc123',
    'status': 'pending',
    'inviteCode': 'Ab3dEf',
    'inviteUrl': 'https://seeitlive.io/u/Ab3dEf#tok',
    'inviteShortUrl': 'https://sitl.cc/Ab3dEf',
    'agentViewUrl': 'https://seeitlive.io/a/Zz9yXw#agenttok',
    'metadata': {'odoo_model': 'res.partner', 'odoo_res_id': '1'},
    'expiresAt': '2026-09-02T18:30:00.000Z',
    'protection': 'none',
    'protectionSecret': None,
}


@tagged('post_install', '-at_install')
class TestSessionCreate(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', 'sil_live_deadbeef')
        self.partner = self.env['res.partner'].create({
            'name': 'Ada Lovelace',
            'phone': '+15555550123',
            'email': 'ada@example.com',
        })
        self.Session = self.env['seeitlive.session']

    def _start(self, **options):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = dict(CREATE_RESPONSE)
            session = self.Session.action_start_for('res.partner', self.partner.id, options)
        return session, request

    def test_a_link_session_stores_what_cannot_be_re_derived(self):
        session, request = self._start(channel='link')
        self.assertEqual(session.name, 'ses_abc123')
        self.assertEqual(session.invite_url, 'https://seeitlive.io/u/Ab3dEf#tok')
        self.assertEqual(session.invite_short_url, 'https://sitl.cc/Ab3dEf')
        self.assertEqual(session.invite_code, 'Ab3dEf')
        self.assertEqual(session.res_model, 'res.partner')
        self.assertEqual(session.res_id, self.partner.id)
        self.assertEqual(session.state, 'pending')

    def test_the_agent_view_url_is_never_persisted(self):
        """It is re-mintable and expires in 120 minutes; storing it would put a
        credential that grants annotate + snapshot rights at rest in the ERP."""
        session, _request = self._start(channel='link')
        stored = session.read()[0]
        self.assertNotIn('agenttok', str(stored))
        self.assertFalse([f for f in session._fields if 'agent' in f and 'token' in f])

    def test_metadata_attributes_the_session_to_the_record(self):
        _session, request = self._start(channel='link')
        payload = request.call_args[0][2]
        self.assertEqual(payload['metadata']['odoo_model'], 'res.partner')
        self.assertEqual(payload['metadata']['odoo_res_id'], str(self.partner.id))
        self.assertEqual(payload['metadata']['odoo_db'], self.env.cr.dbname)

    def test_sms_without_consent_is_refused_before_any_http_call(self):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            with self.assertRaises(UserError) as caught:
                self.Session.action_start_for('res.partner', self.partner.id, {
                    'channel': 'sms', 'recipient': '+15555550123', 'sms_consent': False,
                })
        self.assertIn('consent', str(caught.exception).lower())
        request.assert_not_called()

    def test_sms_with_consent_sends_consent_and_asks_for_a_short_link(self):
        _session, request = self._start(
            channel='sms', recipient='+15555550123', sms_consent=True,
        )
        payload = request.call_args[0][2]
        self.assertEqual(payload['channel'], 'sms')
        self.assertEqual(payload['recipientPhone'], '+15555550123')
        self.assertIs(payload['smsConsent'], True)
        self.assertIs(payload['shortLink'], True)

    def test_email_sends_the_recipient_address(self):
        _session, request = self._start(channel='email', recipient='ada@example.com')
        payload = request.call_args[0][2]
        self.assertEqual(payload['recipientEmail'], 'ada@example.com')
        self.assertNotIn('smsConsent', payload)

    def test_protection_none_is_omitted_rather_than_sent(self):
        """The create schema has no "none" value for protection; sending it
        would be a 400."""
        _session, request = self._start(channel='link', protection='none')
        self.assertNotIn('protection', request.call_args[0][2])

    def test_a_qr_is_rendered_for_the_customer_link(self):
        session, _request = self._start(channel='link')
        self.assertTrue(session.qr_png)

    def test_api_timestamps_become_naive_utc(self):
        parsed = self.Session._parse_api_datetime('2026-09-02T18:30:00.000Z')
        self.assertEqual(parsed.year, 2026)
        self.assertIsNone(parsed.tzinfo)
        self.assertFalse(self.Session._parse_api_datetime(None))
        self.assertFalse(self.Session._parse_api_datetime('not a date'))
