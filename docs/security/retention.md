# Segurança — Retenção

## Estado atual

- **Expurgo manual via CLI/API**: `forge-lens prune` apaga traces por
  projeto e por idade (`--before <iso>` ou `--days N`; sem flag usa
  `retention_days` da config). `POST /v1/admin/prune` espelha isso para
  admins (`all_projects: true` exige admin sem escopo).
- **Arquivamento frio antes do expurgo**: `forge-lens archive` exporta
  JSONL por trace (`--delete` apaga após exportar — e só o que foi
  exportado; `--append` preserva um arquivo existente).
- **Sem auto-expurgo em background**: nada roda sozinho — retenção é
  sempre uma ação explícita do operador.
- Estado concentrado em `.forge-lens/lens.db` (+ `-wal`/`-shm`) —
  apagar o arquivo = expurgo total e definitivo.
- Re-ingestão idêntica não duplica dados (dedupe por PK).

## Política planejada (roadmap)

| Alvo | Proposta |
|---|---|
| `max_db_bytes` | pressão: rejeita ingest (fail-safe) ou drop mais antigo (fail-open) — decisão do operador, documentada |
| Por classificação | prompts/credentials já nascem descartados por capture level |
| Expurgo agendado | cron/scheduler externo chamando `prune` — não há daemon interno |

## Comandos

```bash
# expurgo: últimos 30 dias do projeto atual
forge-lens prune --days 30 --yes

# exporta e apaga uma trace específica
forge-lens archive <trace_id> --project meu-app --delete --yes
```
