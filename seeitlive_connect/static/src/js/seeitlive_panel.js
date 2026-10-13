/** @odoo-module **/

import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * The SeeItLive panel: one piece of the form that is, in turn, the launcher,
 * the waiting room, and the live viewer.
 *
 * It binds to `seeitlive_active_session_id` — empty means "show the launcher".
 * Every call goes through the ORM service, so the API key stays on the server;
 * the browser never sees it. `useService("orm")` is stable across Odoo 17-19,
 * unlike the "rpc" service, which 18 removed.
 */
export class SeeItLivePanel extends Component {
    static template = "seeitlive_connect.Panel";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");

        this.state = useState({
            phase: "idle",          // idle | starting | waiting | live
            channel: "sms",
            recipient: "",
            recipients: { phones: [], emails: [] },
            consent: false,
            protection: "none",
            sessionId: null,
            session: null,          // the last rpc_panel_state payload
            agentViewUrl: null,
            viewerError: "",        // set when minting the viewer URL fails
            reshareOpen: false,      // the re-share form is showing
            reshareChannel: null,    // null = reuse the recorded channel
            reshareRecipient: "",
            reshareConsent: false,
            reshareBusy: false,
        });

        onWillStart(async () => {
            // An unsaved record has no resId yet (it is false client-side)
            // and no session can exist for it either: show the launcher
            // with nothing prefilled rather than dispatching an RPC with a
            // meaningless id. Mirrors the guard _compute_seeitlive_sessions
            // already applies for the same NewId/unsaved case.
            if (!this.resId) {
                return;
            }

            if (this.props.record.resModel !== "seeitlive.session") {
                // launcher data: only meaningful on the source record
                const [recipients, defaults] = await Promise.all([
                    this.orm.call(this.resModel, "rpc_seeitlive_recipients", [[this.resId]]),
                    this.orm.call(this.resModel, "rpc_seeitlive_defaults", [[this.resId]]),
                ]);
                this.state.recipients = recipients;
                this.state.channel = defaults.channel;
                this.state.protection = defaults.protection;
                this.state.recipient = this.defaultRecipient(defaults.channel);

                const bound = this.props.record.data[this.props.name];
                if (bound) {
                    this.state.sessionId = bound[0];
                    this.state.phase = "waiting";
                }
            }
        });

