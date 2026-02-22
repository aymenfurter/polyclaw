# --- Stage 1: Build the Vite/React frontend -------------------------------
FROM node:22-slim AS frontend-build
WORKDIR /build
COPY app/frontend/package.json app/frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY app/frontend/ ./
RUN npm run build

# --- Stage 2: Main runtime image -----------------------------------------
FROM python:3.12-slim

# System deps for Playwright and general tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq tree wget ca-certificates gnupg \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libasound2 libatspi2.0-0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Node.js (for Playwright MCP via npx) + Copilot CLI
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @github/copilot@0.0.405

# GitHub CLI (for authentication -- copilot CLI uses gh auth)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Azure CLI (for automated bot provisioning)
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# Docker CLI only (no daemon) -- used to push the locally-built image to ACR
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached unless pyproject.toml changes)
COPY pyproject.toml ./
RUN mkdir -p polyclaw && touch polyclaw/__init__.py \
    && pip install --no-cache-dir -e . \
    && (chmod +x /usr/local/lib/python3.12/site-packages/copilot/bin/copilot || true)

# Install Playwright MCP server globally, then install the MATCHING Chromium build
# by running `npx playwright install` from inside the package directory so it
# resolves the bundled Playwright version (not the latest stable one).
RUN npm install -g @playwright/mcp@latest \
    && cd "$(npm root -g)/@playwright/mcp" \
    && npx playwright install --with-deps chromium

# Cloudflare Tunnel (for exposing bot server to Azure Bot Service)
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
        -o /tmp/cloudflared.deb \
    && dpkg -i /tmp/cloudflared.deb \
    && rm /tmp/cloudflared.deb

# Copy the backend from app/runtime/ into the polyclaw/ package directory
# so existing entry points (polyclaw.server:main etc.) keep working.
COPY app/runtime/ polyclaw/

# Copy the single-command CLI so app.cli imports resolve in the container.
COPY app/cli/ app/cli/

# Reinstall so console-script entry points (polyclaw-admin etc.) are built
# against the real source tree, not the stub __init__.py used for dep caching.
RUN pip install --no-cache-dir --no-deps -e .
COPY skills/ skills/
COPY plugins/ plugins/

# Embed the built frontend at the path _FRONTEND_DIR resolves to
COPY --from=frontend-build /build/dist/ /app/frontend/dist/

# Copy logo & favicon into the frontend build
COPY assets/logo.png /app/frontend/dist/logo.png
COPY assets/favicon.ico /app/frontend/dist/favicon.ico

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

VOLUME /data
ENV POLYCLAW_DATA_DIR=/data
ENV POLYCLAW_CONTAINER=1

EXPOSE 8080 3978

CMD ["/app/entrypoint.sh"]
