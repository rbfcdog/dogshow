# Tarvo — backend de fechamento financeiro

Backend Python com `uv`, FastAPI, cálculos em `Decimal` e separação entre domínio, aplicação, infraestrutura e HTTP. O pacote está em **`backend/src/`**, conforme a reorganização do projeto; o módulo Python instalado é `src`.

Aplica regras confirmadas pelo financeiro, calcula comissões e divergências, propõe notas, classifica os dois extratos e gera os seis CSVs com rastro. Não executa devoluções, contestações, pagamentos ou emissão real de NF.

## Rodar pelo terminal e gerar os seis CSVs

Na **raiz do clone**, instale `uv` e Python 3.12 (o backend aceita Python 3.12 ou 3.13). Os nove arquivos de `dados_tarvo/` já estão no repositório. Execute estes comandos em um terminal Bash; a chave é lida sem aparecer na tela nem no histórico de comandos:

```bash
uv sync --project backend --locked
read -r -s -p 'OPENAI_API_KEY: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
export OPENAI_MODEL=gpt-4.1-mini
uv run --locked --project backend python -m src \
  --entrada dados_tarvo \
  --competencia 2026-09 \
  --saida saida
```

Use um modelo com acesso a respostas estruturadas na sua conta, caso `gpt-4.1-mini` não esteja disponível. Também é possível configurar `OPENAI_API_KEY` e `OPENAI_MODEL` em `.env` na raiz, sem versioná-lo, em vez de exportar as variáveis. Variáveis exportadas têm precedência na CLI. **O último comando lê `dados_tarvo/` e grava `saida/`; não é preciso iniciar API ou frontend.** Na primeira execução, ele gera automaticamente as propostas bancárias com OpenAI e normaliza os contratos antes de exportar. Se existir `docs/` local, ele consulta esses documentos nas propostas; o diretório não está no repositório. Sem esses documentos, hipóteses sem evidência continuam como pendências.
Durante a execução, o terminal mostra etapas determinísticas e cada consulta/resposta da IA em tempo real (`stderr`, com flush); o resumo final permanece em `stdout`. Falhas não imprimem confirmação de exportação.

Confira os seis arquivos produzidos:

```bash
printf '%s\n' saida/*.csv
```

Os nomes esperados são `comissoes_por_lojista.csv`, `divergencias_taxa.csv`, `notas_a_emitir.csv`, `lancamentos_classificados.csv`, `pendencias.csv` e `rastro.csv`. O rastro liga cada número às linhas de origem; a execução valida suas somas. JSON, cache e logs ficam em `.tarvo-work/`, nunca em `saida/`. Confira no resumo `sem conta` e o número de pendências; erro no modelo, propostas inválidas/obsoletas ou falta de chave interrompem o processo, sem converter falhas em sucesso.

Se aparecer erro de **propostas bancárias obsoletas ou inválidas** em uma execução posterior, gere propostas com as fontes atuais e rode o fechamento novamente (não reutilize aprovação antiga):

```bash
uv run --locked --project backend python -m src.normalizar_bancos gerar \
  --entrada dados_tarvo --competencia 2026-09 \
  --saida .tarvo-work/referencias_bancarias_novas
uv run --locked --project backend python -m src \
  --entrada dados_tarvo --competencia 2026-09 --saida saida
```

As contas com `metodo=IA` são **propostas não aprovadas**: `VALIDAR_CLASSIFICACAO_IA` permanece em `pendencias.csv`, e nada é lançado no ERP. A revisão humana, se houver, usa o comando `normalizar_bancos aprovar` abaixo e o caminho explícito `--referencias-bancarias`. A normalização contratual consulta a LLM em toda execução; totais podem mudar conforme os vínculos evidenciados.

Para o dashboard, após gerar os CSVs, inicie os dois serviços na raiz (o frontend é opcional):

```bash
TARVO_DATA_DIR="$PWD/dados_tarvo" uv run --locked --project backend uvicorn src.main:app --host 127.0.0.1 --port 8000
# Em outro terminal: cd frontend && npm install && npm run build && npm run start
```

