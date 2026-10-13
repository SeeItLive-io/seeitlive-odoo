from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

CREATE_RESPONSE = {
    'id': 'ses_x', 'status': 'pending', 'inviteCode': 'Ab3dEf',
    'inviteUrl': 'https://seeitlive.io/u/Ab3dEf#tok', 'inviteShortUrl': None,
    'agentViewUrl': 'https://seeitlive.io/a/Zz#t', 'protection': 'none',
    'expiresAt': None,
}


@tagged('post_install', '-at_install')
class TestEnabledModels(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', 'sil_live_deadbeef')
        self.partner = self.env['res.partner'].create({'name': 'Ada'})

    def _start(self, state='pending'):
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = {**CREATE_RESPONSE, 'status': state}
            return self.env['seeitlive.session'].action_start_for(
                'res.partner', self.partner.id, {'channel': 'link'},
            )

    def test_a_record_sees_its_own_sessions(self):
        session = self._start()
        self.assertIn(session, self.partner.seeitlive_session_ids)

    def test_a_session_on_a_non_chatter_model_does_not_raise(self):
        """action_start_for takes res_model as a plain string with no
        restriction to mail.thread models, so a session can point at one
        that never got seeitlive_session_ids/seeitlive_active_session_id.
        The thread-cache invalidation hook must skip it rather than let
        invalidate_recordset(fnames) KeyError on a missing field and break
        the create()/write()."""
        currency = self.env['res.currency'].search([], limit=1)
        self.assertTrue(currency, 'expected at least one res.currency record to exist')
        self.assertNotIn('seeitlive_session_ids', currency._fields)

        session = self.env['seeitlive.session'].create({
            'name': 'ses_non_chatter',
            'res_model': 'res.currency',
            'res_id': currency.id,
            'state': 'pending',
        })
        session.write({'state': 'ended'})

    def test_the_active_session_is_the_newest_non_terminal_one(self):
        old = self._start()
        old.state = 'ended'
        current = self._start(state='live')
        self.assertEqual(self.partner.seeitlive_active_session_id, current)

    def test_no_active_session_once_every_session_is_terminal(self):
        session = self._start()
        session.state = 'ended'
        self.partner.invalidate_recordset()
        self.assertFalse(self.partner.seeitlive_active_session_id)

    def test_an_unsaved_record_computes_without_blowing_up(self):
        draft = self.env['res.partner'].new({'name': 'Draft'})
        self.assertFalse(draft.seeitlive_session_ids)
        self.assertFalse(draft.seeitlive_active_session_id)

    def test_saving_the_settings_injects_one_view_per_enabled_model(self):
        settings = self.env['res.config.settings'].create({})
        model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        settings.seeitlive_enabled_model_ids = [(6, 0, model.ids)]
        settings.set_values()

        views = self.env['ir.ui.view'].search([('name', '=', 'seeitlive.panel.dyn.res.partner')])
        self.assertEqual(len(views), 1)
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param('seeitlive.enabled_models'),
            'res.partner',
        )

    def test_saving_twice_does_not_leave_a_duplicate_tab(self):
        model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        for _i in range(2):
            settings = self.env['res.config.settings'].create({})
            settings.seeitlive_enabled_model_ids = [(6, 0, model.ids)]
            settings.set_values()
        views = self.env['ir.ui.view'].search([('name', '=like', 'seeitlive.panel.dyn.%')])
        self.assertEqual(len(views), 1)

    def test_unchecking_a_model_removes_its_tab(self):
        model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_enabled_model_ids = [(6, 0, model.ids)]
        settings.set_values()

        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_enabled_model_ids = [(6, 0, [])]
        settings.set_values()

        self.assertFalse(self.env['ir.ui.view'].search([('name', '=like', 'seeitlive.panel.dyn.%')]))

    def test_unchecking_every_model_round_trips_as_empty(self):
        """ir.config_parameter.get_param(key, default) is
        `_get_param(key) or default` — an empty string is falsy, so storing
        '' for "no models" would silently read back as the three-model
        default on the very next settings open, exactly like the Boolean
        below. Assert the round trip, not just the immediate view sync,
        which is the shape test_unchecking_a_model_removes_its_tab above
        does not cover."""
        model = self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_enabled_model_ids = [(6, 0, model.ids)]
        settings.set_values()

        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_enabled_model_ids = [(6, 0, [])]
        settings.set_values()

        fresh = self.env['res.config.settings'].create({})
        self.assertFalse(fresh.seeitlive_enabled_model_ids)

    def test_unchecking_attach_snapshots_round_trips_as_off(self):
        """ir.config_parameter.set_param(key, False) UNLINKS the row rather
        than storing it, so get_param's default silently wins on the next
        read — unticking the box had no effect and it re-ticked itself on
        the next settings open."""
        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_attach_snapshots = False
        settings.set_values()

        fresh = self.env['res.config.settings'].create({})
        self.assertFalse(fresh.seeitlive_attach_snapshots)
        self.assertFalse(self.env['seeitlive.api']._config_bool('attach_snapshots', True))

    def test_sandbox_indicator_round_trips_true_for_a_test_key(self):
        """seeitlive_is_sandbox used to be a non-stored compute
        (@api.depends('seeitlive_api_key')). That reads right on the very
        same in-memory record — settings.seeitlive_api_key = 'sil_test_x';
        settings.seeitlive_is_sandbox is True — which is exactly why the
        bug survived: res.config.settings is a TransientModel whose client
        payload comes from default_get() -> get_values(), a path a compute
        never runs through, so the browser always saw False no matter what
        was saved. Go through set_values() and then a FRESH create(), the
        way the settings form actually loads, not the same record."""
        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_api_key = 'sil_test_deadbeef'
        settings.set_values()

        fresh = self.env['res.config.settings'].create({})
        self.assertTrue(fresh.seeitlive_is_sandbox)

    def test_sandbox_indicator_round_trips_false_for_a_live_key(self):
        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_api_key = 'sil_live_deadbeef'
        settings.set_values()

        fresh = self.env['res.config.settings'].create({})
        self.assertFalse(fresh.seeitlive_is_sandbox)

    def test_test_connection_does_not_revert_a_disabled_setting(self):
        """action_seeitlive_test_connection() calls set_values() to persist
        the form before pinging — that used to be enough on its own to flip
        an unticked 'Attach Snapshots' back on, for the same unlink-on-False
        reason as the test above."""
        settings = self.env['res.config.settings'].create({})
        settings.seeitlive_attach_snapshots = False
        # A successful ping now also triggers _check_embed_origin() (a real
        # HTTP call otherwise) — mock it alongside _ping so this test stays
        # hermetic instead of reaching the live SeeItLive API.
        with patch.object(type(self.env['seeitlive.api']), '_ping', return_value=(True, 'ok')), \
             patch.object(type(self.env['seeitlive.api']), '_check_embed_origin', return_value=(True, 'ok')):
            settings.action_seeitlive_test_connection()

        fresh = self.env['res.config.settings'].create({})
        self.assertFalse(fresh.seeitlive_attach_snapshots)

    def test_the_sheet_fallback_wraps_the_page_in_its_own_notebook(self):
        """A form with no <notebook> still gets the tab rather than nothing."""
        settings = self.env['res.config.settings'].create({})
        notebook_arch = settings._seeitlive_panel_arch('notebook')
        sheet_arch = settings._seeitlive_panel_arch('sheet')
        self.assertIn('//notebook', notebook_arch)
        self.assertNotIn('<notebook>', notebook_arch)
        self.assertIn('//sheet', sheet_arch)
        self.assertIn('<notebook>', sheet_arch)

    def test_only_chatter_models_are_offered(self):
        settings = self.env['res.config.settings'].create({})
        domain = settings._fields['seeitlive_enabled_model_ids'].domain
        self.assertIn(('is_mail_thread', '=', True), domain)
