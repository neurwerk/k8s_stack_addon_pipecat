# Private scripts

The bundled public example needs no object store. S3-loaded apps are trusted administrator code, not user uploads or a sandbox; they run with the bot's credentials and network permissions. All Python dependencies must already be in the reviewed image; neither init nor runtime installs dependencies.

## App source and commands

Each app is a direct child of an explicitly selected apps directory. The sample is `apps/simple-chatbot/`; its root `app.yaml` has exactly these fields:

```yaml
id: simple-chatbot
runtimeVersion: "0.2.0"
entrypoint: server.py
requirements: null
files:
  - server.py
```

The ID must match the folder; `runtimeVersion` selects the supported bundle format, independently of image and tooling versions. The entrypoint starts the app-owned server. List supporting files explicitly; never include transcripts or credentials. Optional dependency locks require exact versions and SHA-256 hashes, with dependencies already present in the runtime image. Packaging generates `manifest.json`, rejects unsafe paths/links and common credential files, and bounds file count and archive size. Review the allowlist and source; these checks are not a complete secret scanner.

From the add-on checkout, use the locked tooling environment:

```sh
uv run --locked --project tooling pipecat-apps list --apps-dir ./apps
uv run --locked --project tooling pipecat-apps validate --apps-dir ./apps --app simple-chatbot
uv run --locked --project tooling pipecat-apps interactive --apps-dir ./apps \
  --client example --context example-context --namespace pipecat \
  --client-repo /work/client --values /work/client/products/pipecat/values.yaml
```

Client paths and identities are explicit, not discovered from a universal layout. Before first publication, add a reviewed `pipecat.bots` entry with the matching ID and configure Keycloak, access policy and credential delivery. The CLI does not generate these approvals. Interactive mode selects an app and action; `publish`/`update`, `enable`/`disable`, `status` and bounded `logs` are also available as subcommands.

## Storage and rollout

Provision the bucket and separate reader/publisher users before the first upload. For optional Rook storage, set `pipecat.scriptStorage.enabled`, `store`, `clusterNamespace` and `bucket`; the shared chart declares retained storage and users. Allow Rook users in the `pipecat` namespace and wait for bucket binding and generated Secrets. Set `script.secretName` to the read-only `rook-ceph-object-user-<store>-pipecat-script-reader` Secret, with `script.accessKeyKey: AccessKey` and `script.secretKeyKey: SecretKey`; the default key aliases are `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Never mount publisher or bucket-owner credentials in bots.

The publisher uses ordinary approved workstation AWS credentials or a named profile, never credentials in command arguments. `--endpoint` is the workstation upload endpoint; `--runtime-endpoint` selects the in-cluster endpoint on first publication or an explicit change. Existing runtime endpoints, Secret references and key aliases are preserved. Uploads require HTTPS unless `--allow-local-http` explicitly permits loopback port-forward access; the first HTTP upload also requires `--runtime-endpoint`. Runtime HTTP service endpoints do not relax the upload rule.

Publishing creates a deterministic, content-addressed immutable object and verifies its SHA-256. For pinned apps it updates only the selected app's `script` fields in the client Git working tree; review, commit and push explicitly for Flux to replace the app. For channel followers it advances the channel without editing client files. Replacement ends that app's active calls. `enabled: false` removes its workload without removing identity or credential setup. Runtime's init container verifies the archive before exposing read-only `/scripts/bundle`; the bot container receives no S3 credentials. Retain storage and users on removal; deletion needs separate approval.

## Automatic release channels

After adopting a channel-capable release, set `script.channel` (for example `dev`) and `script.runtimeVersion` on each follower. Keep its exact `key` and `sha256` as the bootstrap bundle. Configure `pipecat.channels.image` with the release's verified tooling image, plus `apiServerCIDR` and `apiServerPort` for Kubernetes API egress. The controller uses the existing read-only S3 account and polls every `intervalSeconds` (default 30).

`publish` and `update` advance the configured channel after verifying the stored archive. The controller updates only the selected Deployment's `pipecat.neurwerk.com/channel-release` pod annotation; Helm retains the workload configuration. The loader verifies app identity, source settings, supported bundle format and checksum. Missing annotations or annotations for an old source policy use the bootstrap bundle. Failed verification preserves the previous selection; a failed rollout requires operator correction or rollback.

To select a previously uploaded bundle, including rollback, no local app source is needed:

```sh
uv run --locked --project tooling pipecat-apps promote --app "<app>" \
  --client "<client-id>" --context "<context>" --namespace "<bot-namespace>" \
  --client-repo "<client-repo>" --values "<product-values-file>" \
  --endpoint "<https-upload-endpoint>" --bucket "<bucket>" --sha256 "<verified-checksum>"
```

Promotion verifies the archive again and writes an immutable history record before conditionally updating `pipecat/<app>/channels/<channel>/current.json`. Concurrent publishers cannot silently overwrite one another. Failed attempts can leave history records that were never selected; inspect the current pointer before retrying an uncertain publication. Use `pipecat-apps status` with explicit app, client, context and namespace, then verify app health and features. Channel following is eventual; a publication arriving during a rollout is picked up on a later poll.

## Upload from any workstation

Use a machine with Python, uv, kubectl and approved cluster access. Start from the reviewed app source with its matching built frontend and dependency files; these can come from that revision's successful CI build. Pinned apps need a separate client Git worktree; channel followers only read the selected client values.

1. Use the existing approved publisher account through an AWS profile or temporary process environment. If credentials come from a Kubernetes Secret, load them without printing or saving them; do not use the bot's read-only account.
2. For an internal S3 service, keep this command running in a separate terminal (replace the placeholders):

   ```sh
   kubectl --context "<context>" -n "<storage-namespace>" port-forward service/<s3-service> 18993:<service-port> --address=127.0.0.1
   ```

3. From the add-on checkout, validate and upload:

   ```sh
   uv run --locked --project tooling pipecat-apps validate --apps-dir apps --app "<app>"
   uv run --locked --project tooling pipecat-apps update --apps-dir apps --app "<app>" --client "<client-id>" --context "<context>" --namespace "<bot-namespace>" --client-repo "<client-worktree>" --values "<product-values-file>" --endpoint http://127.0.0.1:18993 --allow-local-http --bucket "<bucket>"
   ```

   This preserves the existing in-cluster endpoint and verifies the uploaded checksum. For a directly reachable HTTPS endpoint, use that URL and omit `--allow-local-http`.
4. For pinned apps, review and validate the client diff, then commit and merge through the normal deployment process. Channel followers deploy automatically without a client diff. Verify the bot is ready and its page loads.
5. Stop the port-forward and clear temporary credentials. Keep machine-specific profiles, paths and settings outside the repository.
