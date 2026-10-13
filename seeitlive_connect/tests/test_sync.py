import base64
from json import JSONDecodeError
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.seeitlive_connect.models.seeitlive_api import SeeItLiveApiError

# 1x1 transparent PNG.
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)

CREATE_RESPONSE = {
    'id': 'ses_abc123', 'status': 'pending', 'inviteCode': 'Ab3dEf',
    'inviteUrl': 'https://seeitlive.io/u/Ab3dEf#tok', 'inviteShortUrl': None,
    'agentViewUrl': 'https://seeitlive.io/a/Zz#t', 'protection': 'none', 'expiresAt': None,
}

ENDED_STATUS = {
    'id': 'ses_abc123', 'status': 'ended',
    'startedAt': '2026-09-02T18:00:00.000Z',
    'endedAt': '2026-09-02T18:04:12.000Z',
}

SNAPSHOT_LIST = [
    {'id': 'snap_1', 'width': 1280, 'height': 720, 'capturedAt': '2026-09-02T18:01:00.000Z',
     'url': '/api/v1/snapshots/snap_1'},
    {'id': 'snap_2', 'width': 1280, 'height': 720, 'capturedAt': '2026-09-02T18:02:00.000Z',
     'url': '/api/v1/snapshots/snap_2'},
]


def api_responses(status=None, listing=None):
    """A _request side effect covering the three calls a sync makes."""
    def respond(_self, method, path, payload=None, raw=False):
        if path.endswith('/status'):
            return dict(status or ENDED_STATUS)
        if path.endswith('/snapshots'):
            return list(listing if listing is not None else SNAPSHOT_LIST)
        if path.startswith('/snapshots/'):
            return PNG
        if path.endswith('/protection'):
            return {}
        raise AssertionError('unexpected call %s %s' % (method, path))
    return respond


