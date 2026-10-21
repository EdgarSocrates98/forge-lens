# Segurança — Retenção

## Estado atual (ciclo 01, honesto)

- **Sem auto-expurgo**: o SQLite cresce até o operador agir.
- Estado concentrado em `.forge-lens/lens.db` (+ `-wal`/`-shm`) —
  apagar o arquivo = expurgo total e definitivo.
- Re-ingestão idêntica não duplica dados (dedupe por PK).

## Política planejada (roadmap)

| Alvo | Proposta |
|---|---|
| `retention_days` | job interno que apaga traces antigas por `started_ns` |
| `max_db_bytes` | pressão: rejeita ingest (fail-safe) ou drop mais antigo (fail-open) — decisão do operador, documentada |
| Export antes do purge | Parquet/JSONL para arquivamento frio |
| Por classificação | prompts/credentials já nascem descartados por capture level |

## Hoje

```bash
# expurgo manual por projeto
sqlite3 .forge-lens/lens.db "DELETE FROM spans WHERE project_id='x'; \
       DELETE FROM traces WHERE project_id='x'; VACUUM;"
```
