# 0002 — Fly.io Machines as the reference host

**Status:** accepted

## Context

Given ADR 0001, we need a host that allows in-container FUSE, wakes on an HTTP
request, and costs approximately nothing while idle. The workload is bursty:
idle most of the time, then a few minutes of real work.

## Decision

Fly.io Machines, `shared-cpu-1x` with 2 GB RAM, `auto_stop_machines = "suspend"`,
in the same region as the database.

## Why

Fly is the only candidate that provides all four properties at once:

**Unrestricted `/dev/fuse`.** Firecracker microVM, you are root. Fly's own LiteFS
is FUSE-based, and staff have said no kernel changes were needed for FUSE to
work.

**Persistent disk.** Rootfs and volumes survive suspend, so the mount and the
scratch directory are not rebuilt on every wake. Cloudflare Containers, by
contrast, give a fresh disk every time an instance sleeps.

**Fast wake.** Resume from suspend is a few hundred milliseconds versus ~2s for a
cold boot. Note that **suspend requires ≤2 GB RAM** — we are exactly at the
ceiling, so sizing up silently costs sub-second resume.

**No idle billing.** Suspended machines bill storage only: roughly $0.50–1.50/mo
at ~5 active hours, versus $11.11/mo always-on.

## Rejected

**Modal VM Sandboxes** — the strongest $0 alternative, and the only platform
whose docs say "FUSE mounts are supported" in plain text alongside a real kernel.
$30/mo in *recurring* free credits. But `vm_runtime` is experimental, default
gVisor sandboxes do *not* support FUSE, there is no HTTP-wakes-a-sleeping-sandbox
primitive (you build endpoint → restore snapshot → re-mount), and no memory
snapshots in VM mode, so every wake re-runs `tigerfs mount`.

**Cloudflare Containers** — flat $5/mo, 1–3s cold start, FUSE documented since
Nov 2025. But all disk is ephemeral on sleep, and their FUSE docs are written
entirely around object-storage adapters (s3fs, gcsfuse, tigrisfs); whether
`tigerfs mount postgres://…` works is unproven.

**Oracle Cloud Always Free ARM** — technically perfect, literally free, wrong for
*this* workload. The A1 allowance was halved to 2 OCPU / 12 GB on 15 June 2026,
and Oracle reclaims instances whose 95th-percentile CPU, network *and* memory all
sit under 20% over 7 days — which describes a mostly-idle agent exactly.

**Vercel Sandbox / E2B / Daytona** — all support FUSE, none is a host for a
long-lived wake-on-HTTP service. Vercel is `iad1`-only with a 45-min Hobby cap
and Hobby is non-commercial; E2B Hobby caps continuous runtime at 1 h and Pro
starts at $150/mo; Daytona's credits are one-time.

## The region rule

Non-negotiable: **the container and the database go in the same region.** TigerFS
turns every `stat` and `read` into SQL, and agent exploration is chatty enough
that a single recursive grep can be hundreds of round trips. `scripts/spike-latency.sh`
measures this; treat it as a go/no-go gate, not a nice-to-have.

## References

- <https://community.fly.io/t/include-fuse-module-in-the-kernel-of-the-vm/649>
- <https://fly.io/docs/reference/suspend-resume/>
- <https://fly.io/docs/about/pricing/>
- <https://modal.com/docs/guide/vm-sandboxes>
- <https://developers.cloudflare.com/containers/platform-details/architecture/>
- <https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/>