## Referências e formato do fechamento

Referências aplicadas:

| Arquivo | Origem | Regra de aplicação |
|---|---|---|
| `.tarvo-work/referencias/referencias.json` | Gerada automaticamente pelo fechamento via OpenAI | Exige hashes e contexto atuais. |
| `.tarvo-work/referencias_bancarias_novas/referencias_bancarias.json` | Gerada automaticamente com OpenAI quando ausente; também pode ser gerada com `normalizar_bancos gerar` | Propostas validadas e aplicadas como IA **não aprovada**, com pendência humana no CSV. |
| Referência bancária revisada (opcional) | `python -m src.normalizar_bancos aprovar` após nova geração | Prevalece sobre propostas com `--referencias-bancarias`; exige aprovação humana válida. |

O arquivo local `revisada-v1.json` teve hashes das fontes e da proposta **reescritos após a revisão**, sem nova aprovação. Não o use como evidência de revisão dos dados atuais. Gere propostas atuais e peça revisão; nunca atualize hashes de uma aprovação antiga para fazê-la passar.

`--referencias`, `--propostas-bancarias` e `--referencias-bancarias` aceitam caminhos explícitos. Se uma proposta existente estiver obsoleta ou inválida, gere-a novamente com `normalizar_bancos gerar`; não reutilize nem aprove automaticamente a versão antiga. JSON, caches e logs ficam fora de `saida/`, que contém somente os seis CSVs pedidos.
O comando termina com código zero quando conclui, e com código 2 e mensagem em caso de entrada inválida. Os arquivos originais não são alterados. O carregador exige `06_orbe_ajustes_tardios_*.csv` além de `01_orbe_relatorio_comissoes_*.csv`, integrando estornos e reversões de antecipação e preservando arquivo e linha física de cada lançamento no rastro.

CSV UTF-8 com `;`, decimal com ponto, dinheiro em reais com duas casas decimais, datas ISO e CNPJ com 14 dígitos. O rastro inclui linha física, aba e descrição da regra como colunas extras.

Todas as saídas monetárias são validadas contra suas contribuições antes da exportação. IDs são determinísticos e a reexecução substitui os arquivos gerados, sem acumular linhas. Falhas de escrita são propagadas; a substituição é por arquivo, não uma transação única envolvendo os seis arquivos.

## Câmbio determinístico

O fechamento em lote e a API executam `close_remittances` sem consultar a LLM: deduplicação por operação/conteúdo econômico, contrato por CNPJ e vigência, comissão de 50% do spread realizado elegível com arredondamento por operação, divergências e NF única para a Ponte. A conferência de recursos é separada da comissão.

Após deduplicar, `remittance_reconciliation.py` confere:

- Por dia: TEDs enviadas à Ponte no Meridiano menos principal + IOF das operações por contratação.
- Por cliente/mês: PIX recebidos menos principal + IOF. Prioriza CNPJ explícito; sem identificador, usa nomes completos normalizados e prefixo com pelo menos dois termos, exigindo CNPJ candidato único. Não usa fuzzy matching nem LLM.
- PIX sem correspondência ou com identificador/nome ambíguo não é distribuído; suspende a comparação mensal para evitar falsos gaps. Ausência de fonte bancária ou operações conflitantes produz pendência sem valor, não caixa zero.

Diferenças saem em `pendencias.csv`, com contribuições assinadas no `rastro.csv`. Valores positivos/negativos são diferenças dos fluxos identificados no recorte, não saldo contábil nem prova de valor a devolver, recebível ou receita. A conferência não altera cadastro, comissão, NF ou classificação bancária. Diferenças diárias e mensais podem se referir aos mesmos recursos e não devem ser somadas como exposição.

