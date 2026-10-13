from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

ALL = ['sms', 'email', 'link']


@tagged('post_install', '-at_install')
class TestChannels(TransactionCase):
    """Which invitation channels the panel offers, per the key's plan."""

    def setUp(self):
        super().setUp()
        self.api = self.env['seeitlive.api']
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', 'sil_live_deadbeef')

    def _channels_with(self, **request_kwargs):
        with patch.object(type(self.api), '_request', **request_kwargs) as request:
            return self.api._allowed_channels(), request

    def test_asks_the_capabilities_endpoint(self):
        _channels, request = self._channels_with(return_value={'channels': ALL})
        method, path = request.call_args[0][:2]
        self.assertEqual(method, 'GET')
        self.assertEqual(path, '/capabilities')

    def test_a_plan_with_sms_offers_every_channel(self):
        channels, _request = self._channels_with(return_value={'channels': ALL})
        self.assertEqual(channels, ALL)

    def test_a_plan_without_sms_leaves_it_out(self):
        channels, _request = self._channels_with(return_value={'channels': ['email', 'link']})
        self.assertEqual(channels, ['email', 'link'])

    def test_keeps_the_panel_order_whatever_order_the_api_uses(self):
        """The panel falls back to the first channel left when its default is
        not allowed, so the order is part of the contract: email before link."""
        channels, _request = self._channels_with(return_value={'channels': ['link', 'email']})
        self.assertEqual(channels, ['email', 'link'])

    def test_ignores_a_channel_this_addon_does_not_know(self):
        channels, _request = self._channels_with(
            return_value={'channels': ['email', 'link', 'whatsapp']},
        )
        self.assertEqual(channels, ['email', 'link'])

    def test_fails_open_when_the_api_refuses(self):
        """An API older than /capabilities answers 404, a bad key 401. The API
        refuses an SMS session the plan lacks anyway, so an unknown answer
        must never take SMS away from a team that pays for it."""
        channels, _request = self._channels_with(side_effect=UserError('HTTP 404'))
        self.assertEqual(channels, ALL)

    def test_fails_open_without_an_api_key(self):
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', '')
        self.assertEqual(self.api._allowed_channels(), ALL)

    def test_fails_open_on_a_reply_with_no_list(self):
        for reply in ({}, None, {'channels': None}, {'channels': []}, ['email']):
            channels, _request = self._channels_with(return_value=reply)
            self.assertEqual(channels, ALL, reply)

    def test_the_panel_rpc_returns_the_same_list(self):
        with patch.object(
            type(self.api), '_request', return_value={'channels': ['email', 'link']},
        ):
            self.assertEqual(
                self.env['seeitlive.session'].rpc_allowed_channels(), ['email', 'link'],
            )
