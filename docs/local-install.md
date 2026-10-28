# Local mock/paper installation

This path starts the API and website on loopback for supervised manual mock/paper
use. It does not start a worker or beat, connect a broker, or enable trading. Keep
`APP_MODE=mock`, `MARKET_DATA_PROVIDER=mock`, `LIVE_TRADING_ENABLED=false`, and both
Sentry DSNs empty. Demo credentials and autonomous strategies require their own
qualification; this installation is not that qualification.

Use Python3.12, Node24.21.0 and npm11.19.0 (the repository toolchain), Docker and
Compose v2. Run from a fresh checkout, with unused selected ports. Do not use
`make reset`, delete volumes, or repurpose another installation's database.

## Configuration

```sh
python3.12 infra/scripts/create_env.py
```

The helper creates a mode600 `.env` with independently generated keys and never
replaces an existing path. An existing regular file is preserved byte for byte;
symlinks and non-files are refused. It needs a filesystem supporting hard links
within the checkout directory. Failure leaves no published partial configuration.
It does not repair an existing weak, incomplete or colliding key configuration.
Do not rotate an existing `MASTER_KEY` casually: stored broker credentials depend
on it. Back up existing key material separately under the owner's custody.

Review the new file locally. Set a unique administrator password before ordinary
use; the checked-in example password is a fixture default. Confirm database and
Redis URLs describe only the intended local services. If changing a datastore
password, do not assume an existing volume's stored password also changes.
Never paste keys into logs, `NEXT_PUBLIC_*`, issues or source control.

## Dependencies and datastores

```sh
python3.12 -m venv apps/api/.venv
apps/api/.venv/bin/python -m pip install pip==26.2.2
apps/api/.venv/bin/python -m pip install -r apps/api/requirements.txt
cd apps/web
npm ci --include=dev
cd ../..
docker compose up -d postgres redis
```

Wait for both services to become healthy. Confirm authenticated access to the
configured database and Redis, rather than treating `pg_isready` as an
authentication test. The Compose datastore host ports are loopback-only. This
recipe does not run `docker compose up` for the API/web: those development service
bindings and normal launchers are a separate access policy.

The virtual environment's installer is explicitly pinned to pip26.2.2; the
Python3.12 bundled pip can be older and vulnerable. This updates only that
virtual environment, not the system interpreter.

The API pins direct requirements; Python transitive versions are not locked by
this change. Record the resolved graph for an acceptance rehearsal. `npm ci`
uses the committed lock and must not rewrite it. Build-time development dependencies
are explicitly included even if the shell sets NODE_ENV=production. Use an isolated virtual
environment; do not install backend packages globally.

## Migrate, seed and start

Use the installed dotenv parser to load the exact file without evaluating it as
shell code. These commands honor its configured database URL, including a
deliberately selected nondefault local port/database.

```sh
cd apps/api
.venv/bin/python -m dotenv -f ../../.env run -- .venv/bin/python -m alembic upgrade head
.venv/bin/python -m dotenv -f ../../.env run -- .venv/bin/python -m app.db.seed
.venv/bin/python -m dotenv -f ../../.env run -- .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Seeding an existing administrator synchronizes its password to `ADMIN_PASSWORD`;
it is not a harmless way to inspect an existing installation. Seed only the
intended new database, or with a separately reviewed password-change intention.
Initial seeding must leave auto-trading and live unlock disabled. The kill switch
has its existing configured/default behavior; this recipe does not change it.
If saved application/venue automation or live unlock is enabled, a later API
restart refuses normal HTTP/WebSocket admission without altering saved state.
Use the explicit offline status/block/verify workflow in
[backup and restore](./operations/local-backup-restore.md), then restart API/site.
Fresh/already-unarmed starts retain their current defaults. Only exact liveness
is available before validation; lifespan-off startup stays closed. Intentional
authenticated rearm after a valid boot does not invalidate readiness. This manual
recipe never starts workers/beat or qualifies autonomous execution.

In a second terminal:

```sh
cd apps/web
SENTRY_DSN= NEXT_PUBLIC_SENTRY_DSN= NEXT_TELEMETRY_DISABLED=1 NEXT_PUBLIC_APP_MODE=mock NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npm run build
cp -R public .next/standalone/
cp -R .next/static .next/standalone/.next/
SENTRY_DSN= NEXT_PUBLIC_SENTRY_DSN= NEXT_TELEMETRY_DISABLED=1 API_URL=http://127.0.0.1:8000 HOSTNAME=127.0.0.1 PORT=3000 node .next/standalone/server.js
```

The repository builds with standalone output. Its generated server needs the
public/static copies above; build again before preparing a new copy. This follows
the [Next.js standalone instructions](https://nextjs.org/docs/app/api-reference/config/next-config-js/output).

Open `http://127.0.0.1:3000/auth/login`. Verify live/dependency health, login,
operator mode labels, disabled automation/live state and paper-only order labels.
An HTTP response alone is not the acceptance test. Exit each foreground server
with Ctrl-C; stop only the datastore services belonging to this installation.
Do not disable a kill switch or enable strategies merely to obtain a green page.

Existing shortcuts are not equivalent to this recipe: the legacy quickstart
still assumes its default database URL and invokes a general npm install;
`make dev` and the normal launcher expose different bindings. Their additional
configuration variants are not certified by this supported local path.

Before retaining important history, rehearse
[backup and restore](./operations/local-backup-restore.md). Keep the source SHA,
runtime versions, resolved dependencies and acceptance results with the release.
