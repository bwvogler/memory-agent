# Hosting comparison

Every row was checked against primary sources in August 2026. The single
selection criterion is ADR 0001: **can the container FUSE-mount TigerFS?**
Everything else is secondary, because a host that cannot do this cannot run the
project at all.

Workload assumed: bursty, idle most of the time, wakes on an HTTP request,
1–2 GB RAM, ~5 active hours/month.

| Platform | In-container FUSE? | Scale-to-zero / cold start | ~Monthly, mostly idle | Free allowance |
|---|---|---|---|---|
| **Fly.io Machines** | **Yes, verified.** Firecracker microVM, root, `/dev/fuse` present. Fly's own LiteFS is FUSE-based; staff confirm no kernel changes needed. | Autostop/autostart via Fly Proxy. Resume from suspend "a few hundred milliseconds" vs ~2s cold boot. Suspend requires ≤2 GB RAM. Stopped/suspended = no CPU/RAM billing. | **~$0.50–1.50** ($0.0154/h awake; $11.11/mo always-on for comparison) | None. Pure pay-as-you-go, no plan minimum. |
| **Modal VM Sandboxes** | **Yes, explicitly.** "FUSE mounts are supported" as a VM-mode improvement; real Linux kernel via `experimental_options={"vm_runtime": True}`. Default gVisor sandboxes do **not** support FUSE. | No HTTP-wakes-a-sleeping-sandbox primitive. Pattern: scale-to-zero web endpoint → restore Filesystem Snapshot → re-mount. No Memory Snapshots in VM mode. | **~$0.10–0.15**, effectively $0 | **$30/mo recurring credits** on Starter |
| **Cloudflare Containers** | **Yes, since 2025-11-21.** Docs describe installing a FUSE adapter in your Dockerfile — but only ever through the lens of object-storage adapters (s3fs, gcsfuse, tigrisfs). Whether CAP_SYS_ADMIN is granted is undocumented; `tigerfs` is not explicitly blessed. | Native. Cold start "often 1–3 second range". **All disk is ephemeral** — a sleeping instance wakes with a fresh disk from the image. | **$5 flat** (Workers Paid; usage fits included allowances) | No true free tier for Containers |
| **Google Cloud Run** | **No — explicitly forbidden.** Contract bars in-container mounts, adding/using kernel capabilities, and setuid FUSE binaries. gen1 and gen2 alike. `--privileged` and `--cap-add SYS_ADMIN --device /dev/fuse` both fail in practice. | (moot) excellent | (moot) ~$0 | 180k vCPU-s, 360k GiB-s, 2M req/mo |
| **Railway** | **No.** Privileged containers unsupported; staff: "You simply can't do such things on Railway.. Yet." | (moot) | (moot) | Hobby $5/mo |
| **Render** | **No.** Staff: "mounting S3 requires via the likes of Fuse require privileged access which we don't provide." | (moot) | (moot) | — |
| **Oracle Cloud Always Free ARM** | **Yes.** Real VM, full root. | Always-on, no cold start, persistent disk. **But** Oracle reclaims idle instances when 7-day p95 CPU <20%, network <20% *and* memory <20% — a mostly-idle agent trips all three. | **$0** | A1 **halved to 2 OCPU / 12 GB** effective 15 June 2026 |
| **E2B** | **Yes, with caveats.** Documented bucket mounts, but needs a custom template with FUSE installed and `sudo`. Unclear whether gVisor-shimmed, which would break arbitrary FUSE. | Auto-pause preserves state and stops billing. **Hobby caps continuous runtime at 1 h** (Pro 24 h). | Low single digits; **rates unverified** | $100 one-time credits; Pro $150/mo base |
| **Daytona** | **Yes.** Dedicated external-storage docs (mount-s3, gcsfuse, blobfuse2), `user_allow_other` covered. Privileged requirements not spelled out. | Sub-second creation, per-second billing, auto-stop/archive. | **~$0.40** | $200 one-time credits |
| **Vercel Sandbox** | **Yes, verified.** July 2026: mount remote storage and custom filesystems, "or other FUSE-compatible drivers". | Ephemeral by design. **Hobby max runtime 45 min**, Pro 24 h. `iad1` region only. | **$0** on Hobby | 5 h active CPU, 420 GB-h memory/mo. Hobby is non-commercial. |

## Recommendation

**Fly.io** for most people. The only option with unrestricted FUSE *and* a
persistent rootfs *and* sub-second HTTP-triggered wake *and* no idle billing. The
absence of a free tier is irrelevant at these volumes.

**Modal VM Sandboxes** if you want a genuine $0 and will accept extra
architecture: recurring credits mean your bill is zero rather than "free until
credits run out", but you build the wake path yourself and re-mount on every wake.

**Cloudflare Containers** for a predictable flat rate — but prove
`tigerfs mount postgres://…` actually works there before committing, and design
for a fresh disk on every wake.

## Uncertainties worth closing yourself

Whether Cloudflare grants CAP_SYS_ADMIN or shims `fusermount` (undocumented).
Whether E2B's sandboxes are gVisor-based, which would break arbitrary FUSE despite
their bucket docs. E2B's current per-second rates. And whether Oracle PAYG
accounts are exempt from the new lower A1 caps — still officially unclarified.

## Sources

Fly: [FUSE](https://community.fly.io/t/include-fuse-module-in-the-kernel-of-the-vm/649) ·
[suspend/resume](https://fly.io/docs/reference/suspend-resume/) ·
[autostop](https://fly.io/docs/reference/fly-proxy-autostop-autostart/) ·
[pricing](https://fly.io/docs/about/pricing/) ·
[plans](https://fly.io/plans)

Cloudflare: [FUSE changelog](https://developers.cloudflare.com/changelog/post/2025-11-21-fuse-support-in-containers/) ·
[r2-fuse-mount](https://developers.cloudflare.com/containers/examples/r2-fuse-mount/) ·
[architecture](https://developers.cloudflare.com/containers/platform-details/architecture/) ·
[pricing](https://developers.cloudflare.com/containers/pricing/) ·
[limits](https://developers.cloudflare.com/containers/platform-details/limits/)

Cloud Run: [container contract](https://docs.cloud.google.com/run/docs/container-contract) ·
[GCS volume mounts](https://docs.cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts) ·
[gcsfuse#787](https://github.com/GoogleCloudPlatform/gcsfuse/issues/787) ·
[pricing](https://cloud.google.com/run/pricing)

Others: [Modal VM Sandboxes](https://modal.com/docs/guide/vm-sandboxes) ·
[Modal pricing](https://modal.com/pricing) ·
[Railway](https://station.railway.com/feedback/allow-services-to-be-run-in-privileged-m-8c66b22b) ·
[Render](https://community.render.com/t/how-to-mount-s3-object-storage-on-docker-container/6241) ·
[Oracle Always Free](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) ·
[InfoQ on Oracle cuts](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/) ·
[E2B buckets](https://e2b.dev/docs/sandbox/connect-bucket) ·
[E2B billing](https://e2b.dev/docs/billing) ·
[Daytona storage](https://www.daytona.io/docs/en/mount-external-storage/) ·
[Daytona pricing](https://www.daytona.io/pricing) ·
[Vercel Sandbox FUSE](https://vercel.com/changelog/vercel-sandbox-now-supports-fuse-based-filesystems) ·
[Vercel Sandbox pricing](https://vercel.com/docs/sandbox/pricing)
