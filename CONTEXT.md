# Homeflix Glossary

- **Acquisition phase** — The opt-in setup phase that enables VPN-routed download and indexer capabilities after core verification succeeds.
- **Artifact repository** — A destination that stores, lists, retrieves, and prunes Homeflix backup artifacts without defining how their contents are produced.
- **Backup artifact** — A dated, self-contained archive of consistently snapshotted Homeflix application configuration.
- **Compatibility adapter** — A preserved command entry point that delegates to the canonical Homeflix interface without owning duplicate behavior.
- **Core phase** — The setup phase that provides the family-facing media and request experience without requiring acquisition credentials or starting acquisition services.
- **Disruptive verification** — An explicitly authorized check that changes live state temporarily and must restore that state before it can succeed.
- **Fixture acceptance** — Evidence from deterministic fixtures and fake adapters; it does not establish that a real deployment works.
- **Live acceptance** — Redacted evidence that the intended behavior works on a real supported deployment.
- **Reconciliation** — Inspection of current state followed by the smallest safe changes needed to reach desired state; rerunning it does not duplicate resources.
- **Runtime verification** — Read-only inspection of a running deployment whose success requires every mandatory runtime domain to be observed.
- **Stack contract** — The load-bearing structural invariants that a rendered Homeflix Compose model must satisfy for storage, privacy, routing, recovery, and setup phases to work safely.
- **Static verification** — Read-only validation of configuration and the stack contract without requiring running containers.
- **Verification mode** — One explicit verification intent—static, runtime, or disruptive—with success semantics appropriate to that intent.
