# Pipecat tooling

`tooling/` packages `pipecat-tooling` / `pipecat_tooling` with `pipecat-validate`. It imports the exact shared Tooling dependency for manifest parsing and ordinary values loading. Product code checks bot IDs, gateway permissions, independent speech choices and feature agreement. Credential recipes, readers and lifecycle targets live only in `release/manifest.yaml`; privileged preparation stays in shared setup.

`make tooling-check` requires the reviewed release dependency lock. Publish shared SDK `v0.14.0` first, then setup `stack-setup-v0.7.0`; their separate wheel URLs are declared in `tooling/pyproject.toml`. Generate `tooling/uv.lock` only after both artifacts are available; never commit a candidate-local source or temporary lock. Required CI installs development dependencies, including exact setup `0.7.0`, and runs `make tooling-integration ADDON_BASE=/absolute/path/to/reviewed-base`. Missing setup or Base fails rather than skips. Candidate wheels installed outside Git can exercise the same target with an explicitly selected runner; they do not satisfy the release lock gate.

The image uses one locked environment on pinned Python/uv inputs, runs as UID 65532, and installs no dependencies at startup. `make tooling-build VERSION=<version> REVISION=<commit>` builds `ghcr.io/neurwerk/k8s-stack-addon-pipecat-tooling` from `tooling/`. Optional consumer declarations permit disabled bots, browser/machine identities, private scripts and TURN to be absent; when present, shared validation still checks their exact declared images.

The installed command supports:

```sh
pipecat-validate /work/addon --list-packages
pipecat-validate /work/addon --values /work/client/config/client.yaml --values /work/client/apps/pipecat/values.yaml --selection /work/client/config/addons.yaml
```

It validates only; it neither generates a second plan nor reads credentials. Shared setup verifies the complete selection and source revisions before custody. Runtime commands `pipecat-bundle` and `pipecat-turn-config` stay in the runtime image. There are no product-specific credential, Job-launch or refresh engines.

See [selection and lifecycle](../architecture/README.md) and [release operations](../operations/README.md).
