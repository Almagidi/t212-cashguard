FROM node:24.21.0-alpine AS base
WORKDIR /workspace

# Every npm invocation passes --no-update-notifier, which switches off npm's
# update check; it is not part of the build.

# Retrieval only: download the lockfile-pinned tarballs (npm checks each one
# against its lockfile integrity hash) into an npm cache. Lifecycle scripts are
# off, so no dependency code runs while the network is reachable.
FROM base AS npm_fetch
COPY package.json package-lock.json* ./
RUN npm ci --ignore-scripts --no-audit --no-fund --no-update-notifier --cache /npm-cache

# Cache-only filesystem (root holds _cacache). Pass
# `--build-context npm_cache=<dir>` to supply a previously exported cache
# instead, so every RUN instruction can run with networking disabled; see
# docs/WEB_IMAGE_BUILD.md.
FROM scratch AS npm_cache
COPY --from=npm_fetch /npm-cache/ /

# Execution: install from the cache only, without --ignore-scripts, with
# networking disabled for the instruction. Any lifecycle script npm runs here
# has no network.
FROM base AS deps
COPY package.json package-lock.json* ./
RUN --network=none --mount=type=bind,from=npm_cache,target=/root/.npm,rw \
    npm ci --offline --no-audit --no-fund --no-update-notifier

FROM base AS builder
COPY --from=deps /workspace/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_URL=""
ARG API_URL="http://localhost:8000"
ARG NEXT_PUBLIC_APP_MODE=mock
ARG NEXT_PUBLIC_APP_VERSION=1.0.0
ARG NEXT_PUBLIC_ENABLE_PWA=false
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV API_URL=$API_URL
ENV NEXT_PUBLIC_APP_MODE=$NEXT_PUBLIC_APP_MODE
ENV NEXT_PUBLIC_APP_VERSION=$NEXT_PUBLIC_APP_VERSION
ENV NEXT_PUBLIC_ENABLE_PWA=$NEXT_PUBLIC_ENABLE_PWA
ENV NEXT_TELEMETRY_DISABLED=1
# The application compile runs dependency code (Next.js, PostCSS) too, so it
# also gets no network.
RUN --network=none npm --no-update-notifier run build

FROM base AS runner
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /workspace/public ./public
COPY --from=builder --chown=nextjs:nodejs /workspace/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /workspace/.next/static ./.next/static
USER nextjs
EXPOSE 3000
ENV PORT 3000
CMD ["node", "server.js"]