@tagged('post_install', '-at_install')
class TestSync(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.api_key', 'sil_live_deadbeef')
        self.partner = self.env['res.partner'].create({'name': 'Ada'})
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            request.return_value = dict(CREATE_RESPONSE)
            self.session = self.env['seeitlive.session'].action_start_for(
                'res.partner', self.partner.id, {'channel': 'link'},
            )

    def _attachments(self):
        return self.env['ir.attachment'].search([
            ('res_model', '=', 'res.partner'),
            ('res_id', '=', self.partner.id),
            ('description', '=like', 'seeitlive:%'),
        ])

    def _sync(self, **kwargs):
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=api_responses(**kwargs)):
            self.session.action_sync()

    def test_each_snapshot_lands_as_an_attachment_on_the_record(self):
        self._sync()
        attachments = self._attachments()
        self.assertEqual(len(attachments), 2)
        self.assertEqual(
            sorted(attachments.mapped('description')),
            ['seeitlive:snap_1', 'seeitlive:snap_2'],
        )
        self.assertEqual(attachments[0].mimetype, 'image/png')

    def test_syncing_twice_does_not_duplicate_anything(self):
        """The cron, the Sync button and the end-of-call trigger can all fire
        for the same session; the marker makes that harmless."""
        self._sync()
        self._sync()
        self.assertEqual(len(self._attachments()), 2)

    def test_the_summary_is_posted_once_and_only_once(self):
        self._sync()
        self._sync()
        messages = self.partner.message_ids.filtered(lambda m: 'SeeItLive' in (m.body or ''))
        self.assertEqual(len(messages), 1)
        self.assertIn('4 min', messages.body)

    def test_the_summary_is_an_internal_note(self):
        """A customer's broken boiler should not be emailed back to them."""
        self._sync()
        message = self.partner.message_ids.filtered(lambda m: 'SeeItLive' in (m.body or ''))
        self.assertEqual(message.subtype_id, self.env.ref('mail.mt_note'))

    def test_sync_records_the_lifecycle_and_marks_itself_done(self):
        self._sync()
        self.assertEqual(self.session.state, 'ended')
        self.assertEqual(self.session.snapshot_count, 2)
        self.assertTrue(self.session.synced_at)
        self.assertAlmostEqual(self.session.duration, 4.2, places=1)

    def test_disabling_attachments_still_posts_the_summary(self):
        self.env['ir.config_parameter'].sudo().set_param('seeitlive.attach_snapshots', 'False')
        self._sync()
        self.assertFalse(self._attachments())
        self.assertTrue(self.session.synced_at)

    def test_a_session_with_no_snapshots_still_closes_cleanly(self):
        self._sync(listing=[])
        self.assertTrue(self.session.synced_at)
        self.assertEqual(self.session.snapshot_count, 0)

    def test_a_download_failure_leaves_the_session_resyncable(self):
        def flaky(_self, method, path, payload=None, raw=False):
            if path.endswith('/status'):
                return dict(ENDED_STATUS)
            if path.endswith('/snapshots'):
                return list(SNAPSHOT_LIST)
            raise UserError('storage hiccup')

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=flaky):
            self.session.action_sync()
        self.assertFalse(self._attachments())
        self.assertTrue(self.session.last_error)
        self.assertFalse(self.session.synced_at)   # so the cron tries again

        self._sync()
        self.assertEqual(len(self._attachments()), 2)
        self.assertTrue(self.session.synced_at)

    def test_the_cron_picks_up_a_session_the_panel_never_finished(self):
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=api_responses()):
            # _cron_sync commits right after each session so a long sweep
            # does not hold every already-finished session's row lock for
            # the rest of the batch (see _cron_sync). A real commit is
            # mocked out here because TransactionCase's own test isolation
            # is itself built on a real SAVEPOINT that a genuine COMMIT
            # would destroy — not something this addon's code should, or
            # safely can, work around for the sake of its own tests.
            with patch.object(type(self.env.cr), 'commit') as commit:
                self.env['seeitlive.session']._cron_sync()
        commit.assert_called_once()
        self.assertTrue(self.session.synced_at)
        self.assertEqual(len(self._attachments()), 2)

    def test_the_cron_leaves_an_already_synced_session_alone(self):
        self._sync()
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            self.env['seeitlive.session']._cron_sync()
        request.assert_not_called()

    def test_sync_takes_a_row_lock_on_the_session(self):
        """A double-clicked Sync now, the cron landing in the same window as
        the panel's end-of-call sync, or two agents on one record must
        serialize through an explicit row lock — the only thing standing
        between that and two attachments or two chatter notes, since state
        is already terminal by the time a second caller gets here.

        TransactionCase cannot exercise two real concurrent transactions, so
        this only proves the lock is actually issued — a future refactor
        that drops it should fail this test loudly.
        """
        cursor_cls = type(self.env.cr)
        original_execute = cursor_cls.execute
        queries = []

        def spy(cr_self, query, *args, **kwargs):
            if isinstance(query, str):
                queries.append(query)
            return original_execute(cr_self, query, *args, **kwargs)

        with patch.object(cursor_cls, 'execute', spy):
            self._sync()

        locking = [q for q in queries if 'seeitlive_session' in q and 'FOR UPDATE' in q.upper()]
        self.assertTrue(locking, 'expected a SELECT ... FOR UPDATE on seeitlive_session')

    def test_a_malformed_listing_reply_does_not_raise_through_the_button(self):
        """A stale base URL or misconfigured proxy can serve a 2xx HTML page
        for the snapshot listing; resp.json() then raises a ValueError
        subclass, not a UserError. Sync now must not blow up in the agent's
        face for that — same contract _refresh_status already promises."""
        def bad_listing(_self, method, path, payload=None, raw=False):
            if path.endswith('/status'):
                return dict(ENDED_STATUS)
            if path.endswith('/snapshots'):
                raise JSONDecodeError('Expecting value', '', 0)
            raise AssertionError('unexpected call %s %s' % (method, path))

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=bad_listing):
            self.session.action_sync()  # must not raise

        self.assertFalse(self._attachments())
        self.assertIn('Expecting value', self.session.last_error)
        self.assertFalse(self.session.synced_at)   # so the cron/button can retry

    def test_a_session_past_the_sweep_window_is_flagged_once_not_every_run(self):
        """The cron's 7-day cutoff is bounded on purpose (an unbounded sweep
        would poll dead rows forever), but a session that ages out unsynced
        must not vanish silently — Sync now still reaches it. The flag must
        be stamped once, not rewritten every five minutes."""
        self.env.cr.execute(
            "UPDATE seeitlive_session SET create_date = create_date - interval '8 days' "
            "WHERE id = %s", (self.session.id,),
        )
        self.session.invalidate_recordset(['create_date'])

        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            self.env['seeitlive.session']._cron_sync()
        request.assert_not_called()  # aged out of the sweep window entirely
        self.assertTrue(self.session.last_error)
        stamped_write_date = self.session.write_date

        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            self.env['seeitlive.session']._cron_sync()
        request.assert_not_called()
        self.assertEqual(self.session.write_date, stamped_write_date)

    def test_the_synced_at_guard_survives_a_stale_prefetch(self):
        """Simulates the exact mechanism a batch cron sweep can hit: the
        ORM prefetches stored fields for the whole candidates recordset the
        moment any field is touched on the first-processed session —
        including synced_at for sessions this loop has not locked yet. If a
        concurrent caller committed a sync on one of those rows in that
        window, that prefetch is now a lie.

        A live two-transaction race cannot be expressed in TransactionCase,
        so this proves the mechanism instead of the race itself: prime the
        cache with the pre-lock (unsynced) value, mutate the row directly in
        SQL — bypassing the ORM and its cache, exactly as a different
        transaction's commit would be invisible to it — then confirm the
        guard reads the post-lock truth rather than the stale cache.
        """
        # Prime the cache: this read is legitimately False right now, the
        # same way a batch prefetch would capture it before this row's turn.
        self.assertFalse(self.session.synced_at)

        # A concurrent caller commits a full sync on this very row in the
        # window between that prefetch and this row's own lock. Simulated
        # with a direct SQL write, bypassing the ORM (and therefore its
        # cache) exactly as a different transaction's commit would be.
        # state is left alone: the mocked _refresh_status() below carries
        # this session to 'ended' on its own, the same way a normal sync
        # would — only synced_at's cache is deliberately corrupted here.
        self.env.cr.execute(
            "UPDATE seeitlive_session SET synced_at = now() WHERE id = %s",
            (self.session.id,),
        )

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=api_responses()):
            self.session._sync_one()

        messages = self.partner.message_ids.filtered(lambda m: 'SeeItLive' in (m.body or ''))
        self.assertFalse(
            messages,
            'the guard must see the post-lock synced_at, not the stale prefetch — '
            'trusting the cache here would post a duplicate note',
        )

    def test_a_402_on_the_listing_posts_the_summary_once_with_zero_snapshots(self):
        """A plan-gate 402 is permanent, not transient — unlike every other
        failure this method handles. Before this fix, incomplete stayed
        True forever, the summary never posted, and the cron retried this
        exact session every 5 minutes for 7 days for nothing."""
        def gated(_self, method, path, payload=None, raw=False):
            if path.endswith('/status'):
                return dict(ENDED_STATUS)
            if path.endswith('/snapshots'):
                raise SeeItLiveApiError('plan does not include snapshots', status=402)
            raise AssertionError('unexpected call %s %s' % (method, path))

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=gated):
            self.session.action_sync()

        self.assertFalse(self._attachments())
        self.assertEqual(self.session.snapshot_count, 0)
        self.assertTrue(self.session.synced_at)
        self.assertTrue(self.session.last_error)

        messages = self.partner.message_ids.filtered(lambda m: 'SeeItLive' in (m.body or ''))
        self.assertEqual(len(messages), 1)

        # synced_at is now set, so a later cron pass leaves it alone —
        # proving this is a one-time close, not a session still "stuck".
        with patch.object(type(self.env['seeitlive.api']), '_request') as request:
            self.env['seeitlive.session']._cron_sync()
        request.assert_not_called()

    def test_a_403_on_the_listing_is_treated_the_same_as_a_402(self):
        def gated(_self, method, path, payload=None, raw=False):
            if path.endswith('/status'):
                return dict(ENDED_STATUS)
            if path.endswith('/snapshots'):
                raise SeeItLiveApiError('forbidden', status=403)
            raise AssertionError('unexpected call %s %s' % (method, path))

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=gated):
            self.session.action_sync()

        self.assertEqual(self.session.snapshot_count, 0)
        self.assertTrue(self.session.synced_at)

    def test_the_cron_never_lists_snapshots_for_a_session_that_never_started(self):
        """An invite nobody ever opened has no started_at and cannot have
        produced a snapshot. The cron revisits every unsynced session every
        5 minutes for up to 7 days — for an abandoned invite that is
        ~4,000 wasted /snapshots calls (and as many row writes) before the
        cutoff gives up."""
        def respond(_self, method, path, payload=None, raw=False):
            if path.endswith('/status'):
                return {'id': 'ses_abc123', 'status': 'pending'}
            return []  # a call here would mean the guard let a listing through

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=respond) as request:
            with patch.object(type(self.env.cr), 'commit'):
                self.env['seeitlive.session']._cron_sync()

        called_paths = [c.args[2] for c in request.call_args_list]
        self.assertTrue(any(p.endswith('/status') for p in called_paths))
        self.assertFalse(any(p.endswith('/snapshots') for p in called_paths))

    def test_sync_now_still_lists_snapshots_for_a_session_that_never_started(self):
        """Unlike the cron sweep, a manually clicked 'Sync now' keeps
        pulling even before started_at — it is a single deliberate click,
        not a forever-loop, so there is no reason to withhold it."""
        def respond(_self, method, path, payload=None, raw=False):
            if path.endswith('/status'):
                return {'id': 'ses_abc123', 'status': 'pending'}
            if path.endswith('/snapshots'):
                return []
            raise AssertionError('unexpected call %s %s' % (method, path))

        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=respond) as request:
            self.session.action_sync()

        called_paths = [c.args[2] for c in request.call_args_list]
        self.assertTrue(any(p.endswith('/snapshots') for p in called_paths))

    def test_a_failed_commit_is_rolled_back_not_left_to_poison_the_cursor(self):
        """A failed cr.commit() leaves Postgres with an aborted transaction;
        every following statement on that cursor — including the trailing
        stale.write() below it — would fail identically without an
        explicit rollback. Proven at the level reachable without a real
        commit (which would destroy TransactionCase's own savepoint-based
        isolation, same reasoning as the cron test above): the except block
        must call rollback whenever anything in the try raises, commit
        included."""
        with patch.object(type(self.env['seeitlive.api']), '_request',
                          autospec=True, side_effect=api_responses()):
            with patch.object(type(self.env.cr), 'commit', side_effect=Exception('boom')):
                with patch.object(type(self.env.cr), 'rollback') as rollback:
                    self.env['seeitlive.session']._cron_sync()  # must not raise
        rollback.assert_called_once()
