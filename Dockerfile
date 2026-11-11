FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install runtime dependencies: ffmpeg, ca-certificates, curl, jq, wireguard-tools, netcat-openbsd, procps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    jq \
    wireguard-tools \
    netcat-openbsd \
    procps \
    && rm -rf /var/lib/apt/lists/*

# WARP config generator and stable userspace SOCKS5 relay.
ARG WARP_GENERATOR_COMMIT=d4616f154d654d5c159c193432159240c96614bb
ARG WIREPROXY_VERSION=1.1.3
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) wireproxy_arch="amd64" ;; \
        arm64) wireproxy_arch="arm64" ;; \
        armhf) wireproxy_arch="arm" ;; \
        *) echo "Unsupported architecture for wireproxy: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fL "https://raw.githubusercontent.com/lanrat/wireguard-warp-generator/${WARP_GENERATOR_COMMIT}/scripts/warp-register.sh" -o /usr/local/bin/warp-register; \
    chmod 755 /usr/local/bin/warp-register; \
    curl -fL "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${wireproxy_arch}.tar.gz" -o /tmp/wireproxy.tar.gz; \
    curl -fL "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/checksums.txt" -o /tmp/wireproxy.checksums; \
    checksum="$(awk -v asset="wireproxy_linux_${wireproxy_arch}.tar.gz" '$2 == asset { print $1 }' /tmp/wireproxy.checksums)"; \
    test -n "$checksum"; \
    printf '%s  /tmp/wireproxy.tar.gz\n' "$checksum" | sha256sum -c -; \
    tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin wireproxy; \
    chmod 755 /usr/local/bin/wireproxy; \
    rm -f /tmp/wireproxy.tar.gz /tmp/wireproxy.checksums; \
    mkdir -p /etc/wireguard

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source
COPY . .

# Ensure line endings are LF and scripts are executable
RUN sed -i 's/\r$//' entrypoint.sh scripts/wireproxy_ctl.sh \
    && chmod +x entrypoint.sh scripts/wireproxy_ctl.sh

# User and data directory setup
RUN useradd --create-home --uid 10001 sidecar \
    && mkdir -p /app/data /tmp/toastflix-warp \
    && chown -R sidecar:sidecar /app /tmp/toastflix-warp
USER sidecar

EXPOSE 3107

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3107"]
