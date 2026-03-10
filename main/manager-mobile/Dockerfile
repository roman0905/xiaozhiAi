# ============================================================
# Stage 1: Build H5 static assets
# ============================================================
FROM node:18-alpine AS builder
WORKDIR /app

# Install pnpm (matches packageManager field in package.json)
RUN npm install -g pnpm@10.10.0 --registry=https://registry.npmmirror.com

# Use domestic mirror to avoid timeouts when building on a Chinese server
RUN pnpm config set registry https://registry.npmmirror.com

# Copy dependency manifests first to leverage Docker layer cache
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY patches/ ./patches/

# Install all dependencies
RUN pnpm install --no-frozen-lockfile

# Copy the full source tree
COPY . .

# ---------------------------------------------------------------------------
# Build-time environment overrides
#
# VITE_SERVER_BASEURL: base URL used by the frontend for all API requests.
#   Use the relative path "/xiaozhi" so that nginx (stage 2) proxies the
#   requests to the real backend – this avoids CORS issues entirely.
#   You can override at build time:
#     docker build --build-arg VITE_SERVER_BASEURL=/xiaozhi ...
# ---------------------------------------------------------------------------
ARG VITE_SERVER_BASEURL=/xiaozhi

# Write to .env.production.local – Vite loads this with the highest priority
# so it overrides any value already set in .env or .env.production.
RUN printf 'VITE_SERVER_BASEURL=%s\n' "${VITE_SERVER_BASEURL}" > env/.env.production.local

# Build the H5 target; output lands in dist/build/h5
RUN pnpm build:h5

# ============================================================
# Stage 2: Serve the built assets with Nginx
# ============================================================
FROM nginx:alpine

# ---------------------------------------------------------------------------
# BACKEND_URL: the upstream URL nginx will proxy /xiaozhi/ requests to.
#   Defaults to the web-manager container that already runs on port 8002.
#   Override when the backend lives elsewhere:
#     docker build --build-arg BACKEND_URL=http://<host>:<port> ...
# ---------------------------------------------------------------------------
ARG BACKEND_URL=http://192.168.0.25:8002

# Materialise the nginx config by substituting ${BACKEND_URL} at build time.
# Only ${BACKEND_URL} is substituted; all nginx variables ($uri, $host, …)
# are left untouched because we pass the variable name explicitly.
COPY docker/nginx.conf.template /tmp/nginx.conf.template
RUN envsubst '${BACKEND_URL}' < /tmp/nginx.conf.template \
      > /etc/nginx/conf.d/default.conf \
    && rm /tmp/nginx.conf.template

# Copy the built H5 assets from the previous stage
COPY --from=builder /app/dist/build/h5 /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
