# Private scripts

The bundled public example needs no object store. S3-loaded apps are trusted administrator code, not user uploads or a sandbox; they run with the bot's credentials and network permissions. All Python dependencies must already be in the reviewed image; neither init nor runtime installs dependencies.

## App source and commands

Each app is a direct child of an explicitly selected apps directory. The sample is `apps/simple-chatbot/`; its root `app.yaml` has exactly these fields:

```yaml
id: simple-chatbot
runtimeVersion: "0.1.0"
files:
  - bot.py
  - LICENSE.txt
```

The ID must match the folder; `runtimeVersion` must equal the selected runtime package version, independently of the tooling version. `bot.py` exports `async run_bot(connection, settings, http, credential, notice)`. The sample wraps the runtime's authenticated, PII-aware pipeline. List supporting files explicitly; never include transcripts, credentials or dependencies. Packaging generates `manifest.json`, rejects unsafe paths/links and common credential files, and bounds file count and archive size. Review the allowlist and source; these checks are not a complete secret scanner.

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

Publishing creates a deterministic, content-addressed immutable object, verifies its SHA-256, then updates only the selected app's `script` fields in the client Git working tree. Review the diff, commit and push explicitly; Flux performs the replacement, ending that app's active calls. `enabled: false` removes its workload without removing identity or credential setup. There is no direct deployment patch or restart command. Runtime's init container verifies the archive before exposing read-only `/scripts/bundle`; the bot container receives no S3 credentials. Retain storage and users on removal; deletion needs separate approval.
