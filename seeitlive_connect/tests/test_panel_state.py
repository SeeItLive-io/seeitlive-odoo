from json import JSONDecodeError
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

CREATE_RESPONSE = {
    'id': 'ses_abc123', 'status': 'pending', 'inviteCode': 'Ab3dEf',
    'inviteUrl': 'https://seeitlive.io/u/Ab3dEf#tok',
    'inviteShortUrl': 'https://sitl.cc/Ab3dEf',
    'agentViewUrl': 'https://seeitlive.io/a/Zz#t', 'protection': 'none', 'expiresAt': None,
}


@tagged('post_install', '-at_install')
class TestPanelState(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', 'sil_live_deadbeef')
        self.partner = self.env['res.partner'].create({'name': 'Ada'})

    def _session(self, **overrides):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {**CREATE_RESPONSE, **overrides}
            return self.env['seeitlive.session'].action_start_for(
                'res.partner', self.partner.id, {'channel': 'link'},
            )

    def test_status_refresh_writes_the_lifecycle(self):
        session = self._session()
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {
                'id': 'ses_abc123', 'status': 'live',
                'startedAt': '2026-09-02T18:00:00.000Z', 'endedAt': None,
                'producerCount': 1, 'hasVideo': True,
            }
            session._refresh_status()
        self.assertEqual(session.state, 'live')
        self.assertEqual(session.started_at.hour, 18)

    def test_a_terminal_session_is_not_polled_again(self):
        session = self._session()
        session.state = 'ended'
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            session._refresh_status()
        request.assert_not_called()

    def test_a_network_failure_is_recorded_not_raised(self):
        """The panel polls every 5 seconds. A blip must not throw a dialog in
        the agent's face mid-call."""
        session = self._session()
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.side_effect = UserError('SeeItLive is unreachable')
            session._refresh_status()
        self.assertIn('unreachable', session.last_error)
        self.assertEqual(session.state, 'pending')

    def test_a_non_json_reply_is_recorded_not_raised(self):
        """A stale base URL or a misconfigured proxy can serve an HTML page on
        a 200; _request()'s resp.json() then raises a ValueError subclass,
        not a UserError. That must be caught too, not just UserError."""
        session = self._session()
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.side_effect = JSONDecodeError('Expecting value', '', 0)
            session._refresh_status()
        self.assertIn('Expecting value', session.last_error)
        self.assertEqual(session.state, 'pending')

    def test_an_unrecognised_status_is_logged_not_written(self):
        """The server's status enum can drift ahead of this addon's
        SESSION_STATES before an upgrade lands; an unknown value must not
        blow up the Selection field write."""
        session = self._session()
        logger_name = 'odoo.addons.seeitlive_connect.models.seeitlive_session'
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {'id': 'ses_abc123', 'status': 'a-future-status'}
            with self.assertLogs(logger_name, level='WARNING') as captured:
                session._refresh_status()
        self.assertEqual(session.state, 'pending')
        self.assertTrue(any('a-future-status' in line for line in captured.output))

    def test_panel_state_carries_everything_one_render_needs(self):
        session = self._session()
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {'id': 'ses_abc123', 'status': 'pending'}
            state = session.rpc_panel_state()
        self.assertEqual(state['share_url'], 'https://sitl.cc/Ab3dEf')
        self.assertTrue(state['qr_data_url'].startswith('data:image/png;base64,'))
        self.assertFalse(state['is_finished'])
        self.assertEqual(state['protection'], {})

    def test_an_unprotected_session_never_asks_for_a_secret(self):
        session = self._session()
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {'id': 'ses_abc123', 'status': 'pending'}
            session.rpc_panel_state()
        called = [call[0][1] for call in request.call_args_list]
        self.assertNotIn('/sessions/ses_abc123/protection', called)

    def test_a_protected_session_fetches_the_current_secret_every_poll(self):
        """Nothing is stored locally: a rotating code would be stale within
        30 seconds, and a stored password is a secret at rest for no gain."""
        session = self._session(protection='totp')
        self.assertEqual(session.protection, 'totp')
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.side_effect = [
                {'id': 'ses_abc123', 'status': 'pending'},
                {'type': 'totp', 'password': None, 'code': '481920',
                 'secondsRemaining': 12, 'locked': False, 'failedAttempts': 0},
            ]
            state = session.rpc_panel_state()
        self.assertEqual(state['protection']['code'], '481920')
        self.assertEqual(state['protection']['seconds_remaining'], 12)
        self.assertNotIn('protection_secret', session._fields)

    def test_the_secret_stops_being_fetched_once_the_guest_has_answered(self):
        """The API refuses to move a protected session past 'pending' until
        the guest has already passed the challenge (invitations/claim checks
        the unlock token before writing 'ringing'). So by the time state is
        anything but 'pending' there is nothing left to read out, and the
        five-second poll must stop hitting /protection for the rest of the
        call -- not just once the call goes live."""
        for status in ('ringing', 'live', 'ended'):
            session = self._session(protection='totp')
            with patch.object(type(self.env['seeitlive.api']), '_request') as request:
                request.return_value = {'id': 'ses_abc123', 'status': status}
                state = session.rpc_panel_state()
            self.assertEqual(state['protection'], {}, status)
            called = [call[0][1] for call in request.call_args_list]
            self.assertNotIn('/sessions/ses_abc123/protection', called, status)

    def test_finished_is_true_for_every_terminal_state(self):
        for terminal in ('ended', 'expired', 'cancelled', 'error'):
            session = self._session()
            session.state = terminal
            with patch.object(type(self.env['seeitlive.api']), '_request'):
                self.assertTrue(session.rpc_panel_state()['is_finished'], terminal)
