"""Give every chatter-enabled model the SeeItLive capability.

Extending mail.thread rather than naming models one by one means the addon
never has to depend on crm, project or helpdesk: whatever is installed and
has a chatter can host a live session, and the "Enabled Models" setting
decides where the tab actually appears.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .seeitlive_session import TERMINAL_STATES

_logger = logging.getLogger(__name__)

# Terminal for the panel's purposes: everything the API calls finished, plus
# our local 'error' — a failed session is not resumable either.
_FINISHED = TERMINAL_STATES + ('error',)


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    seeitlive_session_ids = fields.One2many(
        comodel_name='seeitlive.session',
        compute='_compute_seeitlive_sessions',
        string='SeeItLive Sessions',
    )
    seeitlive_active_session_id = fields.Many2one(
        comodel_name='seeitlive.session',
        compute='_compute_seeitlive_sessions',
        string='Live Session',
        help='The newest session that has not finished. The SeeItLive panel '
             'binds to this field: empty means "show the launcher".',
    )
    seeitlive_snapshot_count = fields.Integer(
        compute='_compute_seeitlive_snapshot_count',
        string='SeeItLive Snapshots',
    )

    def _compute_seeitlive_sessions(self):
        Session = self.env['seeitlive.session']
        for record in self:
            # An unsaved record carries a NewId, which no session can reference.
            if not isinstance(record.id, int):
                record.seeitlive_session_ids = Session
                record.seeitlive_active_session_id = Session
                continue
            sessions = Session.search([
                ('res_model', '=', record._name),
                ('res_id', '=', record.id),
            ])  # _order is create_date desc, so the newest comes first
            record.seeitlive_session_ids = sessions
            record.seeitlive_active_session_id = sessions.filtered(
                lambda s: s.state not in _FINISHED
            )[:1]

    def _compute_seeitlive_snapshot_count(self):
        Attachment = self.env['ir.attachment']
        for record in self:
            record.seeitlive_snapshot_count = Attachment.search_count([
                ('res_model', '=', record._name),
                ('res_id', '=', record.id),
                ('description', '=like', 'seeitlive:%'),
            ]) if isinstance(record.id, int) else 0

    # ------------------------------------------------------------------
    # Actions called from the panel
    # ------------------------------------------------------------------

    def action_seeitlive_start(self, options=None):
        """Start a session for this record. Returns the new session's id."""
        self.ensure_one()
        # action_start_for is an @api.model on a different model and never
        # touches a field of self, so no ORM ACL fires on its own — without
        # this, any user able to reach this RPC could start a session (and
        # spend the team's SeeItLive quota) against a record they cannot
        # otherwise write to.
        self.check_access_rights('write')
        self.check_access_rule('write')
        session = self.env['seeitlive.session'].action_start_for(
            self._name, self.id, options,
        )
        return session.id

    def action_seeitlive_open(self):
        """Open the newest session full screen (the panel's escape hatch)."""
        self.ensure_one()
        session = self.seeitlive_active_session_id or self.seeitlive_session_ids[:1]
        if not session:
            raise UserError(_('This record has no SeeItLive session yet.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Live view'),
            'res_model': 'seeitlive.session',
            'res_id': session.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # RPC surface for the panel widget
    # ------------------------------------------------------------------

    def rpc_seeitlive_recipients(self):
        """Where this record's customer can be reached.

        Resolved server-side rather than read off the form, so it works on any
        model without the panel having to know which fields that model happens
        to put in its arch.

        Called by the panel on every record it is bound to, including an
        unsaved one (an unsaved record's resId is False client-side) and,
        rarely, a stale one whose row was deleted after the form loaded.
        Neither can back a field read, so both return the empty payload
        rather than raising:
        - id False/0 -- falsy, and self.exists() cannot even be queried
          against it (Postgres has no integer = boolean operator), so that
          check is guarded behind the truthy-int test rather than run first;
        - a real but no-longer-existing id -- self.exists() rules it out
          before any field is touched, which is what would otherwise raise
          MissingError.
        """
        self.ensure_one()
        phones, emails = [], []
        if not self.id or not isinstance(self.id, int) or not self.exists():
            return {'phones': phones, 'emails': emails}

        def collect(record):
            # Mobile before phone: it is the number a customer can stream from.
            for field in ('mobile', 'phone'):
                if field in record._fields and record[field]:
                    phones.append(record[field])
            for field in ('email', 'email_from'):
                if field in record._fields and record[field]:
                    emails.append(record[field])

        collect(self)
        if 'partner_id' in self._fields and self.partner_id:
            collect(self.partner_id)

        return {
            'phones': list(dict.fromkeys(phones)),
            'emails': list(dict.fromkeys(emails)),
        }

    def rpc_seeitlive_defaults(self):
        """The configured channel and protection, for the launcher's initial state.

        Reads only ir.config_parameter, never a field of self, so — unlike
        rpc_seeitlive_recipients — it needs no existence guard: it is already
        safe to call on an unsaved record (id False) or a deleted one.
        """
        self.ensure_one()
        api_model = self.env['seeitlive.api']
        return {
            'channel': api_model._config('default_channel', 'sms'),
            'protection': api_model._config('default_protection', 'none'),
        }


class SeeItLiveSessionThreadInvalidation(models.Model):
    """Keep a record's SeeItLive fields fresh within the same transaction.

    seeitlive_session_ids/seeitlive_active_session_id are computed from
    seeitlive.session's res_model/res_id — a plain-field "reference" link,
    not a real Many2one — and the compute carries no @api.depends (it can't:
    Odoo's dependency graph has no edge for a cross-model field pair like
    this). Without this override, the One2many is seeded empty by the ORM
    the moment the chatter record is created and is never invalidated
    afterwards, so a session started later in the same env/transaction never
    shows up on it.

    Proven with:
        cd integrations/odoo && ./scripts/test.sh
    which failed, before this class existed, with:
        FAIL: TestEnabledModels.test_a_record_sees_its_own_sessions
        AssertionError: seeitlive.session(63,) not found in seeitlive.session()
    A shell repro on the test box (partner.seeitlive_session_ids read right
    after action_start_for(), with a direct env['seeitlive.session'].search()
    alongside it proving the row really exists) confirmed the field, not the
    data, was stale — and that env.invalidate_all()/invalidate_recordset()
    fixes it. This lives here (not in seeitlive_session.py, which Task 7
    owns) because it's purely a cache-freshness concern of the mail.thread
    fields this file adds.
    """
    _inherit = 'seeitlive.session'

    _SEEITLIVE_THREAD_FIELDS = ('seeitlive_session_ids', 'seeitlive_active_session_id')

    def _invalidate_seeitlive_thread(self):
        """Best-effort cache hint — never let this break the caller's write.

        res_model is a free-text field on a public @api.model method
        (action_start_for), so it can name any registered model, not only
        ones that mix in mail.thread. invalidate_recordset(fnames) does
        self._fields[fname] internally and raises KeyError for a model
        lacking those fields, so this only invalidates fields that actually
        exist on the target model, and — matching the log-and-continue
        idiom _sync_seeitlive_views already uses for view creation — any
        other failure here is swallowed and logged rather than propagated
        into seeitlive.session's create()/write().
        """
        for session in self:
            if not (session.res_model and session.res_id and session.res_model in self.env):
                continue
            try:
                record = self.env[session.res_model].browse(session.res_id)
                fnames = [f for f in self._SEEITLIVE_THREAD_FIELDS if f in record._fields]
                if fnames:
                    record.invalidate_recordset(fnames)
            except Exception as exc:  # noqa: BLE001 — a stale cache hint is cosmetic
                _logger.warning(
                    'SeeItLive: could not invalidate %s#%s after session %s (%s)',
                    session.res_model, session.res_id, session.id, exc,
                )

    @api.model_create_multi
    def create(self, vals_list):
        sessions = super().create(vals_list)
        sessions._invalidate_seeitlive_thread()
        return sessions

    def write(self, vals):
        result = super().write(vals)
        self._invalidate_seeitlive_thread()
        return result