No pacote de setembro, a operação `PC-0926-0162` conta uma vez: não há falso gap de R$ 55.811,47 da Bagagem. Os TEDs dos 21 dias fecham. O residual da Ponte Aérea Estudantil é exportado como `RECURSOS_REMESSA_DIVERGENTES`, R$ 18.240,00, com rastro de R$ 470.489,03 em PIX menos R$ 452.249,03 de principal + IOF. É uma pendência de apuração, sem devolução automática. A comissão/NF cambial permanece em R$ 39.543,45.

Regressões específicas e suíte completa:

```bash
uv run --locked --project backend python backend/tests/check_remittance_reconciliation.py
uv run --locked --project backend python backend/tests/check_backend.py
```

Os dois comandos não imprimem saída quando passam. Os controles já integram os seis CSVs e os downloads HTTP existentes; não exigem uma nova etapa de IA.

## IA para extratos e plano de contas

O fluxo bancário é separado da normalização contratual da Orbe. Regras conhecidas continuam no classificador; somente movimentos pendentes ou sem conta seguem para interpretação/RCA. No pacote de setembro, são L50 (PIX sem finalidade conhecida), L65 (motivo da devolução) e L92 (transferência em trânsito).

Gerar sugestões envia contexto selecionado à OpenAI: movimento, plano de contas e evidências documentais. Configure `OPENAI_API_KEY` e `OPENAI_MODEL` no `.env`; variáveis exportadas prevalecem. Não publique referências bancárias brutas nem caches. Em `saida/`, somente os seis CSVs obrigatórios.

Saídas locais da geração bancária: `referencias_bancarias.json`, `classificacoes_sugeridas.csv`, `revisao_humana.csv`, `rca_bancaria.json`, `escaladas_humanas.json` e `cache_llm/`. Propostas válidas entram no CSV de fechamento como `IA`, ainda pendentes de revisão humana. A conta sugerida deve existir no plano, ser analítica, lançável e ter evidência; o schema estruturado restringe `evidence_ids`, `account_code` e `movement_id` aos identificadores reais do contexto, sem alterar valor, data ou identidade do movimento nem inventar NF/baixa. Erro da API interrompe a geração, sem fallback fabricado.

Uma prévia local anterior (`.tarvo-work/referencias_bancarias_novas/previa_classificacao_nao_aprovada.csv`) não é gerada pelo comando de uma instalação limpa e não integra os seis CSVs oficiais. O fechamento aplica as propostas válidas no CSV oficial com `metodo=IA` e deixa a revisão pendente em `pendencias.csv`.

`escaladas_humanas.json` e `revisao_humana.csv` consolidam o dossiê de revisão humana por movimento: dados originais do extrato, conta atual e conta proposta com descrições, separação entre decisão contábil e pendência operacional, nível de autoridade (`CRITERIO_CONFIRMADO_PELO_FINANCEIRO` vs `DECISAO_FUNDAMENTADA_DO_TIME_EM_HIPOTESES`), diagnóstico validado, hipóteses não confirmadas, evidências citadas resolvidas, ações específicas para o revisor e o comando exato de aprovação quando aplicável.

```bash
uv run --locked --project backend python -m src.normalizar_bancos aprovar \
  --entrada dados_tarvo \
  --referencias .tarvo-work/referencias_bancarias_novas/referencias_bancarias.json \
  --saida .tarvo-work/referencias_bancarias/revisada-v2.json \
  --revisor 'NOME DO REVISOR' --todos-validos
```

A revisão não consulta a OpenAI. Registra o nome declarado do revisor, as propostas selecionadas e hashes das fontes/decisões; não é autenticação de identidade nem assinatura criptográfica. O arquivo revisado é separado e não sobrescreve uma versão existente.

O fechamento rejeita propostas de outra competência, fontes alteradas ou contas sem evidência. Sem aprovação humana, a conta sugerida sai como `metodo=IA`, com evidências no rastro e `VALIDAR_CLASSIFICACAO_IA` nas pendências: **não autoriza lançamento no ERP**. Com `--referencias-bancarias` e aprovação genuína, registra também o revisor. Confiança `0.80` não é probabilidade calibrada.

