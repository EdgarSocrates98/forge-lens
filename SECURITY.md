# Política de Segurança — Forge Lens

## Reportar vulnerabilidades

Reporte de forma privada via GitHub Security Advisories do repositório
(ou contato do mantenedor no perfil). **Não abra issue pública** para
vulnerabilidades não divulgadas.

## Modelo de segurança (resumo)

- **Local-first**: o serviço roda em `127.0.0.1` por padrão e o store é SQLite
  local. Não há telemetria obrigatória nem dependência externa.
- **Sanitização por padrão** (`capture=metadata`): chaves de credenciais são
  redigidas, corpos de prompt/tool são descartados com marcador explícito,
  e-mails são mascarados, valores longos são truncados com marcador.
- **Auth opcional em três modos** (`auth_mode` / `FORGE_LENS_AUTH_MODE`):
  `none` (default local) deixa tudo aberto; `key` exige `X-Forge-Lens-Key`
  igual a `api_key` configurada nas escritas (leituras continuam abertas,
  a menos que `auth_reads=true`); `keys` ativa chaves gerenciadas com
  papéis `reader|writer|admin` e escopo de `projects[]` — nesse modo toda
  rota exige uma chave válida e `project_id` é a fronteira de tenant.
  Se expuser a porta, proteja-a também no nível de rede.
- **Sem auto-execução**: o produto nunca re-executa ferramentas ao abrir um
  trace. Replay observado é read-only por design.

Ver `docs/security/threat-model.md` e `docs/security/privacy.md` para o modelo
completo e a classificação de dados.

## Escopo suportado

| Versão | Suporte |
|--------|---------|
| 0.1.x  | ciclo de fundação — aceita reports, sem SLA formal |
