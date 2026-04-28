FROM ghcr.io/bubuntux/nordvpn:latest

USER root

# ── Environment ──────────────────────────────────────────────
ENV HOME=/root \
    DEBIAN_FRONTEND=noninteractive \
    XDG_CONFIG_HOME=/root/.config \
    XDG_DATA_HOME=/root/.local/share \
    JACKETT_PORT=9118 \
    DISPLAY=:1

# ── System deps ───────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates iptables iproute2 procps \
    aria2 jq python3 python3-pip python3-venv \
    libnss3 libnspr4 libglib2.0-0 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libx11-6 libxext6 libxfixes3 libxrender1 \
    xvfb dbus-x11 xfonts-base x11-utils xpra \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps + Playwright Chromium + Camoufox Firefox ─────
COPY scripts/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && playwright install chromium \
    && playwright install-deps chromium \
    && python3 -m camoufox fetch

# ── Jackett (multi-arch aware) ───────────────────────────────
ARG TARGETARCH
RUN ARCH="${TARGETARCH}" && \
    if [ "$ARCH" = "arm64" ]; then \
      JACKETT_URL="https://github.com/Jackett/Jackett/releases/latest/download/Jackett.Binaries.LinuxARM64.tar.gz"; \
    elif [ "$ARCH" = "arm/v7" ]; then \
      JACKETT_URL="https://github.com/Jackett/Jackett/releases/latest/download/Jackett.Binaries.LinuxARM32.tar.gz"; \
    else \
      JACKETT_URL="https://github.com/Jackett/Jackett/releases/latest/download/Jackett.Binaries.LinuxAMDx64.tar.gz"; \
    fi && \
    mkdir -p /opt/jackett && \
    curl -sL "$JACKETT_URL" | tar xz --strip-components=1 -C /opt/jackett && \
    chmod +x /opt/jackett/jackett

# ── App layout ───────────────────────────────────────────────
RUN mkdir -p /app /downloads /data
WORKDIR /app
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh
COPY scripts/server.py /app/server.py
COPY scripts/browser_fallback.py /app/browser_fallback.py

# ── Volumes & ports ──────────────────────────────────────────
VOLUME /downloads /data
EXPOSE 9876 9118 6081

CMD ["/app/scripts/run.sh"]
