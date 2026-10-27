# Operations

Use [configuration and setup](../setup.md), [private scripts](../scripts.md), and [publication instructions](../development.md). Read availability from `release/manifest.yaml`; staged artifacts are not installable releases.

Both publication workflows keep existing protections. After first publication, a package administrator manually changes GHCR visibility to Public. The ordinary `verify-public` job verifies anonymous version/digest pulls, architecture and OCI labels. After correcting visibility, use **Re-run failed jobs**, not **Re-run all jobs**. Publication does not change client pins or enable voice/TURN.

The client supplies approved browser URLs/roles and per-bot machine permissions, Base auth/bridge SecretStores, bridge managed registrations, gateway callers, TLS approval and narrow network egress. Public script-free bots need no object store. Optional storage retains its bucket claim, StorageClass and reader/publisher users; the client must provide generated-reader credential delivery and readiness dependencies without exposing publisher credentials to bots.

Voice admission stays disabled until compatible speech endpoints and inference are accepted. TURN remains optional; verify its certificate and edge UDP/TLS/relay routing separately. Restarting a bot discards its process-local calls/history. Use a short maintenance window for approved rollout changes.
