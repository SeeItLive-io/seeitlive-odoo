# SeeItLive for Odoo

Odoo 17 addon (technical name `seeitlive_connect`) that starts a live video
session with a customer from any Odoo record and watches — and annotates —
the call from inside Odoo.

The agent picks a contact, lead or task, sends the customer a link (SMS,
email, or a link to hand over some other way), and the customer opens it on
their phone and shares their camera. No app install, no account. Snapshots
taken during the call are attached back to the record automatically, with a
summary posted to the chatter, once the session ends.

## Requirements

- Odoo 17.0
- The `requests` Python package
- A [SeeItLive](https://seeitlive.io) account and API key (`sil_live_...` or
  `sil_test_...`)

## Installation

1. Copy this directory into your Odoo `addons_path` as `seeitlive_connect`.
2. In Odoo, go to **Apps**, click **Update Apps List**, then search for and
   install **SeeItLive**.

## Configuration

Go to **Settings > SeeItLive** and set:

- **API Key** — your SeeItLive API key.
- **Request Signing Secret** — only needed if the key has HMAC request
  signing enabled; every outgoing request is then signed with HMAC-SHA256.
- **Embedding origin** — the Settings page shows this instance's origin (e.g.
  `https://odoo.acme.com`). Add it to your API key in SeeItLive, under the key's
  **Security > Embedding origins**, or the live viewer will not load inside
  Odoo. **Test connection** checks this and tells you if it is missing. It is
  always shown as `https://`, even where Odoo's own `web.base.url` says `http`
  — which is what an instance behind a TLS-terminating proxy records unless
  `proxy_mode = True` is set in the Odoo config. If the host or the port shown
  is wrong, correct `web.base.url` under **Settings > Technical > System
  Parameters**.
- **Defaults** — invite channel (SMS, email, or link only), invite lifetime
  in minutes, guest protection (none, password, or a rotating code), and
  whether snapshots are attached back to the record automatically.
- **Enabled models** — which models get a **SeeItLive** tab. Only models
  with a chatter (`mail.thread`) can be listed; the default set is
  `crm.lead`, `project.task` and `res.partner`, but any chatter-enabled
  model — including ones from other addons — can be added or removed here.
  Odoo needs a primary form view to inject the tab into; a model without one
  is skipped and logged, not failed.

Click **Test connection** to verify the key and base URL are correct.

## Usage

Open a record whose model is enabled and go to the **SeeItLive** tab. The
tab is one panel that moves through three states:

1. **Launcher** — pick how to reach the customer (SMS, email, or a link you
   copy and send yourself), the mobile number or email address (prefilled
   from the record's own phone/mobile/email fields, or its partner's), and
   guest protection. SMS requires an explicit tick confirming the customer
   agreed to receive a text message before the button unlocks. Starting a
   session needs the record saved first — there is nothing to attach the
   session to otherwise.
2. **Waiting room** — the invite link and QR code, and, if the session is
   password- or code-protected, the secret to read out to the customer. The
   panel polls every five seconds for the customer to open the link.

   If the invite link expires before the customer opens it, the waiting room
   offers **Re-share**: re-send the same link (it starts working again — the
   customer's original SMS or email is still valid), or issue a new link, which
   stops the old one working. You can change the channel or the recipient while
   you do it — switching channel requires typing the new recipient (a blank
   field no longer silently reuses the other channel's address). SMS still
   needs the consent tick, whether SMS is the recorded channel or one you just
   switched to.

3. **Live** — once the customer joins, an embedded viewer shows their camera
   feed and lets the agent annotate it and take snapshots, full-screen or
   popped into its own window.

When the call ends, the session's snapshots are attached to the record as
`ir.attachment`s and an internal note (not an email to the customer) is
posted to the chatter with the call's duration and how many snapshots came
back. This happens as soon as the panel sees the session end; if the agent's
tab is already closed by then, a background job checks every five minutes
for anything left unsynced, for up to seven days.

Every session started from a record is also listed under
**SeeItLive > Sessions**, and on the `seeitlive.session` record's own form
there is a **Sync now** button to pull results in on demand.

## Privacy

This module only talks to SeeItLive when an agent explicitly starts a
session, or to poll/sync one already running.

**Customer and record data.** Starting a session sends:

- **The recipient's phone number or email address** — only for the channel
  the agent picked (SMS or email); a link-only session sends neither.
- **`odoo_model`, `odoo_res_id` and `odoo_db`** — which record and which
  database the session belongs to, so a snapshot or a support conversation
  can be traced back to the right place. The same model/record reference is
  also sent as a plain-text label when minting the temporary agent-view
  token used to embed the live viewer.

Nothing else about the customer or the record leaves Odoo: not the record's
other fields, not its chatter history, not any other contact on file.

**Session settings.** The same start-session request also sends the
channel (`sms`/`email`/`link`), the invite lifetime in minutes, the guest
protection scheme (`password`/`totp` — never the secret itself, and only
sent when protection is enabled), and, for SMS only, the consent
confirmation and a flag requesting a short link. These are the agent's own
configuration choices — set in the launcher or the Configuration defaults —
and identify nobody.

Every other request this module makes — refreshing a session's status,
fetching a protection secret to read out, listing or downloading snapshots,
and the Settings page's "Test connection" check — sends nothing beyond the
session's own id (or, for "Test connection", a placeholder id that names no
real session) in the request URL, with no request body of their own.
Minting an agent-view token is the one exception: its request body is only
the model/record label already disclosed above.

The guest protection secret (a password or rotating code), if any, is
generated by SeeItLive and fetched back from it on demand — it is never
stored in Odoo. The agent-view token used to embed the live viewer is
minted fresh each time and is not persisted either; only what cannot be
re-derived (the invite link, the session's own id, and the results once the
call ends) is stored on the `seeitlive.session` record.

## Known gaps

Recorded honestly after the Task 14 end-to-end run:

- **Webhooks are not implemented** — results arrive by polling. If the
  agent's tab closes mid-call, the background cron sweep (every 5 minutes)
  is what brings the snapshots and chatter note in, so the record can be up
  to 5 minutes stale.
- **Only Odoo 17.0 is built and tested.** No 16.0 or 18.0 branch exists yet.