Aprovar uma conta não comprova conciliação com título/NF nem resolve automaticamente origem do PIX, motivo de devolução ou confirmação de crédito futuro. Essas pendências operacionais são preservadas. Sem proposta sustentada, não há conta a aprovar: mantenha o fechamento pelas regras existentes e encaminhe o diagnóstico, sem criar receita ou pagamento.

`--referencias-bancarias` revisada prevalece sobre as propostas; ambas exigem fontes atuais. A rota `POST /api/v1/fechamento/executar`, usada pelo frontend, gera propostas bancárias com LLM quando ausentes e sempre normaliza contratos com LLM.

## Frontend (Dashboard Next.js em Modo Escuro)

Interface web executiva em Next.js 15 e React 19 com tema escuro nativo, baseada nos componentes do Vantara, consumindo os dados apurados e oferecendo os recursos de UX de auditoria e governança:

```bash
cd frontend
./node_modules/.bin/next start --port 3000
# ou para desenvolvimento com recarga automática:
./node_modules/.bin/next dev --port 3000
```

Acessar em `http://localhost:3000`.

O botão **Executar fechamento com LLM** chama `POST /api/run` no Next.js, que chama `POST http://127.0.0.1:8000/api/v1/fechamento/executar?competencia=2026-09`. O backend gera propostas bancárias com LLM quando ausentes, normaliza contratos pela LLM e grava os seis CSVs; propostas existentes obsoletas ou inválidas geram erro. A rota usa `OPENAI_API_KEY` e `OPENAI_MODEL` diretamente do `.env` da raiz, ignorando valores exportados herdados pelo processo HTTP; a CLI continua respeitando variáveis exportadas. A normalização contratual usa `reuse_cache=False`.

### Recursos da Interface

1. **Visão Geral & KPIs:** faturamento consolidado de comissões (checkout e remessa), total de notas fiscais, volume de divergências e contador de lançamentos/pendências com indicador de 100% de batimento no rastro.
2. **Comissões & Notas:** tabelas detalhadas com busca dinâmica por lojista/CNPJ, badges de proveniência (`Contrato Documentado` vs `Vínculo Inferido via RCA`) e botão de atalho para o rastro.
3. **Linhagem & Rastro (Trace UX):** a explicação usa o valor da saída selecionada, as contribuições do `rastro.csv` e, para contratos de checkout vinculados, o motivo e as evidências da decisão LLM em `.tarvo-work/referencias/referencias.json`. Para saídas sem decisão contratual associada, apresenta apenas as fontes e regras registradas; não gera uma nova interpretação por IA.
4. **Divergências & Minutas Acionáveis:** tabela com as divergências e gerador de minutas de e-mail prontas para envio com 1 clique (notificação para a Orbe das 92 diferenças de checkout e contestação para a Ponte Câmbio dos spreads negativos de trava vencida).
5. **Governança ERP & Bancos:** visualização dos 3 níveis de governança do ERP, destaque matemático da conciliação câmbio × banco (sobra de R$ 18.240,00 da Ponte Aérea Estudantil) e tabela de lançamentos bancários classificados.
6. **Casos Humanos & RCA:** dossiês executivos da Atlas Idiomas (57 transações sem contrato 2026) e da Casa do Estudante (isenção verbal sem contrato formal), com botões para gerar pedidos de homologação ao Comercial (Diego).

## API HTTP

```bash
TARVO_DATA_DIR="$PWD/dados_tarvo" \
  uv run --locked --project backend uvicorn src.main:app \
  --host 127.0.0.1 --port 8000
```

Para recarga automática, acrescentar `--reload --reload-dir backend/src`.

| Endpoint | Resultado |
|---|---|
| `GET /health` | Aplicação respondendo; não verifica o pacote |
| `GET /api/v1/diagnostico?competencia=2026-09` | Contagens, valores brutos reportados e achados das fontes |
| `GET /api/v1/fechamento?competencia=2026-09` | Comissões, divergências, notas, classificações, pendências e rastro em JSON |
| `GET /api/v1/fechamento/comissoes_por_lojista.csv?competencia=2026-09` | Download de um dos seis CSVs, usando seu nome exato |
| `GET /docs` | Documentação interativa |
| `GET /openapi.json` | Contrato OpenAPI |

