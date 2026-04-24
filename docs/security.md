# Security

## Credential Storage

Trading 212 API keys are encrypted before storage using **Fernet** (AES-128-CBC with HMAC-SHA256), derived from your `MASTER_KEY` environment variable:

```python
# Encryption key derived from MASTER_KEY
digest = hashlib.sha256(MASTER_KEY.encode()).digest()
key = base64.urlsafe_b64encode(digest)   # 32 bytes → valid Fernet key
fernet = Fernet(key)

# Store
encrypted = fernet.encrypt(api_key.encode()).decode()

# Retrieve
plain = fernet.decrypt(encrypted.encode()).decode()
```

**What this means:**
- If the database is stolen without `MASTER_KEY`, credentials cannot be decrypted
- `MASTER_KEY` must be kept secret and backed up securely
- Rotating `MASTER_KEY` requires re-encrypting all stored credentials

## JWT Authentication

App sessions use RS256-signed JWT tokens with an 8-hour expiry. Tokens are stored in `localStorage` on the client and sent as `Authorization: Bearer <token>` headers.

**What's NOT stored in tokens:** passwords, API keys, or any secrets.

## What This App Does NOT Do

This is a hard guarantee enforced by the codebase, not configuration:

| Prohibited Action | Enforcement |
|---|---|
| Connect to your bank | Zero bank integration code exists |
| Initiate deposits | No deposit endpoint, model, or UI exists |
| Store card details | No card model or form exists |
| Use Open Banking | No Open Banking dependency or route exists |
| Use leverage or margin | No leverage parameter in any order type |
| Exceed available cash | `check_cash_guard()` called before every order |
| Auto-enable live trading | `LIVE_TRADING_ENABLED=False` hardcoded default |

To verify: `grep -r "deposit\|withdrawal\|open.banking\|bank_account\|debit.card" apps/` returns no results.

## Rate Limiting

The Trading 212 adapter handles `429 Too Many Requests` responses by reading the `Retry-After` header and sleeping before the next request. Per-account rate limits are respected by serialising broker calls through a single adapter instance per request.

## Local-Only

By design, CashGuard runs locally. There is no cloud component, no third-party analytics, no telemetry, and no data leaving your machine unless you are actively calling the Trading 212 API.

## Security Checklist Before Live Trading

- [ ] Changed `SECRET_KEY` from default (`openssl rand -hex 32`)
- [ ] Changed `MASTER_KEY` from default
- [ ] Changed `ADMIN_PASSWORD` from `change-me`
- [ ] Not committed `.env` to version control
- [ ] Running on localhost only (not exposed to internet)
- [ ] Verified risk profile limits are appropriate for your account size
- [ ] Tested with Demo mode before enabling Live
- [ ] Know how to trigger the kill switch (Emergency page or `POST /v1/emergency/kill-switch`)

## Incident Response

If you suspect the application has placed an unintended order:

1. **Immediately**: Open Emergency page → **Activate Kill Switch**
2. Log in to Trading 212 and review/cancel orders manually
3. Check Audit Log for all recent actions
4. Check Risk Events for any anomalies
5. Do NOT restart the app until you understand what happened
