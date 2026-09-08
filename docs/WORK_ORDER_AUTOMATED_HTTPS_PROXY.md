# Work Order — Fase 4: proxy HTTPS automatizado

## Objetivo

Implementar exatamente o primeiro item ainda não concluído de `docs/ROADMAP.md` na Fase 4 — **proxy HTTPS automatizado** — preservando o modelo de acesso remoto privado já documentado.

A documentação atual define que a aplicação inicia em HTTP local e que o HTTPS remoto deve ser encerrado por **Tailscale Serve**, somente dentro da tailnet, com **Funnel desabilitado**. Este slice deve automatizar esse estado operacional de forma idempotente e verificável; não transformar a aplicação em serviço público nem antecipar os demais itens da Fase 4.

## Fonte de verdade

Ler antes de implementar e confrontar este Work Order com:

- `docs/ROADMAP.md`;
- `docs/SECURITY.md`;
- `docs/ARCHITECTURE.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `README.md`, `compose.yaml`, `.env.example` e scripts/runbooks operacionais existentes.

Se algum contrato técnico atual demonstrar que Tailscale Serve não é a implementação correta para este item, Claude deve abrir Technical Challenge com evidência concreta antes de substituir a arquitetura. Não decidir por preferência de ferramenta.

## Escopo mínimo

1. Automatizar a configuração/reaplicação do proxy HTTPS privado sobre a aplicação local usando o mecanismo já normativo de Tailscale Serve.
2. O procedimento deve ser idempotente: repetir a execução deve convergir para o mesmo estado desejado, sem criar regras duplicadas ou exposição adicional.
3. Validar pré-requisitos e falhar de forma explícita quando Tailscale não estiver instalado, autenticado/conectado, ou quando a aplicação alvo não estiver disponível/configurada conforme o contrato.
4. Manter **Tailscale Funnel desabilitado** e não abrir porta pública na internet.
5. Não publicar PostgreSQL, Advisor/Codex, volumes de documentos, endpoints internos ou qualquer serviço além da superfície web já prevista.
6. Não introduzir credenciais, auth keys, secrets, hostnames privados reais ou dados do usuário no repositório. Qualquer parâmetro configurável deve usar placeholder/variável segura e documentação correspondente.
7. Preservar cookies/sessão e autenticação da aplicação; não alterar regras financeiras, invariantes, dados, migrations ou contratos determinísticos.
8. Fornecer verificação operacional clara do estado final (HTTPS privado ativo, alvo correto, ausência de Funnel/exposição pública) e procedimento de rollback/desativação que não apague dados.
9. Não antecipar perfis admin/consulta, MFA, backup externo, observabilidade/alertas ou a rotina geral de atualização/rollback da Fase 4, exceto o rollback estritamente necessário deste proxy.

## Critérios de aceite

- Existe um caminho automatizado e documentado para colocar a aplicação atrás de HTTPS privado conforme `docs/SECURITY.md`.
- A automação é segura para múltiplas execuções e não depende de edição manual destrutiva.
- O estado esperado de Tailscale Serve é verificável após a execução.
- Funnel permanece desabilitado e nenhuma porta pública é criada.
- PostgreSQL e Advisor permanecem inacessíveis pela superfície do proxy.
- Falhas de pré-requisito são explícitas e não deixam configuração parcial silenciosa.
- Nenhum segredo real é versionado e logs/saídas não exibem segredos.
- Compatibilidade com a topologia atual de Docker/Windows/WSL é preservada conforme a documentação existente; não inventar uma nova topologia sem Technical Challenge.
- Os 12 gates existentes permanecem verdes no mesmo head final; adicionar testes específicos de scripts/configuração quando tecnicamente aplicável.

## Segurança e proibições

`docs/SECURITY.md` é normativo neste slice: acesso remoto continua privado por identidade/dispositivo Tailscale, sem porta pública; a aplicação não deve ser exposta diretamente à internet; PostgreSQL nunca deve ser publicado; `.env`, chaves de criptografia e documentos não podem aparecer em código, fixtures, logs ou comentários de PR.

Não alterar `FINANCIAL_RULES`, `FINANCIAL_INVARIANTS`, cálculos, snapshots, reconciliação, migrations ou dados para viabilizar infraestrutura. Não adicionar Caddy/Nginx/Traefik ou outro proxy apenas por preferência se Tailscale Serve satisfizer o contrato existente; qualquer mudança arquitetural exige evidência e Technical Challenge. Não habilitar Funnel. Não fazer merge.

## Testes obrigatórios

Cobrir ao menos, por testes automatizados e/ou validações determinísticas de script conforme a plataforma permitir:

- execução idempotente/repetida;
- pré-requisito Tailscale ausente ou não disponível falha com mensagem segura;
- configuração aponta apenas para a superfície web esperada;
- ausência de publicação de PostgreSQL/Advisor;
- Funnel não é habilitado pelo fluxo;
- parâmetros/saídas não contêm segredo real;
- rollback/desativação do proxy não toca banco, documentos ou fatos financeiros;
- sintaxe dos scripts/configuração e regressão dos 12 gates existentes.

Testes não podem exigir tailnet real, credencial real ou acesso externo no CI; use mocks/fakes/inspeção determinística do comando quando necessário.

## Relação de revisão

Claude é o executor e não pode fazer merge. Pode e deve contestar tecnicamente este Work Order, critérios de aceite ou orientação do engenheiro quando houver evidência concreta em documentação, comportamento verificável, segurança ou menor risco de regressão. Divergência não resolvida bloqueia merge.
