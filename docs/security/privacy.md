# Segurança — Privacidade

## Classificação de dados

`DataClassification` no contrato `Evidence` (e usada pelo pipeline):

| Classe | Tratamento default (`capture=metadata`) |
|---|---|
| `metadata` | persistido |
| `user_prompt`, `model_response` | descartado (`_forge_lens.dropped`) |
| `tool_arguments`, `tool_outputs` | descartado |
| `personal_data` | e-mails mascarados; demais via chave sensível |
| `credentials` | `[REDACTED]` sempre (qualquer capture level) |
| `sensitive_code` | truncado/redigido conforme padrões |
| `unknown` | tratado conservadoramente |

## O que nunca sai da máquina

Nada — o produto não envia telemetria, não chama endpoints externos e não
tem "phone home". Rede existe só entre seu SDK e seu servidor local.

## Recomendações

- `capture=full` só para depuração temporária — corpos de prompt podem
  conter segredos do *seu* usuário final.
- Rode `api_key` quando a porta for acessível a outras pessoas.
- `BlobRef` aponta payloads; o conteúdo é decisão do produtor — classifique
  via `metadata`/`classification` e respeite sua política interna.
- `.forge-lens/lens.db` pode conter dados de execuções: trate como artefato
  sensível (backup/cópia/expurgo conscientes).