        onMounted(() => {
            if (this.state.sessionId) {
                this.refresh();
                this.startPolling();
            }
        });
        // A closed tab must leave nothing running.
        onWillUnmount(() => this.stopPolling());
    }

    get resModel() {
        return this.props.record.resModel;
    }

    get resId() {
        return this.props.record.resId;
    }

    get options() {
        return this.state.channel === "sms" ? this.state.recipients.phones
            : this.state.channel === "email" ? this.state.recipients.emails
            : [];
    }

    /** First known address for a channel, or empty for link-only. */
    defaultRecipient(channel) {
        if (channel === "sms") return this.state.recipients.phones[0] || "";
        if (channel === "email") return this.state.recipients.emails[0] || "";
        return "";
    }

    onChannelChange(ev) {
        this.state.channel = ev.target.value;
        this.state.recipient = this.defaultRecipient(this.state.channel);
        this.state.consent = false;
    }

    /**
     * SMS needs an explicit consent tick and a number; email needs an address.
     * The API refuses a consent-free SMS too, so this gate is enforced twice —
     * but the honest place to refuse is before anything leaves the building.
     *
     * An unsaved record (no resId) can never start a session — there is no
     * res_id yet for action_seeitlive_start to attach it to — so the button
     * stays disabled until the record is saved.
     */
    get canStart() {
        if (this.state.phase === "starting") return false;
        if (!this.resId) return false;
        if (this.state.channel === "sms") return !!this.state.recipient && this.state.consent;
        if (this.state.channel === "email") return !!this.state.recipient;
        return true;
    }

    /**
     * The channel a re-send would actually use: the agent's explicit pick,
     * or — while reshareChannel is still null, i.e. the agent has not
     * touched the dropdown — whatever channel is recorded on the session.
     * The dropdown *displays* the recorded channel as selected from the
     * moment the form opens, so gating on the raw reshareChannel alone
     * (null until a change event fires) let an SMS session's re-share look
     * and act like a no-consent-needed send. canReshare, the consent
     * checkbox's t-if, and the dropdown's own selected-state test must all
     * read this same getter so the three can never disagree again.
     */
    get effectiveReshareChannel() {
        return this.state.reshareChannel || this.state.session.channel;
    }

    /**
     * Re-share gate. Mirrors canStart's SMS-needs-consent rule, evaluated
     * against the EFFECTIVE channel (see effectiveReshareChannel) so an
     * inherited SMS channel is gated exactly like an explicitly-picked one
     * — re-sending what the customer already consented to still needs the
     * tick, because a re-send is itself a new billable text.
     *
     * Also refuses a channel switch with no recipient: leaving the
     * recipient blank while changing away from the recorded channel would
     * let the server fall back to invitation.recipient — the OTHER
     * channel's address — silently mis-typing the invitation (server-side
     * cross-validation is out of scope for this branch; this is the
     * client-side half of that guard).
     */
    get canReshare() {
        if (this.state.reshareBusy) return false;
        if (
            this.state.reshareChannel &&
            this.state.reshareChannel !== this.state.session.channel &&
            !this.state.reshareRecipient
        ) {
            return false;
        }
        if (this.effectiveReshareChannel === "sms") return this.state.reshareConsent;
        return true;
    }

    async start() {
        if (!this.canStart) return;
        this.state.phase = "starting";
        try {
            const sessionId = await this.orm.call(
                this.resModel,
                "action_seeitlive_start",
                [[this.resId]],
                {
                    options: {
                        channel: this.state.channel,
                        recipient: this.state.recipient,
                        sms_consent: this.state.consent,
                        protection: this.state.protection,
                    },
                },
            );
            this.state.sessionId = sessionId;
            await this.refresh();
            this.startPolling();
            // Reload so the computed seeitlive_active_session_id points at it.
            await this.props.record.load();
            this.notification.add(_t("Live view started."), { type: "success" });
        } catch (error) {
            // Odoo already shows the UserError dialog; just return to the form.
            this.state.phase = "idle";
            throw error;
        }
    }

    startPolling() {
        this.stopPolling();
        this.pollHandle = setInterval(() => this.refresh(), 5000);
    }

    stopPolling() {
        if (this.pollHandle) {
            clearInterval(this.pollHandle);
            this.pollHandle = null;
        }
    }

    /** One poll: refresh the payload and move the phase if the call has moved. */
    async refresh() {
        if (!this.state.sessionId) return;
        let payload;
        try {
            payload = await this.orm.call(
                "seeitlive.session", "rpc_panel_state", [[this.state.sessionId]],
            );
        } catch {
            // Server unreachable from the browser; the next tick tries again.
            return;
        }
        this.state.session = payload;

        if (payload.is_finished) {
            this.stopPolling();
            this.state.phase = "finished";
            if (!this.syncRequested) {
                // Bring the snapshots in now rather than waiting up to five
                // minutes for the cron, so they are on the record before the
                // agent has finished typing their note.
                this.syncRequested = true;
                try {
                    await this.orm.call("seeitlive.session", "action_sync", [[this.state.sessionId]]);
                    this.state.session = await this.orm.call(
                        "seeitlive.session", "rpc_panel_state", [[this.state.sessionId]],
                    );
                    await this.props.record.load();
                } catch {
                    // Same contract as ensureViewer's own try/catch just
                    // below: a transient failure (a 5xx, a network blip)
                    // here must not throw an unhandled rejection at the
                    // agent right as the call ends. Polling has already
                    // stopped by this point, so clear the flag rather than
                    // leave the session stuck "unsynced" with nothing left
                    // that will ever retry it client-side — the cron still
                    // will, and so will a "Sync now" click.
                    this.syncRequested = false;
                }
            }
            return;
        }
        if (payload.state === "live") {
            try {
                await this.ensureViewer();
                this.state.viewerError = "";
            } catch {
                // Minting can legitimately fail (a transient 5xx from the
                // API). The customer is live either way, so an error dialog
                // repeating every five seconds is the wrong signal — surface
                // it inline instead, same as rpc_panel_state's own failure
                // above, and let the next tick retry the mint.
                this.state.viewerError = _t(
                    "Could not load the live view yet. Retrying…"
                );
            }
            this.state.phase = "live";
        } else {
            this.state.viewerError = "";
            this.state.phase = "waiting";
        }
    }

    /**
     * A previous re-share (successful or abandoned) must not leak into this
     * one: onReshare only clears reshareOpen, so without this reset a form
     * reopened after a completed SMS re-send would show SMS pre-selected
     * with consent already ticked — a stale tick surviving to authorize a
     * second, un-attested text.
     */
    openReshareForm() {
        this.state.reshareOpen = true;
        this.state.reshareChannel = null;
        this.state.reshareRecipient = "";
        this.state.reshareConsent = false;
    }

    /** Mirrors onChannelChange exactly, consent reset included: without
     * it, ticking consent under SMS, switching to Email (the tick just
     * hides), then switching back to SMS would leave the box already
     * checked — a tick surviving a round trip through another channel is
     * not the explicit-per-selection consent this gate exists to record. */
    onReshareChannelChange(ev) {
        this.state.reshareChannel = ev.target.value;
        this.state.reshareConsent = false;
    }

    /**
     * Revive an expired invitation (mode: "revive") or issue a fresh one
     * (mode: "new"). Any failure is left to propagate — Odoo already shows
     * the UserError dialog for a rejected RPC, same as start() above — the
     * finally just guarantees the busy flag can never wedge the form open.
     */
    async onReshare(mode) {
        if (this.state.reshareBusy) return;
        this.state.reshareBusy = true;
        try {
            await this.orm.call("seeitlive.session", "action_reshare", [[this.state.sessionId]], {
                channel: this.state.reshareChannel || undefined,
                recipient: this.state.reshareRecipient || undefined,
                sms_consent: this.state.reshareConsent,
                mode: mode,
            });
            this.state.reshareOpen = false;
            await this.refresh();
        } finally {
            this.state.reshareBusy = false;
        }
    }

    async onReshareRevive() {
        await this.onReshare("revive");
    }

    /**
     * Destructive: kills the link the customer is currently holding, with
     * no undo, on a single click. No dialog-service precedent exists
     * elsewhere in this addon (grepped: this is the only JS file), so this
     * falls back to a plain confirm() rather than inventing a new pattern.
     */
    async onReshareNew() {
        if (!window.confirm(_t(
            "The customer's current link will stop working immediately. Issue a new one?"
        ))) {
            return;
        }
        await this.onReshare("new");
    }

    async copyLink() {
        await navigator.clipboard.writeText(this.state.session.share_url);
        this.notification.add(_t("Link copied."), { type: "success" });
    }

    /**
     * Fetch the viewer URL once per live phase. Minting is a real API call, so
     * the five-second poll must not do it on every tick.
     */
    async ensureViewer() {
        if (this.state.agentViewUrl) return;
        this.state.agentViewUrl = await this.orm.call(
            "seeitlive.session", "rpc_agent_view_url", [[this.state.sessionId]],
        );
    }

    openFullScreen() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "seeitlive.session",
            res_id: this.state.sessionId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    popOut() {
        window.open(this.state.agentViewUrl, "_blank", "noopener");
    }
}

export const seeItLivePanel = {
    component: SeeItLivePanel,
    supportedTypes: ["many2one"],
};

registry.category("fields").add("seeitlive_viewer", seeItLivePanel);

/**
 * The same panel on the seeitlive.session form itself, where the record is the
 * session rather than the thing it was started from. Only the id source
 * differs, so it subclasses rather than duplicating.
 */
export class SeeItLiveSessionPanel extends SeeItLivePanel {
    setup() {
        super.setup();
        this.state.sessionId = this.props.record.resId;
        this.state.phase = "waiting";
    }
}

registry.category("fields").add("seeitlive_session_viewer", {
    component: SeeItLiveSessionPanel,
    supportedTypes: ["integer"],
});
