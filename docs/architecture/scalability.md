# Arquitetura — Escalabilidade

## Perfil alvo (local-first)

Single-user/team workstation: centenas de milhares de spans confortavelmente
em SQLite WAL; milhões são possíveis com atenção a índices e vacuum.

## Gargalos conhecidos e mitigações

| Ponto | Limite prático | Mitigação |
|---|---|---|
| Escrita SQLite | ~milhares de spans/s | lote por trace, WAL, `synchronous=NORMAL` |
| `attributes_json` grandes | cresce rápido em `capture=full` | `metadata` default + cap de chars |
| `list_traces` | paginação (≤500) | índices por project/status/session |
| Flush por trace | traces gigantes pressionam memória do SDK | `flush()` parcial suportado |
| Concorrência | 1 writer SQLite | fila no pipeline (futuro: WAL2/worker) |

## Caminho de escala (roadmap)

- **Export Parquet** para arquivamento/analytics offline (ciclo storage+).
- **Partição por projeto/data** no esquema já preparado (`project_id` na PK).
- **Backend alternativo** via `TraceStore` protocol se um dia ClickHouse
  fizer sentido — *opcional*, nunca obrigatório.
- **Recomendação honesta**: se a carga for "muitos agentes de uma org
  centralizando", o local-first correto é *uma instância por operador* +
  export de artefatos, não um Lens multi-tenant gigante.
