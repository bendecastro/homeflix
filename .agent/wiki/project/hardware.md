# homeflix — Hardware

Updated: 2026-08-04
Decision: [ADR-0002](../decisions/adr-0002-host-minipc-debian-docker.md).

The hardware class homeflix targets, and the constraints it imposes on the design.
Replicator-facing requirements are in [`docs/hardware.md`](../../../docs/hardware.md);
this page records *why* the hardware shapes the architecture.

## Reference build

homeflix was designed and built against a deliberately modest machine, so the
architecture never assumes headroom it might not have:

| Field | Reference |
|---|---|
| Class | Low-power always-on x86-64 mini-PC |
| CPU | Intel Celeron-class with integrated graphics |
| RAM | 8GB |
| Boot drive | 1TB SATA SSD, Debian |
| Library drive | 4TB external HDD over USB 3.0, ext4 |
| Network | Wired gigabit ethernet |

Anything equal or better runs homeflix comfortably. See
[storage.md](storage.md) for how the two drives are used — note that a single-drive
host works equally well.

## Constraints this hardware imposes

These are the reasons behind several design decisions; they are not incidental.

- **Weak CPU → don't rely on transcoding.** A Celeron-class part can manage roughly one
  hardware-assisted stream, and software transcoding of 4K is out of reach. The design
  therefore **prefers direct play**: keep the library in formats clients can play
  natively rather than promising many parallel transcodes. Hardware transcoding via
  Intel QuickSync requires passing `/dev/dri` into the Jellyfin container — left
  commented out in `docker-compose.yml` until verified on a given host.
- **8GB RAM** → comfortable for ~14 containers, but leaves little slack. Avoid piling on
  additional services without checking.
- **USB-attached library drive** → susceptible to bus resets and spin-down. Mount via
  `/etc/fstab` **by UUID with `nofail`**, so a missing or slow drive doesn't hang boot.
  Because downloads now live on this drive too ([ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md)),
  it stays active more of the time — sustained USB reliability matters more than
  spin-down tuning.
- **Single box** → no redundancy and no high availability. Acceptable for a household,
  but it makes off-box config backups essential rather than optional
  ([deployment.md](deployment.md)).

## Links
- [Storage](storage.md) · [Deployment](deployment.md) · [Media server](media-server.md)
- [ADR-0002](../decisions/adr-0002-host-minipc-debian-docker.md) ·
  [ADR-0008](../decisions/adr-0008-single-filesystem-data-root-hardlinks.md)
