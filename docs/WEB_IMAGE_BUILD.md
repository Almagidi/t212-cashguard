# Building the web image

`apps/web/Dockerfile` installs dependencies in two separate steps:

1. **Retrieval** (`npm_fetch` stage): `npm ci --ignore-scripts` downloads the
   lockfile-pinned tarballs into an npm cache. npm checks every tarball against
   its `package-lock.json` integrity hash. No dependency lifecycle script runs
   in this stage, so no dependency code executes while the network is reachable.
2. **Execution** (`deps` stage): `npm ci --offline` installs from that cache
   with lifecycle scripts enabled and with networking disabled for the
   instruction (`RUN --network=none`). A script cannot fall back to the
   network. For a required package that fails the build. npm still treats a
   failed *optional* package as skipped, as it always has, so a cache exported
   on a different platform shows up later as a failed application build.

The `builder` and `runner` stages are unchanged: in a default build
`npm run build` still runs with the builder's normal network access. The cache
is bind-mounted for the install only and is not part of the final image.

Both steps need BuildKit, which is the default builder in Docker 23+ and
Docker Compose v2.

## Default build

```bash
docker build -t cashguard-web apps/web
```

`docker compose build web` uses the same path.

## Build with no network at all

Export the cache once, on the platform you will build for (the cache contains
that platform's optional native packages):

```bash
docker build --target npm_cache --output type=local,dest=/tmp/cashguard-npm-cache apps/web
```

Then build with networking disabled for every instruction, supplying the
exported directory as the `npm_cache` build context:

```bash
docker build --network=none \
  --build-context npm_cache=/tmp/cashguard-npm-cache \
  -t cashguard-web apps/web
```

The supplied directory must have `_cacache` at its root. Keep it outside
`apps/web` so it is not sent as build context. `--network=none` applies to the
build's `RUN` instructions; the builder itself can still pull the base image if
it is not already present locally.

CI runs exactly these two commands, then starts the image and checks the
non-root user, `/auth/login` and the security headers (`Web Image` job in
`.github/workflows/ci.yml`). It does not push the image anywhere.

## Checking the result

```bash
docker run --rm cashguard-web id -u          # 1001 (non-root)
docker run --rm cashguard-web node --version
```

`apps/web/tests/unit/dockerfile-install-isolation.test.ts` fails if an install
instruction that runs lifecycle scripts is ever given network access again.