```bash
curl --fail 'http://127.0.0.1:8000/api/v1/fechamento?competencia=2026-09'

curl --fail \
  'http://127.0.0.1:8000/api/v1/fechamento/notas_a_emitir.csv?competencia=2026-09'
```

Os endpoints não escrevem arquivos no disco; o comando em lote faz a exportação. A API recebe apenas a competência e o nome de um arquivo permitido, nunca caminhos de dados arbitrários. `TARVO_DATA_DIR` é configurado pelo operador. Não há leitura automática de `.env`.

Valores `Decimal` são strings no JSON para preservar precisão. HTTP 422 indica competência/pacote inválido; HTTP 503 indica ausência de configuração do diretório. Arquivo de saída desconhecido retorna 404 quando o pacote está disponível. Erros internos ou falha no rastro não são convertidos em sucesso.

`CONCLUIDO_COM_PENDENCIAS` significa que o processamento terminou com casos documentados para decisão ou acompanhamento. Não significa que as contestações tenham sido aprovadas ou que contratos ausentes tenham sido inventados. O diagnóstico mantém somas brutas, inclusive duplicatas; o fechamento aplica elegibilidade, deduplicação e regras comerciais.

A API é local e não possui autenticação. Não publicar essa configuração na internet.

## Regras financeiras aplicadas

### Checkout

- Comissão integral pela taxa contratada, não pelo `valor_taxa` cobrado pela Orbe, sem descontar custo da adquirente.
- Arredondamento `ROUND_HALF_UP` por operação, inclusive negativos, antes de somar. O truncamento observado na Orbe não é a regra da Tarvo.
- Vigência histórica e faixas de modalidade/parcelamento; Bridge passa para a taxa nova em 15/09.
- Estorno entra no mês do movimento, usando o contrato e os campos da venda original. Vendas de agosto não reabrem agosto. Quando a origem não está no pacote, data e modalidade históricas precisam determinar a taxa.
- Antecipação entra na mesma comissão/NF; reversão integral anula a antecipação referenciada. Referência inconsistente ou reversão parcial sem política fica bloqueada e documentada.
- ECs de Novo Horizonte, Horizonte Viagens e Kiwi são associados aos CNPJs confirmados, sem similaridade de nomes.
- Casa do Estudante tem isenção de MDR e antecipação para datas de setembro a dezembro/2026. Estorno de venda de agosto preserva a taxa de agosto.
- Atlas fica sem comissão determinada e sem NF até confirmação comercial. A pendência informa a taxa reportada pela Orbe como dado conhecido, não como comissão calculada.
- Divergência somente quando os percentuais diferem: `bruto × (aplicada − contratada)`, com sinal e arredondamento por linha. Centavos decorrentes apenas do truncamento não geram divergência.
- Uma NF por lojista/mês quando o saldo é positivo. Zero não gera nota; negativo não gera nota nem transporte automático e exige tratamento por Helena.

### Remessas

- Competência por contratação, inclusive liquidações em outubro.
- Uma ocorrência econômica por número de operação. Duplicatas vão para correção do relatório; valores conflitantes bloqueiam aquele ID, sem escolher silenciosamente uma versão.
- Comissão de 50% do spread realizado positivo, arredondada por operação. Negativos são excluídos e contestados; spread estimado nunca substitui realizado.
- Compliance não altera a comissão, incluindo `APROVADO C/ RESSALVA`.
- Spread contratual vazio/“padrão” significa 1,5% sobre PTAX. Mundo Aberto tem 1,20%.
- Divergência integral assinada = `valor_ME × PTAX × (spread_aplicado − contratado)`, não metade da diferença.
- Tolerância adotada pelo time: diferenças absolutas menores que 0,01 ponto percentual não são reportadas; esse limite não foi imposto pelo financeiro. Justificativa em [HIPOTESES.md](HIPOTESES.md).
- O excesso realizado de Mundo Aberto permanece na comissão e na nota. Devolução futura só afeta o mês do ajuste.
- Contrato ausente/ambíguo bloqueia a comparação contratual, não os 50% do realizado conhecido; não há taxa contratada fictícia.
- Uma NF agregada para Ponte Câmbio. Conferir o recebimento futuro é pendência separada, sem prever TED líquido de descontos.

