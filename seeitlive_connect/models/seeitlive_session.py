"""A SeeItLive session, as Odoo sees it.

What is stored here follows one rule: persist what cannot be re-derived,
mint what can. The invite URL is stored (the server keeps only a hash of its
token, so it exists nowhere else); the agent-view token is not (it is
re-mintable and expires in 120 minutes); the guest protection secret is not
(GET /sessions/:id/protection returns it on demand).
"""
import base64
import logging
from datetime import datetime, timezone

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .seeitlive_api import SeeItLiveApiError

_logger = logging.getLogger(__name__)

# Mirrors StreamSessionStatus in apps/api/prisma/schema.prisma, plus a local
# 'error' for a session whose creation or sync failed.
SESSION_STATES = [
    ('pending', 'Waiting for the customer'),
    ('ringing', 'Ringing'),
    ('live', 'Live'),
    ('ended', 'Ended'),
    ('expired', 'Expired'),
    ('cancelled', 'Cancelled'),
    ('error', 'Error'),
]

TERMINAL_STATES = ('ended', 'expired', 'cancelled')


class SeeItLiveSession(models.Model):
    _name = 'seeitlive.session'
    _description = 'SeeItLive Session'
    _order = 'create_date desc'

    name = fields.Char(string='Session ID', readonly=True, copy=False, index=True)
    invite_code = fields.Char(string='Invite Code', readonly=True, copy=False)
    invite_url = fields.Char(string='Customer Link', readonly=True, copy=False)
    invite_short_url = fields.Char(string='Short Link', readonly=True, copy=False)
    qr_png = fields.Binary(string='QR Code', compute='_compute_qr_png', store=True)

    channel = fields.Selection(
        selection=[('sms', 'SMS'), ('email', 'Email'), ('link', 'Link only')],
        string='Channel', readonly=True, default='link',
    )
    recipient = fields.Char(string='Recipient', readonly=True)
    sms_consent = fields.Boolean(
        string='SMS Consent Given', readonly=True,
        help='Recorded when the agent confirmed the customer agreed to receive '
             'a text message. Kept for compliance.',
    )
    protection = fields.Selection(
        selection=[('none', 'None'), ('password', 'Password'), ('totp', 'Rotating code')],
        string='Guest Protection', readonly=True, default='none',
        help='The secret itself is never stored here — it is fetched from '
             'SeeItLive when the agent needs to read it out.',
    )

    res_model = fields.Char(string='Related Model', readonly=True, index=True)
    res_id = fields.Integer(string='Related Record ID', readonly=True, index=True)

    state = fields.Selection(
        selection=SESSION_STATES, string='Status', default='pending', readonly=True, index=True,
    )
    expires_at = fields.Datetime(string='Link Expires', readonly=True)
    started_at = fields.Datetime(string='Started', readonly=True)
    ended_at = fields.Datetime(string='Ended', readonly=True)
    duration = fields.Float(
        string='Duration (minutes)', compute='_compute_duration', store=True,
    )
    snapshot_count = fields.Integer(string='Snapshots', readonly=True, default=0)
    synced_at = fields.Datetime(
        string='Results Synced', readonly=True,
        help='Set once the snapshots and the chatter summary have been posted. '
             'Guards against posting the summary twice.',
    )
    last_error = fields.Char(string='Last Error', readonly=True)
    created_by = fields.Many2one(
        'res.users', string='Agent', readonly=True, default=lambda self: self.env.uid,
    )

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------

    @api.depends('started_at', 'ended_at')
    def _compute_duration(self):
        for record in self:
            if record.started_at and record.ended_at:
                delta = record.ended_at - record.started_at
                record.duration = delta.total_seconds() / 60.0
            else:
                record.duration = 0.0

    @api.depends('invite_url', 'invite_short_url')
    def _compute_qr_png(self):
        """Render the customer link as a QR, server-side.

        Odoo ships python-qrcode and exposes it through ir.actions.report, so
        this needs no extra dependency and no browser round-trip to
        /report/barcode — which would put the invite URL in an access log.

        Stored: the panel's rpc_panel_state reads this field on every
        five-second poll, and the dependencies never change once a session
        is created, so an unstored field would re-render and re-send this
        1-2 KB base64 PNG on every single poll for no reason.
        """
        Report = self.env['ir.actions.report']
        for record in self:
            url = record.invite_short_url or record.invite_url
            if not url:
                record.qr_png = False
                continue
            try:
                record.qr_png = base64.b64encode(
                    Report.barcode('QR', url, width=256, height=256),
                )
            except Exception as exc:  # noqa: BLE001 — a missing QR is cosmetic
                _logger.warning('SeeItLive: could not render QR: %s', exc)
                record.qr_png = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _parse_api_datetime(self, value):
        """ISO-8601 from the API to a naive UTC datetime for an Odoo field."""
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        except (TypeError, ValueError):
            _logger.warning('SeeItLive: could not parse timestamp %r', value)
            return False
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @api.model
    def _default_options(self):
        api_model = self.env['seeitlive.api']
        return {
            'channel': api_model._config('default_channel', 'sms'),
            'protection': api_model._config('default_protection', 'none'),
            'ttl_minutes': int(api_model._config('default_ttl_minutes', '60') or 60),
        }

    # ------------------------------------------------------------------
    # Starting a session
    # ------------------------------------------------------------------

    @api.model
    def action_start_for(self, res_model, res_id, options=None):
        """Create a SeeItLive session for one record and store it.

        :param str res_model: e.g. ``crm.lead``
        :param int res_id: the record id
        :param dict options: ``channel``, ``recipient``, ``sms_consent``,
            ``protection``, ``ttl_minutes``; anything absent falls back to the
            configured default.
        :returns: the new ``seeitlive.session`` record
        """
        options = {**self._default_options(), **(options or {})}
        channel = options.get('channel') or 'link'
        recipient = (options.get('recipient') or '').strip()
        protection = options.get('protection') or 'none'
        ttl = int(options.get('ttl_minutes') or 60)

        if channel == 'sms' and not options.get('sms_consent'):
            # Enforced here as well as by the API, because the honest place to
            # refuse is before anything leaves the building.
            raise UserError(_(
                'Confirm the customer’s consent to receive a text message '
                'before sending an SMS invitation.'
            ))
        if channel in ('sms', 'email') and not recipient:
            raise UserError(_('Enter the customer’s %s before starting the session.') % (
                _('phone number') if channel == 'sms' else _('email address')
            ))

        payload = {
            'channel': channel,
            'ttlMinutes': max(1, min(ttl, 1440)),
            'metadata': {
                'odoo_model': res_model,
                'odoo_res_id': str(res_id),
                'odoo_db': self.env.cr.dbname,
            },
        }
        if channel == 'sms':
            payload['recipientPhone'] = recipient
            payload['smsConsent'] = True
            # Platform convention: SMS always uses the short link.
            payload['shortLink'] = True
        elif channel == 'email':
            payload['recipientEmail'] = recipient
        if protection != 'none':
            # The create schema has no "none" value — omitting the key is how
            # you say "unprotected".
            payload['protection'] = protection

        data = self.env['seeitlive.api']._request('POST', '/sessions', payload)

        return self.create({
            'name': data.get('id', ''),
            'invite_code': data.get('inviteCode', ''),
            'invite_url': data.get('inviteUrl', ''),
            'invite_short_url': data.get('inviteShortUrl') or '',
            'channel': channel,
            'recipient': recipient,
            'sms_consent': channel == 'sms',
            'protection': data.get('protection') or 'none',
            'res_model': res_model,
            'res_id': int(res_id),
            'state': data.get('status') or 'pending',
            'expires_at': self._parse_api_datetime(data.get('expiresAt')),
        })

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def action_open_record(self):
        """Open the record this session was started from."""
        self.ensure_one()
        if not self.res_model or not self.res_id:
            raise UserError(_('This session is not linked to a record.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Live status
    # ------------------------------------------------------------------

    def _refresh_status(self):
        """Pull the lifecycle from the API. Never raises — not on a network
        blip, not on a non-JSON reply, not on a status this addon doesn't
        know yet, and not on a write-time ORM error.

        The panel calls this every five seconds while a session is open, and
        a future cron sweep will call it across many sessions at once, so no
        failure here may propagate: one bad row must not throw a dialog in an
        agent's face mid-call, or poison the rest of a sweep. Everything is
        logged and, where possible, recorded on ``last_error`` instead.
        """
        self.ensure_one()
        if not self.name or self.state in TERMINAL_STATES or self.state == 'error':
            return
        try:
            data = self.env['seeitlive.api']._request(
                'GET', '/sessions/%s/status' % self.name,
            )
            values = {
                'started_at': self._parse_api_datetime(data.get('startedAt')) or self.started_at,
                'ended_at': self._parse_api_datetime(data.get('endedAt')) or self.ended_at,
                'last_error': False,
            }
            status = data.get('status')
            known_states = dict(SESSION_STATES)
            if status and status in known_states:
                values['state'] = status
            elif status:
                # The server's status enum can drift ahead of this addon's
                # own SESSION_STATES between releases. Degrade to "don't know
                # that one yet" rather than blow up the Selection write.
                _logger.warning(
                    'SeeItLive: session %s returned an unrecognised status %r; '
                    'keeping %r', self.name, status, self.state,
                )
            self.write(values)
        except UserError as exc:
            self.write({'last_error': str(exc)[:255]})
        except Exception as exc:  # noqa: BLE001 — this must never raise; see docstring
            _logger.warning('SeeItLive: could not refresh status for %s: %s', self.name, exc)
            self.write({'last_error': str(exc)[:255]})

    def _fetch_protection(self):
        """The secret the agent reads out, fetched fresh every time. Never
        raises — any failure (network, a non-JSON reply) returns ``{}``
        rather than propagating, the same contract as ``_refresh_status``.

        Never stored: a rotating code is stale within 30 seconds, and a stored
        password is a secret at rest that buys nothing — the API returns it on
        demand to anyone already holding the key.

        Only asked for while the session is still ``pending``: the API
        refuses to move a protected session past that state until the guest
        has already answered the challenge (``invitations/claim`` checks the
        unlock token before writing ``ringing``), so by the time state is
        anything else there is nothing left to read out — polling
        ``/protection`` every five seconds for the rest of the call would
        just re-fetch a value nobody needs anymore.
        """
        self.ensure_one()
        if self.protection == 'none' or not self.name or self.state != 'pending':
            return {}
        try:
            data = self.env['seeitlive.api']._request(
                'GET', '/sessions/%s/protection' % self.name,
            )
        except UserError:
            return {}
        except Exception as exc:  # noqa: BLE001 — this must never raise; see docstring
            _logger.warning('SeeItLive: could not fetch protection for %s: %s', self.name, exc)
            return {}
        return {
            'type': data.get('type'),
            'password': data.get('password'),
            'code': data.get('code'),
            'seconds_remaining': data.get('secondsRemaining'),
            'locked': bool(data.get('locked')),
        }

    def rpc_panel_state(self):
        """One render's worth of state for the panel widget."""
        self.ensure_one()
        self._refresh_status()
        return {
            'id': self.id,
            'name': self.name,
            'state': self.state,
            'channel': self.channel,
            'recipient': self.recipient,
            'share_url': self.invite_short_url or self.invite_url,
            'invite_url': self.invite_url,
            'qr_data_url': (
                'data:image/png;base64,%s' % self.qr_png.decode()
                if self.qr_png else ''
            ),
            'protection': self._fetch_protection(),
            'snapshot_count': self.snapshot_count,
            'is_finished': self.state in TERMINAL_STATES or self.state == 'error',
            # Computed server-side, deliberately: the agent's browser clock can
            # disagree with the API's, and the whole point is to offer
            # re-share exactly when the link is actually dead. Only meaningful
            # before the customer has claimed the invite — 'ringing'/'live'
            # prove the link worked regardless of what this record's own
            # (write-time) expires_at snapshot says.
            'expired': bool(
                self.expires_at and self.state == 'pending'
                and self.expires_at < fields.Datetime.now()
            ),
            'error': self.last_error or '',
        }

    def rpc_agent_view_url(self):
        """Mint a fresh agent-view URL for the embedded iframe.

        Deliberately not stored. The token grants annotate and snapshot rights
        and expires after 120 minutes; minting on demand means the panel works
        the day after the call and for a second agent, and the ERP database
        never holds the credential.
        """
        self.ensure_one()
        if not self.name:
            raise UserError(_('This session was never created on SeeItLive.'))
        data = self.env['seeitlive.api']._request(
            'POST',
            '/sessions/%s/agent-token' % self.name,
            {'label': 'Odoo — %s %s' % (self.res_model or '', self.res_id or '')},
        )
        return data.get('agentViewUrl') or ''

    # ------------------------------------------------------------------
    # Reviving an invitation
    # ------------------------------------------------------------------

    def action_reshare(self, channel=None, recipient=None, sms_consent=False,
                       mode='revive'):
        """Revive this session's expired invitation, or issue a fresh link.

        `mode='revive'` extends the SAME link and code in place, so a message
        the customer already has starts working again. `mode='new'` issues a
        fresh link and kills the old one.

        Omitted channel/recipient are left out of the payload entirely —
        absent means "reuse what was recorded", which is not the same as
        sending null.
        """
        self.ensure_one()
        payload = {'mode': mode}
        if channel:
            # Refuse locally rather than let the API reject it: the agent gets
            # an actionable message instead of a 400.
            if channel == 'sms' and not sms_consent:
                raise UserError(_(
                    'Texting the customer needs their confirmation that they '
                    'agreed to receive a text message.'
                ))
            payload['channel'] = channel
            if channel == 'sms':
                payload['smsConsent'] = True
        if recipient:
            payload['recipient'] = recipient

        data = self.env['seeitlive.api']._request(
            'POST', '/sessions/%s/reshare' % self.name, payload,
        ) or {}
        # The reshare response is authoritative for what just changed. It never
        # returns the raw invite_url (the plaintext token cannot be rebuilt from
        # it by design — see the API's own comment on this route), only the
        # short link, and only when one exists or was just minted. But it
        # always returns a fresh expiresAt. Persist both: without this,
        # rpc_panel_state's 'expired' below would stay true forever after a
        # successful revive, and the panel would keep showing a short link
        # that has already been replaced.
        stored = {}
        if data.get('expiresAt'):
            stored['expires_at'] = self._parse_api_datetime(data.get('expiresAt'))
        if data.get('inviteShortUrl'):
            stored['invite_short_url'] = data.get('inviteShortUrl')
        # 'rotated' means mode:'new' minted a fresh code/token: the invite_url
        # stored at creation time pointed at the OLD one, which the API just
        # killed, and the response never carries a replacement (same reason
        # invite_url is never set from an API reply anywhere else in this
        # method — see the comment above). Left alone, the form's read-only
        # 'Customer Link' field would keep rendering a dead link as if it
        # still worked, and an agent copying it into an email would hand the
        # customer a revoked URL. Clearing it is the only safe move; there is
        # nothing to replace it WITH.
        if data.get('rotated'):
            stored['invite_url'] = False
        if stored:
            self.write(stored)
        # Refresh what the panel shows next: the state may have moved (e.g.
        # out of a stale 'expired').
        self._refresh_status()
        return data

    def action_reshare_button(self):
        """Entry point for the seeitlive.session form's header button.

        Odoo's doActionButton treats a truthy dict returned from a
        type="object" button as an action descriptor and requires a 'type'
        key on it — action_reshare deliberately returns the raw API response
        dict instead (programmatic callers want the payload, and Task 5's
        test_mode_new_is_passed_through asserts on result['rotated']), which
        is not a valid descriptor and would break the button. action_sync
        avoids exactly this by returning True; mirror that here rather than
        changing action_reshare's own contract.
        """
        self.ensure_one()
        self.action_reshare()
        return True

    # ------------------------------------------------------------------
    # Bringing the results home
    # ------------------------------------------------------------------

    def action_sync(self):
        """Pull status and snapshots, and post the summary once."""
        for record in self:
            record._sync_one()
        return True

    def _sync_one(self, from_cron=False):
        self.ensure_one()
        # A double-clicked Sync now, the cron landing in the same window as
        # the panel's end-of-call sync, or two agents on one record must not
        # race past this line together: state is already terminal by the
        # time a second caller gets here (the panel commits the transition
        # in its own earlier RPC), so nothing below is safe to read twice
        # unserialized. This lock is held for the rest of the transaction.
        self.env.cr.execute(
            'SELECT id FROM seeitlive_session WHERE id = %s FOR UPDATE', (self.id,),
        )
        # _cron_sync's candidates recordset prefetches stored fields for
        # every row the moment any field is touched on the first-processed
        # session — including synced_at for sessions this loop has not
        # locked yet. If a concurrent caller committed a sync on one of
        # those rows in that window, that prefetch is now stale. Drop just
        # this record's cached synced_at (not the whole batch's cache) so
        # _post_summary's guard below reads the post-lock truth.
        self.invalidate_recordset(['synced_at'])
        self._refresh_status()
        if not self.name:
            return
        attachments, incomplete = self.env['ir.attachment'], False
        # A session with no started_at was never opened by the customer and
        # cannot have produced a snapshot. From the cron sweep — which
        # revisits every unsynced session every 5 minutes for up to 7 days —
        # fetching the listing anyway is ~4,000 wasted API calls (and as
        # many row writes, via _ingest_snapshots' own write below) per
        # abandoned invite before the cutoff gives up. "Sync now" is a
        # single deliberate click, not a forever-loop — an agent pressing it
        # on a session stuck at "pending" gets exactly what they asked for,
        # so only the cron path is guarded here.
        skip_snapshots = from_cron and not self.started_at
        if not skip_snapshots and self.env['seeitlive.api']._config_bool('attach_snapshots', True):
            attachments, incomplete = self._ingest_snapshots()
        if self.state in TERMINAL_STATES:
            self._post_summary(attachments, incomplete=incomplete)

    def _ingest_snapshots(self):
        """Attach every snapshot not already on the record.

        Idempotent by construction: each attachment carries
        ``description = 'seeitlive:<snapshot id>'``, so the cron, the Sync
        button and the end-of-call trigger can all run without duplicating a
        thing. Returns the attachments created by *this* call, which is what
        the chatter summary should show.
        """
        self.ensure_one()
        api_model = self.env['seeitlive.api']
        Attachment = self.env['ir.attachment']
        created = Attachment

        try:
            listing = api_model._request('GET', '/sessions/%s/snapshots' % self.name) or []
        except SeeItLiveApiError as exc:
            if exc.status in (402, 403):
                # A plan gate is a permanent state, not a transient failure —
                # unlike everything else caught here, retrying it every 5
                # minutes for the whole 7-day window would never succeed.
                # Let the summary post with zero snapshots instead of
                # holding the session "incomplete" forever.
                self.write({
                    'snapshot_count': 0,
                    'last_error': _(
                        'Snapshots are not included in the current SeeItLive '
                        'plan, so none were fetched for this session.'
                    ),
                })
                return created, False
            self.write({'last_error': str(exc)[:255]})
            return created, True
        except UserError as exc:
            self.write({'last_error': str(exc)[:255]})
            return created, True
        except Exception as exc:  # noqa: BLE001 — this must never raise; see below
            # A stale base URL or a misconfigured proxy can serve a non-JSON
            # 2xx; resp.json() then raises a ValueError subclass, not a
            # UserError. Sync now must not blow up in the agent's face for
            # that — same contract _refresh_status already promises.
            _logger.warning('SeeItLive: could not list snapshots for %s: %s', self.name, exc)
            self.write({'last_error': str(exc)[:255]})
            return created, True

        failed = False
        for item in listing:
            snapshot_id = item.get('id')
            if not snapshot_id:
                continue
            marker = 'seeitlive:%s' % snapshot_id
            if Attachment.search_count([
                ('res_model', '=', self.res_model),
                ('res_id', '=', self.res_id),
                ('description', '=', marker),
            ]):
                continue
            try:
                png = api_model._request('GET', '/snapshots/%s' % snapshot_id, raw=True)
            except UserError as exc:
                self.write({'last_error': str(exc)[:255]})
                failed = True
                continue
            created |= Attachment.create({
                'name': 'seeitlive-%s.png' % snapshot_id,
                'datas': base64.b64encode(png),
                'mimetype': 'image/png',
                'res_model': self.res_model,
                'res_id': self.res_id,
                'description': marker,
            })

        self.write({
            'snapshot_count': len(listing),
            **({} if failed else {'last_error': False}),
        })
        # Odoo models use __slots__, so the "did every download succeed?" flag
        # cannot be stashed on self — it is returned alongside the attachments.
        return created, failed

    def _post_summary(self, attachments, incomplete=False):
        """One internal note on the source record, the first time we finish.

        A note, not a message: a customer's broken boiler should not be
        emailed back to them. ``synced_at`` is the guard — the cron, the button
        and the panel all call this.
        """
        self.ensure_one()
        # A partial download must not mark the session done, or the sweep would
        # never come back for the rest.
        if self.synced_at or incomplete:
            return
        if not self.res_model or not self.res_id:
            return
        record = self.env[self.res_model].browse(self.res_id).exists()
        if not record or not hasattr(record, 'message_post'):
            return

        minutes = int(self.duration)
        seconds = int(round((self.duration - minutes) * 60))
        # Wrap the TRANSLATED TEMPLATE in Markup before the %, not the other
        # way around. Markup(template) % {...} has Markup's __mod__ escape
        # each interpolated value as it goes in; str(template) % {...} would
        # produce a plain str that message_post() then escapes WHOLESALE
        # (its own contract: "str content will be escaped, Markup for html
        # body"), turning our own <p>/<b>/<br/> right back into visible
        # &lt;p&gt; text. Wrapping the already-substituted result afterwards
        # would dodge that, but would also mark whatever was interpolated
        # into it as trusted HTML unescaped — harmless today since these
        # values are ints, but the wrong order to establish as the pattern.
        body = Markup(_(
            '<p><b>SeeItLive</b> — live view session ended.<br/>'
            'Duration: %(minutes)s min %(seconds)s s · '
            'Snapshots: %(count)s</p>'
        )) % {
            'minutes': minutes,
            'seconds': seconds,
            'count': self.snapshot_count,
        }
        record.message_post(
            body=body,
            attachment_ids=attachments.ids,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        self.write({'synced_at': fields.Datetime.now()})

    @api.model
    def _cron_sync(self, limit=50):
        """Reconcile whatever the panel did not finish.

        Covers the agent who closed the tab mid-call, the browser that lost the
        network, and every Odoo that is not reachable from the internet — which
        is most self-hosted ones, and the reason this addon polls rather than
        waiting for a webhook.
        """
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=7)
        candidates = self.search([
            ('name', '!=', False),
            ('synced_at', '=', False),
            ('create_date', '>=', cutoff),
        ], limit=limit)
        for session in candidates:
            try:
                with self.env.cr.savepoint():
                    session._sync_one(from_cron=True)
                # Commit right after the savepoint exits cleanly, not inside
                # it: each session can need several serialized HTTP round
                # trips while holding its row lock (see _sync_one), and a
                # sweep of up to `limit` sessions must not hold every one it
                # has already finished locked for the rest of the batch — a
                # "Sync now" click or the panel's end-of-call RPC on any of
                # them would otherwise queue behind the whole cron run.
                # Sessions are independent and already savepoint-isolated,
                # so a partially-completed sweep is fine: the next tick
                # picks up whatever is left.
                self.env.cr.commit()
            except Exception:  # noqa: BLE001 — one bad session must not stop the sweep
                # If the commit itself is what failed, Postgres leaves this
                # cursor's transaction aborted: every statement after it —
                # including the trailing stale.write() below — would raise
                # the same error until this runs. Safe unconditionally: each
                # session already committed on success just above, so there
                # is nothing earlier in this sweep left to lose.
                self.env.cr.rollback()
                _logger.exception('SeeItLive: sync failed for session %s', session.name)

        # A session that ages out of the window above (e.g. after a partial
        # download failure) is never retried by this cron again — "Sync now"
        # still reaches it, but silently. Say so once; the domain below
        # excludes anything already carrying this exact message, so re-runs
        # do not keep rewriting it every five minutes.
        give_up_message = _(
            'SeeItLive could not sync this session within 7 days and has '
            'stopped retrying automatically. Press "Sync now" to bring its '
            'results in.'
        )
        stale = self.search([
            ('name', '!=', False),
            ('synced_at', '=', False),
            ('create_date', '<', cutoff),
            ('last_error', '!=', give_up_message),
        ])
        if stale:
            stale.write({'last_error': give_up_message})
        return True
