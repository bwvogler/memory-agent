# The container must be able to FUSE-mount TigerFS, which needs /dev/fuse and
# CAP_SYS_ADMIN. That single requirement is what rules out Cloud Run, Railway
# and Render as hosts. See docs/decisions/0001-fuse-is-the-constraint.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KB_MOUNT=/mnt/kb \
    WORK_DIR=/work

RUN apt-get update && apt-get install -y --no-install-recommends \
        bc ca-certificates curl fuse3 git ripgrep tini xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node, for two reasons: a few SDK install paths still want it present, and the
# outbound MCP servers below ARE Node programs. Not Debian's `nodejs` package,
# which is v20 on trixie - @klodr/gmail-mcp declares `node >=22.23.1`, and npm
# reports that as a WARNING and installs anyway, so the mismatch would surface
# as a server that fails at some unknown later moment rather than at build time.
#
# Pinned to an exact version and checksummed, for the reason bd is pinned and
# cloudflared was removed: nothing in this image should be able to change
# underneath a rebuild nobody reviewed.
ENV NODE_VERSION=22.23.2
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch=x64; sha=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307 ;; \
      arm64) arch=arm64; sha=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8 ;; \
      *) echo "unsupported arch"; exit 1 ;; \
    esac; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    echo "${sha}  node-v${NODE_VERSION}-linux-${arch}.tar.xz" | sha256sum -c -; \
    tar -xJf "node-v${NODE_VERSION}-linux-${arch}.tar.xz" -C /usr/local --strip-components=1 \
        --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \
    rm "node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    node --version; npm --version

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
#
# DO NOT bump this as a routine dependency update; see img-4r2. 1.2.2 is the
# tested 1.1.2 code re-released and speaks schema v53. Every ledger here is at
# v53 to match, which took a one-time Dolt cursor rollback after the retracted
# 1.2.1 migrated them all to v65 - so the cost of getting a bump wrong is not a
# failed build, it is a data migration.
#
# What makes that expensive is the direction. A binary refuses a database ahead
# of it, and kb.py logs-and-continues when bd is unreachable, so the symptom of
# a bad bump is not an error: it is a ledger that quietly stops recording. Read
# img-4r2 for the recovery, and note that a newer bd may migrate on first run
# without asking, which is not reversible by reinstalling the old one.
#
# The container tier cannot catch any of this. It builds fresh ledgers, which
# any binary opens happily; only a real volume carries the history.
ENV BEADS_VERSION=1.2.2
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/beads.tar.gz \
      "https://github.com/gastownhall/beads/releases/download/v${BEADS_VERSION}/beads_${BEADS_VERSION}_linux_${arch}.tar.gz"; \
    tar -xzf /tmp/beads.tar.gz -C /usr/local/bin bd; \
    rm /tmp/beads.tar.gz; \
    chmod +x /usr/local/bin/bd; \
    bd version

# The outbound MCP servers in app/mcp_catalog.py. Baked at PINNED versions
# rather than npx-ed per turn: npx would resolve over the network on a machine
# that suspends, and an unpinned server can gain TOOLS on a rebuild - tools that
# neither `auto_approve` nor `deny` names, which is a security review rather
# than a version bump. Same reasoning as bd above and cloudflared's removal.
#
# The prune is not cosmetic. googleapis ships typed clients for all 323 Google
# APIs and each server depends on its own copy, so a plain install is 443MB
# against a ~1.5GB image and the 2GB suspend ceiling in fly.toml. Dropping the
# TypeScript declarations and sourcemaps leaves 148MB installed and both
# servers still start. Deleting the unused API directories does NOT work -
# apis/index.js requires all 323 eagerly - so do not try to take this further.
#
# Net effect on the image is slightly NEGATIVE (1.57GB -> 1.53GB): the pinned
# Node tarball above replaced Debian's nodejs+npm, which cost more than this.
ENV GCAL_MCP_VERSION=2.6.2 \
    GMAIL_MCP_VERSION=1.3.3
RUN npm install -g --omit=dev --no-audit --no-fund \
        "@cocal/google-calendar-mcp@${GCAL_MCP_VERSION}" \
        "@klodr/gmail-mcp@${GMAIL_MCP_VERSION}" \
    && find "$(npm root -g)" \( -name '*.d.ts' -o -name '*.map' \) -delete \
    && npm cache clean --force \
    && command -v google-calendar-mcp && command -v gmail-mcp

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