### Bancos e ERP (Três Níveis de Governança)

- **Nível 1 (Regras Determinísticas + Trava de Conta Analítica):** Valida todas as movimentações contra `04_plano_de_contas_fenix_erp.csv` (bloqueando contas sintéticas e não lançáveis), pareia transferências próprias (Aurora ↔ Meridiano por data e valor invertido), separa aplicação de CDB (`1.1.02.001`) de rendimento (`3.2.01`) e baixa obrigações de folha e tributos de agosto no passivo (`2.1.03.xxx` e `2.1.04.xxx`).
- **Nível 2 (Conciliação Cruzada Câmbio × Banco — `remittance_reconciliation.py`):** Cruza diariamente os TEDs enviados à Ponte no Meridiano com principal + IOF das operações por contratação, e mensalmente os PIX recebidos com as operações únicas da Ponte, comprovando matematicamente a sobra de **R$ 18.240,00 da Ponte Aérea Estudantil** (`MRD2026090425`), registrada como pendência `RECURSOS_REMESSA_DIVERGENTES` com rastro assinado.
- **Nível 3 (RCA Bancária com IA + Gate de Aprovação Humana — `normalizar_bancos.py`):** Para os lançamentos com dúvida (como o PIX de R$ 7.350 sem identificação ou a devolução de R$ 12.400 da Mundo Aberto), a IA lê as evidências documentais, sugere a conta analítica e gera o comando de aprovação humana (`--revisor`), com trava estrita no código que impede a IA de sobrescrever qualquer lançamento determinístico de confiança 1.0.

## Resultado exercitado em setembro

O fechamento de setembro grava em `saida/` os seis CSVs definidos no enunciado. As propostas bancárias validadas foram aplicadas como `IA`, não como `HUMANO`: cada uma possui pendência `VALIDAR_CLASSIFICACAO_IA` antes da escrituração. O valor das comissões e das notas continua determinado exclusivamente pelo núcleo `Decimal` sobre as linhas de origem; classificações bancárias não alteram dinheiro nem eliminam pendências de conciliação.

O comando padrão exige chave, modelo e rede para nova normalização contratual. `--referencias-bancarias` permite usar uma versão realmente revisada em lugar das propostas. Fórmulas são determinísticas, mas novos vínculos contratuais podem mudar números entre execuções; consulte os próprios CSVs e o resumo impresso para os totais da execução.

Casa do Estudante fecha com saldo de checkout em −R$ 5,50 por estorno de venda de agosto (vendas de setembro isentas) e não recebe NF; Atlas Idiomas fica bloqueada sem NF aguardando confirmação contratual. Pendências incluem bases distintas, como taxas reportadas, diferenças contratuais, recebimentos a conferir e movimentos bancários: **não somar todas como exposição financeira ou receita perdida**.

## Arquitetura

```text
backend/
├── pyproject.toml
├── uv.lock
├── src/
│   ├── domain/
│   │   ├── models.py
│   │   ├── closing.py
│   │   ├── policies.py
│   │   ├── normalization.py
│   │   └── banking_reference.py
│   ├── application/
│   │   ├── diagnostics.py
│   │   ├── checkout.py
│   │   ├── remittances.py
│   │   ├── banking.py
│   │   ├── normalization.py
│   │   ├── banking_reference.py
│   │   ├── remittance_reconciliation.py
│   │   └── closing.py
│   ├── infrastructure/
│   │   ├── parsing.py
│   │   ├── readers.py
│   │   ├── normalization_sources.py
│   │   ├── banking_sources.py
│   │   ├── openai_normalizer.py
│   │   └── exports.py
│   ├── api/routes.py
│   ├── main.py
│   ├── normalizar.py
│   ├── normalizar_bancos.py
│   └── __main__.py
└── tests/
    ├── check_backend.py
    ├── check_checkout.py
    ├── check_remittances.py
    ├── check_banking.py
    ├── check_normalization.py
    ├── check_banking_reference.py
    ├── check_remittance_reconciliation.py
    └── check_late_orbe.py
```
O núcleo financeiro usa dataclasses e biblioteca padrão; schemas de propostas LLM usam Pydantic. Aplicação e infraestrutura dependem do domínio; API e execução em lote compõem as camadas. Não há banco ou contêiner de DI. O build do `uv` configura `module-name = "src"` e `module-root = ""` para preservar esse layout.

