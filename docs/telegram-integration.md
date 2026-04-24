# Telegram Integration

CashGuard now supports two Telegram use cases:

- Alert delivery to a fixed chat via `TELEGRAM_CHAT_ID`
- Supervisory control from allowlisted chats and user IDs through the webhook route

## Required configuration

Add these values to `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:abc...
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_ALLOWED_CHAT_IDS=-1001234567890
TELEGRAM_ALLOWED_USER_IDS=123456789
TELEGRAM_WEBHOOK_SECRET=replace-this-with-a-random-secret
TELEGRAM_CONFIRM_WINDOW_SECONDS=120
```

Guidance:

- `TELEGRAM_CHAT_ID` is used for outbound alerts.
- `TELEGRAM_ALLOWED_CHAT_IDS` and `TELEGRAM_ALLOWED_USER_IDS` gate interactive control.
- If neither allowlist is configured, Telegram control is disabled by design.
- High-risk commands require a short-lived confirmation code.

## Webhook

Expose the API publicly and register your Telegram webhook against:

```text
POST /v1/telegram/webhook
```

Send Telegram's `secret_token` header with the value from `TELEGRAM_WEBHOOK_SECRET`.

## Supported commands

- `/status`
- `/positions`
- `/pause`
- `/resume`
- `/kill`
- `/cancelall`
- `/flatten`
- `/confirm <code>`
- `/cancel`

## Safety model

- Unauthorized chats/users are rejected.
- Risky actions are not executed immediately; they require `/confirm <code>`.
- Confirmations expire after `TELEGRAM_CONFIRM_WINDOW_SECONDS`.
- Executed, cancelled, and expired confirmation requests are stored in the database.
- Telegram control uses the same emergency control service as the web UI, so kill switch, cancel-all, flatten-all, and auto-trading toggles share the same safety rails and audit trail.

## Operational notes

- Prefer demo or mock mode until Telegram controls are exercised end-to-end.
- Keep the webhook secret private and rotate it if the endpoint is exposed.
- Treat Telegram as supervisory control, not a replacement for routine dashboard review.
