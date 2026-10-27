# Pipecat add-on

Self-hosted voice bots and browser UI, with Keycloak admission and delegated AgentGateway model access. Each bot has one process and one replica; each connection has separate processors and conversation history. No database or Redis is used.

- [Configuration and setup](setup.md)
- [Private scripts](scripts.md)
- [Development and publication](development.md)
- [Upstream source](source.md)
- [Add-on tooling ownership](tooling/README.md)
- [Selection and lifecycle](architecture/README.md)
- [Development overview](development/README.md)
- [Operations](operations/README.md)

`release/manifest.yaml` is authoritative for availability and prerequisites. Staged packages are not installable releases. A rollout or restart ends in-memory sessions and calls; use a short maintenance window rather than a staged rollout.
