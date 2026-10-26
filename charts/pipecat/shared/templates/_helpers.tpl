{{- define "pipecat.managedBot" -}}
{{- $bot := .bot -}}
{{- $name := printf "pipecat-bot-%s" $bot.id -}}
{{- $ref := $bot.gatewaySecretRef | default dict -}}
{{- $canonical := or (empty $ref) (and (eq ($ref.name | default "") $name) (eq ($ref.key | default "") "gatewayApiKey")) -}}
{{- if and (not (empty $ref)) (hasPrefix "pipecat-bot-" $ref.name) (not $canonical) -}}{{ fail "Managed gateway Secret references must match their bot ID and gatewayApiKey field" }}{{- end -}}
{{- $sync := .product.secretSync | default dict -}}
{{- if and ($sync.enabled | default false) ($bot.anonymous | default false) $canonical (not (empty $bot.gatewayPermissions)) -}}true{{- end -}}
{{- end -}}
