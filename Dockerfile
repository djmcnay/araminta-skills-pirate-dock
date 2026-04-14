FROM ghcr.io/bubuntux/nordvpn:latest

USER root

# ── Environment ──────────────────────────────────────────────
ENV HOME=/root \
    DEBIAN_FRONTEND=noninteractive \
    XDG_CONFIG_HOME=/root/.config \
    XDG_DATA_HOME=/root/.local/share \
    JACKETT_PORT=9118

# ── System deps (minimal — no browser libs!) ─────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates iptables iproute2 procps \
    aria2 jq python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ──────────────────────────────────────────────
COPY scripts/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

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

# Copy server.py to app root so uvicorn can import it
COPY scripts/server.py /app/server.py

# ── Volumes & ports ──────────────────────────────────────────
VOLUME /downloads /data
EXPOSE 9876

CMD ["/app/scripts/entrypoint.sh"]
