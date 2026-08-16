# The container must be able to FUSE-mount TigerFS, which needs /dev/fuse and
# CAP_SYS_ADMIN. That single requirement is what rules out Cloud Run, Railway
# and Render as hosts. See docs/decisions/0001-fuse-is-the-constraint.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KB_MOUNT=/mnt/kb \
    WORK_DIR=/work

RUN apt-get update && apt-get install -y --no-install-recommends \
        bc ca-certificates curl fuse3 git ripgrep tini \
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

# No cloudflared. It was here to run a tunnel in-process, which cannot work
# under this fly.toml - the tunnel suspends with the machine and only the Fly
# proxy can wake it, on the route the tunnel was replacing. Removing it drops an
# UNPINNED binary (it tracked releases/latest, so every rebuild could ship a
# different one) and its weight, which matters against the 2GB suspend ceiling.
# See entrypoint.sh, "Why there is no tunnel here" in the README, and img-753.

# bd (beads), the task ledger. Pinned: bd refuses to open a database written by
# a newer schema, so an unpinned agent would break the graph on redeploy.
# Ships an embedded Dolt engine, which is most of the ~100MB - watch it against
# the 2GB suspend ceiling in fly.toml.
ENV BEADS_VERSION=1.2.1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/beads.tar.gz \
      "https://github.com/gastownhall/beads/releases/download/v${BEADS_VERSION}/beads_${BEADS_VERSION}_linux_${arch}.tar.gz"; \
    tar -xzf /tmp/beads.tar.gz -C /usr/local/bin bd; \
    rm /tmp/beads.tar.gz; \
    chmod +x /usr/local/bin/bd; \
    bd version

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY skills ./skills
# Seeded into the KB at startup. Without this the seeder finds no source
# directory and silently does nothing, so bootstrap skills never ship.
COPY bootstrap ./bootstrap
COPY entrypoint.sh .
COPY scripts ./scripts
# The one file from docs/ the running app reads: which beads this image
# resolves, closed in every ledger on the volume at startup. Copied on its own
# rather than as the whole directory - the ADRs are for readers of the repo,
# not for the image. See docs/decisions/0010.
COPY docs/shipped-beads.jsonl ./docs/shipped-beads.jsonl
RUN sed -i 's/\r//' entrypoint.sh && chmod +x entrypoint.sh && chmod +x scripts/*.sh && mkdir -p "$KB_MOUNT" "$WORK_DIR"

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/srv/entrypoint.sh"]
