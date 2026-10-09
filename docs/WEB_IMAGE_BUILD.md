# Building the web image

`apps/web/Dockerfile` separates fetching packages from running code:

1. **Retrieval** (`npm_fetch` stage): `npm ci --ignore-scripts` downloads the
   lockfile-pinned tarballs into an npm cache. npm checks every tarball against
   its `package-lock.json` integrity hash. No dependency lifecycle script runs
   in this stage. It is the only instruction that uses npm with network access.
2. **Install** (`deps` stage): `npm ci --offline` installs from that cache
   without `--ignore-scripts` and with networking disabled for the instruction
   (`RUN --network=none`). A lifecycle script that tries to download something
   gets no network in this instruction; for a required package that fails the
   build. Which install scripts npm actually runs is npm's own policy (npm
   11.19.0 reports packages whose install scripts are "not yet covered by
   allowScripts"). npm still treats a failed *optional* package as skipped, as
   it always has, so use the inventory check below rather than the install
   summary to confirm the tree is complete.
3. **Compile** (`builder` stage): `npm run build` also runs with
   `RUN --network=none`.

The `runner` stage, the non-root user and the entrypoint are unchanged. The
cache is bind-mounted for the install only and is not part of the final image.

The base image is `node:24.21.0-alpine` (npm 11.19.0), matching `.nvmrc`,
`.node-version`, CI and `package.json`. Every npm invocation passes
`--no-update-notifier`, which switches off npm's update check.

These instructions need BuildKit, which is the default builder in Docker 23+
and Docker Compose v2.

## What this does and does not establish

It establishes that the install in which dependency lifecycle scripts can run
and the application compile are instructions with networking disabled, that
the only npm instruction
with network access runs no lifecycle script, and (with the runtime check
below) that the built image serves with outbound networking denied.

It does not establish that nothing in a build ever attempts a network
operation, and it does not record such attempts. `RUN --network=none` governs
`RUN` instructions; the builder still pulls the base image and resolves build
contexts. Lockfile integrity hashes authenticate package bytes against the
lockfile, not the publisher, and npm itself runs with network access in the
retrieval stage.

## Ordinary build

```bash
docker build -t cashguard-web apps/web
```

```bash
docker compose build web
```

Both fetch packages over the network in the retrieval stage, then install and
compile with networking disabled.

## Build from a supplied cache, no network for any `RUN` instruction

Export the cache once, on the platform you will build for (the cache contains
that platform's optional native packages):

```bash
docker build --target npm_cache --output type=local,dest=/tmp/cashguard-npm-cache apps/web
```

Then build with networking disabled for every `RUN` instruction, supplying the
directory as the `npm_cache` build context. The retrieval stage is replaced by
that directory and does not run:

```bash
docker build --network=none \
  --build-context npm_cache=/tmp/cashguard-npm-cache \
  -t cashguard-web apps/web
```

The directory must have `_cacache` at its root. Keep it outside `apps/web` so
it is not sent as build context. `--network=none` applies to the build's `RUN`
instructions; the builder itself can still pull the base image if it is not
already present locally.

## Checking a build

Installed tree against the lockfile, for the platform the image was built for
(tag the `builder` stage with `--target builder` first):

```bash
docker run --rm -i --network none cashguard-web:builder node - < scripts/verify_web_install_inventory.js
```

Runtime with outbound networking denied. The probe fails unless the container
has only a loopback interface, an outbound connection has no route, DNS
resolution fails, and `/auth/login` answers 200 with the security headers:

```bash
docker run -d --name web-offline --network none -e HOSTNAME=0.0.0.0 cashguard-web
docker exec -i web-offline node - < scripts/web_image_offline_probe.js
docker rm -f web-offline
```

Docker sets `HOSTNAME` to the container id, and the server binds to
`HOSTNAME`; with no network that name has no address, so the probe overrides
it to bind all interfaces. Publishing a port to `127.0.0.1` on the
host limits who can connect in; it does not stop the container connecting out.

## What CI covers

The `Web Image` job in `.github/workflows/ci.yml` runs, on the runner's
platform only: the ordinary build from a cold layer cache, the cache export,
the supplied-cache build with networking disabled, the inventory check, the
Dockerfile contract test (`apps/web/tests/unit/dockerfile-install-isolation.test.ts`)
using the Jest installed from the lockfile inside the build stage, the
egress-denied runtime probe, `docker compose build web`, and a published-port
smoke test of the ordinarily built image. It does not push an image anywhere,
and it does not replace the `Frontend` job's audit, lint, type check or full
unit suite.
