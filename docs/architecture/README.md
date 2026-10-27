# Selection and lifecycle

Each selected bot has one `bot` instance in client selection, including UI-only bots. There are 0–32 instances. The parser prefixes internal record/input IDs with the instance ID and substitutes `{instance}` only in explicitly varying resource names and paths. Shared SecretStore, ServiceAccount and role names remain literal.

Global features are `oidc`, `turn`, `managed-oidc` and `managed-turn`. The managed flags equal the active feature AND `secretSync.enabled`. Per-bot features are `gateway`, `stt`, `tts` and `runtime`; `runtime` equals the OR of the three credential features and gates its delivery wait. Product validation rejects any mismatch. `sessionsEnabled` never gates credential preparation.

`gateway` selects an anonymous managed bot with canonical gateway reference and approved permissions. Protected bots use delegated user tokens only. Optional login does not change an anonymous bot's credential mode. `stt` and `tts` independently select exact canonical credential references. External references and authless providers do not select supplied inputs.

Each managed gateway bot contributes three isolated records: runtime raw key, machine client secret and gateway verifier. Speech keys share only the bot runtime record. A speech-only bot contributes one record. Maximum selection has 98 records and 99 ExternalSecret deliveries, including browser and TURN records. No human handover or automatic rotation is defined.

Stage delivery objects before privileged setup. ExternalSecret readiness includes SecretStore readiness; disabled credentials have no waits. Provision identities, then grant descriptors and bridge registration. Flux owns the final app deployment, certificates and optional storage. Explicit restart targets are bot Deployments and enabled TURN; ordinary preparation does not restart them. Retain namespace, credentials and optional storage on removal; remove access through shared setup.

[Synthetic selection](../../validation/addons.yaml) and [matching product values](../../validation/product-selection-values.yaml) show exact names for client authors. They select no runtime image and are not deployment releases. The shared model reads credential definitions; native machine Jobs enforce the client-selected model/provider scopes through the shared scoped helper.
