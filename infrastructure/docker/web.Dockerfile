# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS dependencies

WORKDIR /app
RUN npm install --global pnpm@11.20.0

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/shared-types/package.json ./packages/shared-types/package.json
COPY packages/ui/package.json ./packages/ui/package.json
RUN pnpm install --frozen-lockfile

FROM dependencies AS build

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

COPY tsconfig.base.json ./tsconfig.base.json
COPY apps/web ./apps/web
COPY packages/shared-types ./packages/shared-types
COPY packages/ui ./packages/ui
RUN pnpm --filter @universal-ai-search/web build

FROM node:22-bookworm-slim AS runtime

ENV HOSTNAME=0.0.0.0 \
    NODE_ENV=production \
    PORT=3000

WORKDIR /app
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

COPY --from=build --chown=nextjs:nodejs /app/apps/web/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/apps/web/.next/static ./apps/web/.next/static

USER nextjs
EXPOSE 3000

CMD ["node", "apps/web/server.js"]
