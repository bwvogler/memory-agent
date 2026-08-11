# The container must be able to FUSE-mount TigerFS, which needs /dev/fuse and
# CAP_SYS_ADMIN. That single requirement is what rules out Cloud Run, Railway
# and Render as hosts. See docs/decisions/0001-fuse-is-the-constraint.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KB_MOUNT=/mnt/kb \
    WORK_DIR=/work

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl fuse3 git ripgrep tini \
    # The SDK bundles a native Claude Code binary for most installs, but a few
    # install paths still need Node present. Cheap insurance.
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# TigerFS. The installer may drop the binary in a non-standard location
# (e.g. ~/.local/bin), so we find it and copy to /usr/local/bin explicitly.
RUN curl -fsSL https://install.tigerfs.io | sh \
    && TIGERFS=$(find / -name tigerfs -type f -perm /111 2>/dev/null | head -1) \
    && [ -n "$TIGERFS" ] || { echo "ERROR: tigerfs binary not found after install"; exit 1; } \
    && echo "Found tigerfs at: $TIGERFS" \
    && install -m 755 "$TIGERFS" /usr/local/bin/tigerfs \
    && /usr/local/bin/tigerfs version

# cloudflared, so the origin needs no public IP and Access cannot be bypassed
# by hitting this container directly.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"; \
    chmod +x /usr/local/bin/cloudflared

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY skills ./skills
COPY entrypoint.sh .
COPY scripts ./scripts
RUN sed -i 's/\r//' entrypoint.sh && chmod +x entrypoint.sh && chmod +x scripts/*.sh && mkdir -p "$KB_MOUNT" "$WORK_DIR"

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/srv/entrypoint.sh"]