Cada consulta relê o pacote e trabalha em memória; o custo cresce com volume/concorrência. Uma fotografia imutável por fechamento é a evolução indicada quando esse custo se tornar relevante.

## Verificação

```bash
uv run --locked --project backend python backend/tests/check_backend.py
```

O comando termina sem prints quando passa; falhas geram `AssertionError` e código não zero. Não usar `python -O`, que desativa os asserts.

Cobre arredondamento, contratos, notas, rastro, conta analítica e HTTP. Também valida a aplicação das propostas como `IA` sem fingir aprovação, pendências operacionais, rejeição de fontes obsoletas e aplicação de decisões humanas apenas quando realmente revisadas.

Em clone isolado de `HEAD` com estas alterações locais aplicadas, **sem Markdown em `docs/`**, `uv sync --project backend --locked` e o comando de fechamento acima (chave fornecida por `--env-file` externo) geraram as duas referências com OpenAI e exatamente seis CSVs: 35 comissões, 238 divergências, 21 notas, 184 lançamentos, 475 pendências e 5.172 contribuições do rastro, todas reconciliadas por `id_saida`. A IA sugeriu 109 contas; **Aurora/L65 permaneceu sem `conta_codigo` e com `CLASSIFICACAO_BANCARIA` pendente**, pois faltam origem, motivo e obrigação comprovados para a devolução Pix no pacote publicado. Numa execução local com documentos adicionais houve proposta para L65, ainda não aprovada. Totais e cobertura podem variar com evidências e respostas do modelo: confira o resumo e as pendências da própria execução.

## Fontes e limites

- Regras confirmadas, hipóteses e fontes: [HIPOTESES.md](HIPOTESES.md).
- Histórico de prompts (pedidos antigos resumidos; último pedido preservado literalmente): [PROMPTS/HISTORICO.md](PROMPTS/HISTORICO.md).
- Momentos relevantes de uso da IA: [PROMPTS/MOMENTOS.md](PROMPTS/MOMENTOS.md).

`docs/` permanece local e não é publicado. Quando fornecido, seus arquivos entram nos hashes das propostas bancárias; numa instalação nova sem documentos locais, a IA gera propostas a partir de `dados_tarvo/`. `saida/` contém exatamente os seis CSVs do enunciado. Credenciais em `.env`, caches e referências não revisadas em `.tarvo-work/` não são publicados; o comando de fechamento acima gera as propostas ausentes automaticamente.

Os leitores reconhecem os papéis e cabeçalhos atuais de Orbe, contratos, e-mail Ponte, plano de contas, Aurora CSV e Meridiano OFX 1.02/SGML. Não prometem aceitar layouts diferentes. Uma pasta deve ter exatamente um arquivo por papel; múltiplos candidatos geram erro explícito. CNPJs fictícios são normalizados sem validar dígitos verificadores.

A geração usa OpenAI com respostas estruturadas. Propostas bancárias não aprovadas podem preencher o CSV como `IA` e permanecem pendentes de validação antes de qualquer postagem; não são decisões humanas. Referências, logs e caches ficam em `.tarvo-work/`, fora dos seis CSVs de `saida/`. O fechamento chama novamente a LLM contratual e falha sem credencial ou rede. Nenhuma ação financeira externa é autorizada.
