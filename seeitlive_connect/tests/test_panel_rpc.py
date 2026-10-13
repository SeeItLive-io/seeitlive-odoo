from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPanelRpc(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()

    def test_recipients_come_from_the_record_itself(self):
        partner = self.env['res.partner'].create({
            'name': 'Ada', 'phone': '+15555550123', 'mobile': '+15555550999',
            'email': 'ada@example.com',
        })
        found = partner.rpc_seeitlive_recipients()
        # Mobile first: it is the number a customer can actually stream from.
        self.assertEqual(found['phones'], ['+15555550999', '+15555550123'])
        self.assertEqual(found['emails'], ['ada@example.com'])

    def test_recipients_fall_back_to_the_linked_partner(self):
        partner = self.env['res.partner'].create({
            'name': 'Ada', 'mobile': '+15555550999', 'email': 'ada@example.com',
        })
        lead = self.env['crm.lead'].create({'name': 'Broken boiler', 'partner_id': partner.id})
        found = lead.rpc_seeitlive_recipients()
        self.assertIn('+15555550999', found['phones'])
        self.assertIn('ada@example.com', found['emails'])

    def test_recipients_are_deduplicated_and_never_blank(self):
        partner = self.env['res.partner'].create({
            'name': 'Ada', 'phone': '+15555550123', 'mobile': '+15555550123',
        })
        found = partner.rpc_seeitlive_recipients()
        self.assertEqual(found['phones'], ['+15555550123'])
        self.assertEqual(found['emails'], [])

    def test_defaults_come_from_the_settings(self):
        self.ICP.set_param('seeitlive.default_channel', 'email')
        self.ICP.set_param('seeitlive.default_protection', 'totp')
        partner = self.env['res.partner'].create({'name': 'Ada'})
        self.assertEqual(
            partner.rpc_seeitlive_defaults(),
            {'channel': 'email', 'protection': 'totp'},
        )

    def test_recipients_on_an_unsaved_record_do_not_raise(self):
        """The panel dispatches with id False for a record that has not
        been saved yet (an unsaved record's resId is False client-side) --
        this must return the empty payload, not blow up."""
        unsaved = self.env['res.partner'].browse([False])
        self.assertEqual(unsaved.rpc_seeitlive_recipients(), {'phones': [], 'emails': []})

    def test_recipients_on_a_deleted_record_do_not_raise(self):
        """A stale id (the row was deleted after the form loaded) must not
        raise MissingError -- it should read as 'no recipients known'."""
        partner = self.env['res.partner'].create({'name': 'Ghost', 'mobile': '+15555550999'})
        ghost = self.env['res.partner'].browse([partner.id])
        partner.unlink()
        self.assertEqual(ghost.rpc_seeitlive_recipients(), {'phones': [], 'emails': []})

    def test_defaults_on_an_unsaved_or_deleted_record_do_not_raise(self):
        """rpc_seeitlive_defaults reads only settings, never self, so it
        must already be safe on both an unsaved and a deleted record."""
        self.ICP.set_param('seeitlive.default_channel', 'email')
        self.ICP.set_param('seeitlive.default_protection', 'totp')
        expected = {'channel': 'email', 'protection': 'totp'}

        unsaved = self.env['res.partner'].browse([False])
        self.assertEqual(unsaved.rpc_seeitlive_defaults(), expected)

        partner = self.env['res.partner'].create({'name': 'Ghost'})
        ghost = self.env['res.partner'].browse([partner.id])
        partner.unlink()
        self.assertEqual(ghost.rpc_seeitlive_defaults(), expected)
