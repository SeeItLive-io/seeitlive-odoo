from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReshare(TransactionCase):

    def setUp(self):
        super().setUp()
        self.session = self.env['seeitlive.session'].create({
            'name': 'sess_abc',
            'res_model': 'res.partner',
            'res_id': 1,
        })

    def test_revive_sends_only_the_mode(self):
        """An omitted channel/recipient must not be sent: absent means
        'reuse what was recorded'."""
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2026-09-20T10:00:00.000Z', 'resend': 'sent',
                          'rotated': False, 'inviteShortUrl': None},
        ) as req:
            self.session.action_reshare()
        # action_reshare also calls _refresh_status() on success, which issues
        # its own GET /status request afterwards — call_args would reflect
        # that later call, so pin this to the first (the reshare itself).
        reshare_call = req.call_args_list[0][0]
        _method, path, payload = reshare_call[0], reshare_call[1], reshare_call[2]
        self.assertEqual(path, '/sessions/sess_abc/reshare')
        self.assertEqual(payload.get('mode'), 'revive')
        self.assertNotIn('channel', payload)
        self.assertNotIn('recipient', payload)

    def test_sms_requires_consent_before_the_request_is_made(self):
        """The addon must refuse locally rather than let the API 400."""
        with patch.object(type(self.env['seeitlive.api']), '_request') as req:
            with self.assertRaises(UserError):
                self.session.action_reshare(channel='sms', recipient='+15145550123',
                                            sms_consent=False)
            req.assert_not_called()

    def test_sms_with_consent_sends_the_flag(self):
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2026-09-20T10:00:00.000Z', 'resend': 'sent',
                          'rotated': False, 'inviteShortUrl': None},
        ) as req:
            self.session.action_reshare(channel='sms', recipient='+15145550123',
                                        sms_consent=True)
        # See test_revive_sends_only_the_mode: pin to the reshare call, not
        # the follow-up _refresh_status() request.
        payload = req.call_args_list[0][0][2]
        self.assertEqual(payload['channel'], 'sms')
        self.assertEqual(payload['smsConsent'], True)

    def test_mode_new_is_passed_through(self):
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2026-09-20T10:00:00.000Z', 'resend': 'sent',
                          'rotated': True, 'inviteShortUrl': 'https://sitl.cc/s/Qm81'},
        ) as req:
            result = self.session.action_reshare(mode='new')
        # See test_revive_sends_only_the_mode: pin to the reshare call, not
        # the follow-up _refresh_status() request.
        self.assertEqual(req.call_args_list[0][0][2]['mode'], 'new')
        self.assertTrue(result['rotated'])

    def test_api_failure_is_reported_not_swallowed(self):
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          side_effect=UserError('boom')):
            with self.assertRaises(UserError):
                self.session.action_reshare()

    # ------------------------------------------------------------------
    # The panel's own call shape
    # ------------------------------------------------------------------

    def test_reshare_via_orm_call_shape_matches_the_panel(self):
        """The panel never calls ``self.session.action_reshare(...)`` the
        way the tests above do — it dispatches through the ORM as
        ``orm.call('seeitlive.session', 'action_reshare', [[id]], {...})``,
        which is a positional recordset (browsed by id) plus keyword args.
        Exercising that exact shape, with the exact four keyword names the
        JS sends (channel, recipient, sms_consent, mode), is what catches a
        rename on either side before it ships — nothing else in this file
        would notice a signature drift, since every other test calls the
        method directly on an already-bound recordset.
        """
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2026-09-20T10:00:00.000Z', 'resend': 'sent',
                          'rotated': False, 'inviteShortUrl': None},
        ) as req:
            recordset = self.env['seeitlive.session'].browse(self.session.ids)
            recordset.action_reshare(
                channel='email',
                recipient='client@example.com',
                sms_consent=False,
                mode='revive',
            )
        # See test_revive_sends_only_the_mode: pin to the reshare call, not
        # the follow-up _refresh_status() request.
        reshare_call = req.call_args_list[0][0]
        self.assertEqual(reshare_call[1], '/sessions/sess_abc/reshare')
        payload = reshare_call[2]
        self.assertEqual(payload['mode'], 'revive')
        self.assertEqual(payload['channel'], 'email')
        self.assertEqual(payload['recipient'], 'client@example.com')
        self.assertNotIn('smsConsent', payload)

    # ------------------------------------------------------------------
    # Keeping the local record in sync with what the API just changed
    # ------------------------------------------------------------------

    def test_a_successful_reshare_updates_the_stored_expiry_and_short_link(self):
        """Nothing else writes expires_at/invite_short_url after creation.
        Skipping this write would leave rpc_panel_state's 'expired' true
        forever after a successful revive, and the panel's copy-link box
        would keep showing a short link the API has already replaced."""
        self.session.write({'expires_at': fields.Datetime.now() - timedelta(minutes=5)})
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2099-01-01T00:00:00.000Z', 'resend': 'sent',
                          'rotated': True, 'inviteShortUrl': 'https://sitl.cc/s/new1'},
        ):
            self.session.action_reshare(mode='new')
        self.assertEqual(self.session.invite_short_url, 'https://sitl.cc/s/new1')
        self.assertGreater(self.session.expires_at, fields.Datetime.now())

    def test_a_rotated_reshare_clears_the_stale_invite_url(self):
        """mode:'new' kills the customer's old link server-side and never
        returns a replacement (the plaintext token cannot be rebuilt from
        the API's response by design). Leaving the stored invite_url in
        place would let the form's read-only 'Customer Link' field keep
        rendering a link the API has already revoked."""
        self.session.write({'invite_url': 'https://seeitlive.io/u/OLDCODE#oldtoken'})
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2099-01-01T00:00:00.000Z', 'resend': 'sent',
                          'rotated': True, 'inviteShortUrl': 'https://sitl.cc/s/new1'},
        ):
            self.session.action_reshare(mode='new')
        self.assertFalse(self.session.invite_url)

    def test_a_revived_reshare_leaves_invite_url_alone(self):
        """mode:'revive' extends the SAME link in place — the customer's
        original invite_url still works, so it must not be cleared."""
        self.session.write({'invite_url': 'https://seeitlive.io/u/SAMECODE#sametoken'})
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2099-01-01T00:00:00.000Z', 'resend': 'sent',
                          'rotated': False, 'inviteShortUrl': None},
        ):
            self.session.action_reshare()
        self.assertEqual(self.session.invite_url, 'https://seeitlive.io/u/SAMECODE#sametoken')

    def test_an_absent_short_link_in_the_response_leaves_the_recorded_one_alone(self):
        """inviteShortUrl comes back null when reviving a session that never
        had one (extendSessionShortLink is a no-op with nothing to extend).
        Absent must not overwrite whatever short link WAS recorded with a
        falsy value — the same 'omitted means unchanged' convention the
        request-building half of this method already follows for
        channel/recipient."""
        self.session.write({'invite_short_url': 'https://sitl.cc/s/keep-me'})
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2099-01-01T00:00:00.000Z', 'resend': 'not_requested',
                          'rotated': False, 'inviteShortUrl': None},
        ):
            self.session.action_reshare()
        self.assertEqual(self.session.invite_short_url, 'https://sitl.cc/s/keep-me')

    # ------------------------------------------------------------------
    # The panel's 'expired' flag
    # ------------------------------------------------------------------

    def test_expired_is_true_only_for_a_pending_session_past_its_own_expiry(self):
        """Computed server-side, against THIS server's clock — the whole
        point Task 6 exists for is to show re-share exactly when the link
        is actually dead, which a browser-side comparison cannot promise."""
        self.session.write({'expires_at': fields.Datetime.now() - timedelta(minutes=5)})
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          return_value={'id': 'sess_abc', 'status': 'pending'}):
            self.assertTrue(self.session.rpc_panel_state()['expired'])

        self.session.write({'expires_at': fields.Datetime.now() + timedelta(minutes=30)})
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          return_value={'id': 'sess_abc', 'status': 'pending'}):
            self.assertFalse(self.session.rpc_panel_state()['expired'])

    def test_expired_is_false_once_the_customer_has_claimed_the_invite(self):
        """A session past 'pending' proves the link worked, regardless of
        what this record's own (write-time) expires_at snapshot says —
        offering to revive a link the customer is actively using would be
        actively wrong, not just unnecessary."""
        self.session.write({
            'expires_at': fields.Datetime.now() - timedelta(minutes=5),
            'state': 'ringing',
        })
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          return_value={'id': 'sess_abc', 'status': 'ringing'}):
            self.assertFalse(self.session.rpc_panel_state()['expired'])

    # ------------------------------------------------------------------
    # The header button's entry point
    # ------------------------------------------------------------------

    def test_reshare_button_returns_true_not_a_dict(self):
        """Odoo's doActionButton treats a truthy dict returned from a
        type="object" button as an action descriptor and requires a 'type'
        key on it. action_reshare returns the raw API response dict on
        purpose (Task 5's test_mode_new_is_passed_through asserts on
        result['rotated']), so the header button must go through a wrapper
        that still reaches the reshare endpoint but discards that dict and
        returns True instead — exactly what action_sync already does for
        the same reason."""
        with patch.object(
            type(self.env['seeitlive.api']), '_request',
            return_value={'expiresAt': '2026-09-20T10:00:00.000Z', 'resend': 'sent',
                          'rotated': False, 'inviteShortUrl': None},
        ) as req:
            result = self.session.action_reshare_button()
        self.assertIs(result, True)
        # See test_revive_sends_only_the_mode: pin to the reshare call, not
        # the follow-up _refresh_status() request.
        reshare_call = req.call_args_list[0][0]
        self.assertEqual(reshare_call[1], '/sessions/sess_abc/reshare')
