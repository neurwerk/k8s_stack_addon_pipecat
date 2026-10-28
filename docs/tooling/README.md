# Pipecat tooling

`tooling/` packages `pipecat-tooling` / `pipecat_tooling` with `pipecat-validate` and `pipecat-apps`. It imports the exact shared Tooling dependency for manifest parsing and ordinary values loading. Product validation checks bot IDs, mandatory OIDC, gateway permissions, speech choices and feature agreement; disabled apps retain setup ownership. Credential recipes and readers live in `release/manifest.yaml`; privileged preparation stays in shared `stack-setup`, not a product lifecycle engine.

`mise exec -- make tooling-check BASE=/absolute/path/to/reviewed-base` checks the genuine dependency lock, lint, formatting, types, integration tests and package build. SDK and standalone setup wheel sources are pinned separately in `tooling/pyproject.toml` and `uv.lock`; no sibling checkout is needed. Missing setup or Base fails rather than skips. Candidate-local packages do not replace published dependency pins or establish release availability.

The image uses one locked environment on pinned Python/uv inputs, runs as UID 65532, and installs no dependencies at startup. `make tooling-build VERSION=<version> REVISION=<commit>` builds `ghcr.io/neurwerk/k8s-stack-addon-pipecat-tooling` from `tooling/`. Optional consumer declarations permit disabled bots, browser/machine identities, private scripts and TURN to be absent; when present, shared validation still checks their exact declared images.

The installed command supports:

```sh
pipecat-validate /work/addon --list-packages
pipecat-validate /work/addon --values /work/client/config/client.yaml --values /work/client/products/pipecat/values.yaml --selection /work/client/config/addons.yaml
```

`pipecat-validate` validates only; it neither generates a second plan nor reads credentials. `pipecat-apps` provides interactive and noninteractive source validation, immutable S3 publishing, selected client-value edits, enable/disable, status and bounded logs; see [app format and exact commands](../scripts.md). It does not commit, push or patch deployments. Shared setup verifies complete selection and source revisions before custody. Runtime commands `pipecat-bundle` and `pipecat-turn-config` stay in the runtime image. There are no product-specific credential, Job-launch or refresh engines.

See [architecture](../architecture/README.md) and [release operations](../operations/README.md).
