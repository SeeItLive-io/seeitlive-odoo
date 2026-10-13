import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    seeitlive_api_key = fields.Char(
        string='API Key',
        help='Your SeeItLive API key. Starts with sil_live_ or sil_test_.',
        config_parameter='seeitlive.api_key',
    )
    seeitlive_signing_secret = fields.Char(
        string='Request Signing Secret',
        help='Only needed when the API key has request signing enabled. '
             'Every outgoing request is then signed with HMAC-SHA256.',
        config_parameter='seeitlive.signing_secret',
    )
    seeitlive_default_channel = fields.Selection(
        selection=[('sms', 'SMS'), ('email', 'Email'), ('link', 'Link only')],
        string='Default Invite Channel',
        default='sms',
        config_parameter='seeitlive.default_channel',
    )
    seeitlive_default_ttl_minutes = fields.Integer(
        string='Invite Lifetime (minutes)',
        default=60,
        help='How long the customer link stays valid. 1 to 1440.',
        config_parameter='seeitlive.default_ttl_minutes',
    )
    seeitlive_default_protection = fields.Selection(
        selection=[('none', 'None'), ('password', 'Password'), ('totp', 'Rotating code')],
        string='Default Guest Protection',
        default='none',
        config_parameter='seeitlive.default_protection',
    )
    # No config_parameter attribute: the base res.config.settings machinery
    # cannot round-trip "off" for a Boolean stored this way. On write it
    # calls ir.config_parameter.set_param(key, False), which UNLINKS the
    # row rather than storing it. On read it calls bool(value) on whatever
    # string IS stored — and bool('False') is True, since any non-empty
    # string is truthy. Both ends lose "unticked", so this field is
    # round-tripped by hand in get_values/set_values below instead, via
    # seeitlive.api's own _config_bool helper.
    seeitlive_attach_snapshots = fields.Boolean(
        string='Attach Snapshots to the Record',
        default=True,
    )
    # Not computed: res.config.settings is a TransientModel whose client
    # payload comes from default_get() -> get_values(), never from the ORM's
    # own compute machinery (there is no recordset round trip to the
    # browser that would re-run a compute). A @api.depends-computed,
    # non-stored field here would look right in every server-side test —
    # env['res.config.settings'].create({}).seeitlive_is_sandbox computes
    # fine in Python — while the actual settings form in the browser always
    # sees False, because the compute never runs on that path. So this is a
    # plain Boolean, assigned by hand in get_values() below, exactly like
    # seeitlive_enabled_model_ids already is.
    #
    # It only reflects the last SAVED key: get_values() runs on load, not
    # on every keystroke, so it will not live-update as an admin types a
    # new key before saving. That is fine here — an onchange would be more
    # machinery for a cosmetic label that is correct again the moment the
    # form is saved and reopened.
    seeitlive_is_sandbox = fields.Boolean(
        string='Sandbox Mode',
        readonly=True,
    )
    # Also not computed, for the same reason as seeitlive_is_sandbox above:
    # assigned by hand in get_values(). Purely informational — the exact
    # value an admin must paste into the API key's Security tab for the
    # live viewer to be allowed to embed in this instance.
    seeitlive_embed_origin = fields.Char(
        string='This Instance\'s Embed Origin',
        readonly=True,
        help='Add this exact value to your SeeItLive API key, under the key\'s '
             'Security tab, or the live viewer will not load inside Odoo.',
    )

    def action_seeitlive_test_connection(self):
        """Call the API once and report the result as a notification."""
        self.ensure_one()
        # Save first: the button is pressed before the settings form is applied,
        # so without this the ping would use the previously stored key.
        self.set_values()
        ok, message = self.env['seeitlive.api']._ping()
        if ok:
            # A key can authenticate perfectly and still be unable to show the
            # viewer here. Report that in the same click.
            embed_ok, embed_message = self.env['seeitlive.api']._check_embed_origin()
            message = '%s\n\n%s' % (message, embed_message)
            ok = embed_ok
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('SeeItLive') if ok else _('SeeItLive — connection failed'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': not ok,
            },
        }

    # Stored as a comma-separated list under seeitlive.enabled_models — the
    # source of truth. This Many2many is only the checkbox UI; get_values and
    # set_values translate between the two. The domain limits the choice to
    # models with a chatter, which is where a summary can be posted.
    seeitlive_enabled_model_ids = fields.Many2many(
        comodel_name='ir.model',
        relation='seeitlive_config_enabled_model_rel',
        column1='config_id',
        column2='model_id',
        string='Enabled Models',
        domain=[('is_mail_thread', '=', True)],
        help='Records of these models get a SeeItLive tab. Only models with a '
             'chatter are listed.',
    )

    # Name prefix of the generated inherited views, so a regeneration sweep
    # touches exactly the ones we created and never a hand-made view.
    _SEEITLIVE_VIEW_PREFIX = 'seeitlive.panel.dyn.'

    # Sentinel stored when an admin explicitly unchecks every model. Needed
    # because ir.config_parameter.get_param(key, default) is
    # ``_get_param(key) or default`` — an empty string is falsy, so storing
    # '' for "no models" would silently read back as the three-model
    # default on every subsequent get_values() (the same failure mode as
    # the Boolean above, just via the other end of the round trip). Not a
    # legal model name, so it can never collide with a real selection.
    _SEEITLIVE_NO_MODELS = '__none__'

    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()

        # See the field's comment above: this Boolean carries no
        # config_parameter attribute, so it is round-tripped here by hand,
        # through the same helper _ping()/etc. already trust for reading it.
        res['seeitlive_attach_snapshots'] = self.env['seeitlive.api']._config_bool(
            'attach_snapshots', True,
        )

        # Same reasoning as the field's comment: this Boolean is not
        # computed, so it has to be derived here, from the same
        # ir.config_parameter the API key itself is stored under.
        res['seeitlive_is_sandbox'] = (
            ICP.get_param('seeitlive.api_key') or ''
        ).startswith('sil_test_')

        # Same reasoning again: purely informational, derived fresh from
        # web.base.url on every load rather than computed or stored.
        res['seeitlive_embed_origin'] = self.env['seeitlive.api']._embed_origin()

        # A fresh install defaults to the three usual hosts; an explicit
        # empty selection (the admin unchecked everything) is marked with
        # the sentinel on the way in by set_values() below and decoded back
        # to "no models" here — see _SEEITLIVE_NO_MODELS for why a plain
        # empty string cannot carry that meaning through get_param.
        raw = ICP.get_param(
            'seeitlive.enabled_models', default='crm.lead,project.task,res.partner',
        )
        names = (
            [] if raw == self._SEEITLIVE_NO_MODELS
            else [n.strip() for n in (raw or '').split(',') if n.strip()]
        )
        models_found = (
            self.env['ir.model'].search([('model', 'in', names)])
            if names else self.env['ir.model']
        )
        res['seeitlive_enabled_model_ids'] = [(6, 0, models_found.ids)]
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env['ir.config_parameter'].sudo()

        # Store the literal string 'True'/'False' — never the Python value
        # False, which set_param() would unlink instead of storing. Read
        # back through _config_bool, never through bool(), for the same
        # reason.
        ICP.set_param(
            'seeitlive.attach_snapshots',
            'True' if self.seeitlive_attach_snapshots else 'False',
        )

        model_names = sorted(self.seeitlive_enabled_model_ids.mapped('model'))
        ICP.set_param(
            'seeitlive.enabled_models',
            ','.join(model_names) if model_names else self._SEEITLIVE_NO_MODELS,
        )
        self._sync_seeitlive_views(model_names)

    def _seeitlive_panel_arch(self, anchor):
        """Inherited-view arch adding the SeeItLive page.

        *anchor* is 'notebook' for a form that already has one, or 'sheet' for
        a form that does not — in which case we bring our own notebook rather
        than skipping the model, which is what apertur's plugin does.
        """
        page = (
            '<page string="SeeItLive" name="seeitlive_panel">'
            '<field name="seeitlive_active_session_id" widget="seeitlive_viewer" nolabel="1"/>'
            '</page>'
        )
        if anchor == 'notebook':
            return '<xpath expr="//notebook" position="inside">%s</xpath>' % page
        return '<xpath expr="//sheet" position="inside"><notebook>%s</notebook></xpath>' % page

    def _sync_seeitlive_views(self, model_names):
        """Regenerate the injected SeeItLive tab on every enabled model.

        Drop ours, then recreate the wanted set. Each attempt runs inside its
        own savepoint, so a form we cannot patch costs that one model its tab
        and nothing else.
        """
        View = self.env['ir.ui.view'].sudo()
        View.search([('name', '=like', self._SEEITLIVE_VIEW_PREFIX + '%')]).unlink()

        models_found = self.env['ir.model'].sudo().search([
            ('model', 'in', model_names),
            ('is_mail_thread', '=', True),
        ])
        for model in models_found:
            form = View.search([
                ('model', '=', model.model),
                ('type', '=', 'form'),
                ('mode', '=', 'primary'),
            ], order='priority, id', limit=1)
            if not form:
                _logger.warning('SeeItLive: no primary form view for %s', model.model)
                continue
            for anchor in ('notebook', 'sheet'):
                try:
                    with self.env.cr.savepoint():
                        View.create({
                            'name': '%s%s' % (self._SEEITLIVE_VIEW_PREFIX, model.model),
                            'model': model.model,
                            'inherit_id': form.id,
                            'priority': 99,
                            'arch': self._seeitlive_panel_arch(anchor),
                        })
                    break
                except Exception as exc:  # noqa: BLE001 — usually a missing anchor
                    _logger.info(
                        'SeeItLive: %s anchor did not fit %s (%s)',
                        anchor, model.model, exc,
                    )
            else:
                _logger.warning(
                    'SeeItLive: could not add the tab to %s; use the SeeItLive '
                    'menu to reach its sessions', model.model,
                )
