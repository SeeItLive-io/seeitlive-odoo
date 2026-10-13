from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

CREATE_RESPONSE = {
    'id': 'ses_abc123', 'status': 'live', 'inviteCode': 'Ab3dEf',
    'inviteUrl': 'https://seeitlive.io/u/Ab3dEf#tok', 'inviteShortUrl': None,
    'agentViewUrl': 'https://seeitlive.io/a/Zz#createtok', 'protection': 'none',
    'expiresAt': None,
}


@tagged('post_install', '-at_install')
class TestAgentView(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', 'sil_live_deadbeef')
        partner = self.env['res.partner'].create({'name': 'Ada'})
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = dict(CREATE_RESPONSE)
            self.session = self.env['seeitlive.session'].action_start_for(
                'res.partner', partner.id, {'channel': 'link'},
            )

    def test_the_viewer_url_is_minted_fresh_every_time(self):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {
                'code': 'Yy8', 'token': 'freshtok',
                'agentViewUrl': 'https://seeitlive.io/a/Yy8#freshtok',
            }
            url = self.session.rpc_agent_view_url()

        method, path, payload = request.call_args[0][:3]
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/sessions/ses_abc123/agent-token')
        self.assertEqual(url, 'https://seeitlive.io/a/Yy8#freshtok')

    def test_the_minted_token_is_labelled_with_its_origin(self):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {'code': 'Yy8', 'token': 't', 'agentViewUrl': 'https://x/a/Yy8#t'}
            self.session.rpc_agent_view_url()
        payload = request.call_args[0][2]
        self.assertIn('Odoo', payload['label'])
        self.assertIn('res.partner', payload['label'])

    def test_no_agent_token_is_ever_written_to_the_database(self):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {'code': 'Yy8', 'token': 'freshtok',
                                    'agentViewUrl': 'https://seeitlive.io/a/Yy8#freshtok'}
            self.session.rpc_agent_view_url()
        self.session.invalidate_recordset()
        self.assertNotIn('freshtok', str(self.session.read()[0]))
        self.assertNotIn('createtok', str(self.session.read()[0]))
