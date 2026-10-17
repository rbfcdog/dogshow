# Tarvo — backend de fechamento financeiro

Backend Python com `uv`, FastAPI, cálculos em `Decimal` e separação entre domínio, aplicação, infraestrutura e HTTP. O pacote está em **`backend/src/`**, conforme a reorganização do projeto; o módulo Python instalado é `src`.

Aplica regras confirmadas pelo financeiro, calcula comissões e divergências, propõe notas, classifica os dois extratos e gera os seis CSVs com rastro. Não executa devoluções, contestações, pagamentos ou emissão real de NF.

## Como rodar a solução completa (Backend + Frontend)

Para visualizar o sistema (a leitura do painel usa os CSVs já gerados; o botão **Executar** dispara novo fechamento com LLM):

```bash
# Terminal 1 — Backend FastAPI (porta 8000):
TARVO_DATA_DIR="$PWD/dados_tarvo" uv run --locked --project backend uvicorn src.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — Frontend Next.js (porta 3000), após configurar a chave:
cd frontend
npm install
npm run build
npm run start
```

- **Dashboard Executivo:** [http://localhost:3000](http://localhost:3000)
- **Documentação da API (Swagger):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Status de Saúde:** [http://localhost:8000/health](http://localhost:8000/health)

## Instalação e Requisitos

Requisitos: `uv`, Python 3.12 (versão exercitada: 3.12.14) e Node.js para o frontend.

Comandos a partir da raiz:

```bash
uv sync --project backend --locked
```

O ambiente Python fica em `backend/.venv` e as dependências estão fixadas em `backend/uv.lock`. Não é necessário ativar o ambiente manualmente. O frontend usa Next.js 15 e React 19; instale suas dependências antes de iniciar. Configure `OPENAI_API_KEY` e `OPENAI_MODEL` no `.env` da raiz (não versionado), ou informe outro caminho com `--env-file` no comando CLI; sem credenciais válidas o fechamento padrão falha.

Se necessário, extrair o pacote de dados sem sobrescrever os arquivos existentes:

```bash
unzip -n Tarvo_Data_Pack_set2026.zip -d dados_tarvo
```

O anexo XLSX de remessas é lido em memória a partir do e-mail original. A cópia de XLSX previamente extraída não é usada pelo processamento.
## Fechamento em lote (um comando)

```bash
uv sync --project backend --locked
uv run --locked --project backend python -m src \
  --entrada dados_tarvo \
  --competencia 2026-09 \
  --saida saida
```

Esse comando lê a pasta do mês, executa **normalização e investigação contratual com OpenAI em toda execução** (não reutiliza cache nessa etapa), descobre os ajustes tardios (`06_orbe`) e políticas confirmadas (`07_politicas_confirmadas.json`), calcula os valores em `Decimal` e imprime um resumo executivo. Sem referência bancária revisada, usa apenas regras bancárias e mantém movimentos sem conta como pendências; a IA contratual não classifica os PIX bancários.

Referências aplicadas:

| Arquivo | Origem | Regra de aplicação |
|---|---|---|
| `saida/referencias/referencias.json` | Gerada automaticamente pelo comando de fechamento via OpenAI | Só vale se os hashes das fontes e o contexto de cada decisão baterem com os arquivos atuais. |
| Referência bancária revisada (opcional) | `python -m src.normalizar_bancos aprovar` após nova geração com LLM | Aplicada somente com `--referencias-bancarias` explícito; exige fontes atuais e aprovação humana válida. Sem ela, o fechamento bancário usa regras e registra pendências. |

O arquivo local `revisada-v1.json` teve hashes das fontes e da proposta **reescritos após a revisão**, sem nova aprovação. Não o use como evidência de revisão dos dados atuais. Gere propostas atuais e peça revisão; nunca atualize hashes de uma aprovação antiga para fazê-la passar.

`--referencias` e `--referencias-bancarias` aceitam destinos explícitos; a referência bancária só é aplicada quando aprovada, informada e válida para as fontes atuais.

O comando termina com código zero quando conclui, e com código 2 e mensagem em caso de entrada inválida. Os arquivos originais não são alterados. O carregador exige `06_orbe_ajustes_tardios_*.csv` além de `01_orbe_relatorio_comissoes_*.csv`, integrando estornos e reversões de antecipação e preservando arquivo e linha física de cada lançamento no rastro.

Saídas:

- `saida/comissoes_por_lojista.csv`
- `saida/divergencias_taxa.csv`
- `saida/notas_a_emitir.csv`
- `saida/lancamentos_classificados.csv`
- `saida/pendencias.csv`
- `saida/rastro.csv`

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

Gerar sugestões envia o contexto selecionado à OpenAI, incluindo movimento, plano de contas, correspondências candidatas e trechos documentais. Configure `OPENAI_API_KEY` e `OPENAI_MODEL` no `.env`; variáveis já exportadas prevalecem. Não publique referências bancárias brutas nem caches: contêm evidências financeiras. Em `saida/`, apenas os seis CSVs obrigatórios entram no Git.

```bash
uv run --locked --project backend python -m src.normalizar_bancos gerar \
  --entrada dados_tarvo --competencia 2026-09 --docs docs \
  --saida saida/referencias_bancarias_novas
```

Saídas: `referencias_bancarias.json`, `classificacoes_sugeridas.csv`, `revisao_humana.csv`, `rca_bancaria.json`, `escaladas_humanas.json` e `cache_llm/`. Esses arquivos não alteram o fechamento. A conta sugerida deve existir no plano, ser analítica, lançável e ter evidência; o schema estruturado restringe `evidence_ids`, `account_code` e `movement_id` aos identificadores reais do contexto, sem alterar valor, data ou identidade do movimento nem inventar NF/baixa. Erro da API interrompe a geração, sem fallback fabricado.

Prévia auditável desta execução: `saida/referencias_bancarias_novas/previa_classificacao_nao_aprovada.csv` reúne os 184 movimentos, preserva as 74 contas de regra e mostra as 110 propostas validadas da LLM em `conta_codigo_previa` com `status=IA_NAO_APROVADA` e evidências. Preenche em prévia as 109 contas antes vazias; **não é um dos seis CSVs oficiais nem substitui revisão**. `classificacoes_sugeridas.csv` guarda a justificativa integral por proposta.

`escaladas_humanas.json` e `revisao_humana.csv` consolidam o dossiê de revisão humana por movimento: dados originais do extrato, conta atual e conta proposta com descrições, separação entre decisão contábil e pendência operacional, nível de autoridade (`CRITERIO_CONFIRMADO_PELO_FINANCEIRO` vs `DECISAO_FUNDAMENTADA_DO_TIME_EM_HIPOTESES`), diagnóstico validado, hipóteses não confirmadas, evidências citadas resolvidas, ações específicas para o revisor e o comando exato de aprovação quando aplicável.

```bash
uv run --locked --project backend python -m src.normalizar_bancos aprovar \
  --entrada dados_tarvo --docs docs \
  --referencias saida/referencias_bancarias_novas/referencias_bancarias.json \
  --saida saida/referencias_bancarias/revisada-v2.json \
  --revisor 'NOME DO REVISOR' --todos-validos
```

A revisão não consulta a OpenAI. Registra o nome declarado do revisor, as propostas selecionadas e hashes das fontes/decisões; não é autenticação de identidade nem assinatura criptográfica. O arquivo revisado é separado e não sobrescreve uma versão existente.

```bash
uv run --locked --project backend python -m src \
  --entrada dados_tarvo --competencia 2026-09 --saida saida --docs docs
```

O fechamento rejeita sugestões ainda não revisadas, outra competência, alteração das fontes financeiras/documentais ou adulteração da proposta revisada. A reaplicação da referência bancária aprovada mantém IDs, valores e origens, aplica a conta com `metodo=IA` e registra revisor/evidências no rastro; a normalização contratual chama a LLM novamente. A confiança `0.80` não é probabilidade calibrada.

Aprovar uma conta não comprova conciliação com título/NF nem resolve automaticamente origem do PIX, motivo de devolução ou confirmação de crédito futuro. Essas pendências operacionais são preservadas. Sem proposta sustentada, não há conta a aprovar: mantenha o fechamento pelas regras existentes e encaminhe o diagnóstico, sem criar receita ou pagamento.

`--referencias` da Orbe e `--referencias-bancarias` podem ser usados juntos; quando informada, a referência bancária é verificada no pacote bruto antes da materialização contratual. A rota `POST /api/v1/fechamento/executar`, usada pelo frontend, não requer revisão bancária: gera um fechamento preliminar com regras bancárias e pendências, sempre com normalização contratual por LLM.

## Frontend (Dashboard Next.js em Modo Escuro)

Interface web executiva em Next.js 15 e React 19 com tema escuro nativo, baseada nos componentes do Vantara, consumindo os dados apurados e oferecendo os recursos de UX de auditoria e governança:

```bash
cd frontend
./node_modules/.bin/next start --port 3000
# ou para desenvolvimento com recarga automática:
./node_modules/.bin/next dev --port 3000
```

Acessar em `http://localhost:3000`.

O botão **Executar fechamento com LLM** chama `POST /api/run` no Next.js, que chama `POST http://127.0.0.1:8000/api/v1/fechamento/executar?competencia=2026-09`. O backend executa `normalize_reference` com `OpenAIInterpreter` (normalização e RCA) e políticas confirmadas, valida a referência contratual, fecha o mês com classificação bancária por regras e grava os seis CSVs. O botão usa `reuse_cache=False`: consulta o modelo novamente mesmo quando há resposta em cache. Sem chave/modelo ou normalização concluída, a rota devolve erro sem fechamento parcial. Sem referência bancária revisada, lançamentos não classificados continuam pendentes; não são apresentados como aprovados. A interface só confirma as etapas após a resposta, sem simular progresso durante a chamada.

### Recursos da Interface

1. **Visão Geral & KPIs:** faturamento consolidado de comissões (checkout e remessa), total de notas fiscais, volume de divergências e contador de lançamentos/pendências com indicador de 100% de batimento no rastro.
2. **Comissões & Notas:** tabelas detalhadas com busca dinâmica por lojista/CNPJ, badges de proveniência (`Contrato Documentado` vs `Vínculo Inferido via RCA`) e botão de atalho para o rastro.
3. **Linhagem & Rastro (Trace UX):** explicador de rastro em linguagem natural gerado pela IA para cada saída selecionada, árvore visual de proveniência conectando o arquivo/linha de origem à saída financeira, e badges de governança.
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

Os seis CSVs de `saida/` foram gerados pelo comando padrão com 27 respostas novas do `gpt-6-luna` para contratos/RCA, sem cache. **São preliminares**: a classificação bancária foi apenas por regras, e 109 lançamentos ficaram sem conta. Nesta segunda execução com as mesmas fontes, a IA não confirmou o vínculo contratual de 86 transações antes aceitas para o EC 1038867654; a comissão total caiu R$ 10.886,22. Essas linhas permanecem pendentes, não foram preenchidas com uma taxa inventada. Revisão comercial é necessária antes de tratar o resultado como fechamento aprovado.

| Métrica | Execução com IA contratual, sem revisão bancária |
|---|---:|
| Ajustes tardios Orbe (`06_orbe`) | 14 linhas integradas |
| Comissão de checkout apurada | R$ 192.849,12 |
| Comissão de remessas apurada | R$ 39.543,45 |
| Total de comissão apurada | R$ 232.392,57 (34 registros) |
| Notas fiscais propostas (soma R$) | 20 (R$ 232.398,07) |
| Divergências de taxa | 238 (210 checkout + 28 remessa) |
| Movimentos bancários classificados | 184 (184 regra / 109 sem conta) |
| Pendências totais | 560 |
| Contribuições no rastro (`rastro.csv`) | 5.085 |

> O comando padrão exige `OPENAI_API_KEY`, `OPENAI_MODEL` e rede, mas não exige revisão bancária. Faz chamadas novas à OpenAI para contratos a cada execução; o cache dos comandos separados de normalização não substitui essas chamadas. Uma referência bancária só é aplicada quando passada explicitamente com `--referencias-bancarias` e validada contra as fontes atuais. As fórmulas são determinísticas, mas vínculos e decisões revisadas podem alterar os números.

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

Cobre arredondamento, vigência, isenção e estorno histórico, antecipação/reversão, Atlas bloqueada, deduplicação/conflito, remessas negativas, compliance, notas/rastros, contas de contrapartida e HTTP. Também verifica sugestões bancárias sem efeito antes da revisão, contas/evidências inválidas, adulteração, fontes obsoletas, competência e aplicação revisada sem mudar dinheiro nem apagar pendências operacionais. Usa biblioteca padrão e TestClient com `httpx2`.

Foram exercitados duas vezes o fechamento contratual com 27 respostas novas da OpenAI em cada execução, a leitura HTTP dos CSVs preliminares e o painel web. No lote mais recente, o rastro contém 5.085 contribuições e coincide com os valores de todos os IDs monetários exportados.

## Fontes e limites

- Regras confirmadas, hipóteses e fontes: [HIPOTESES.md](HIPOTESES.md).
- Histórico de sessões, com pedidos e respostas originais: [PROMPTS/HISTORICO_LITERAL.md](PROMPTS/HISTORICO_LITERAL.md).
- Versões reescritas dos pedidos, não transcrição: [PROMPTS/HISTORICO.md](PROMPTS/HISTORICO.md).
- Momentos relevantes de uso da IA: [PROMPTS/MOMENTOS.md](PROMPTS/MOMENTOS.md).

`docs/` e os arquivos financeiros originais em `dados_tarvo/` permanecem locais e não são enviados ao repositório. O README, `HIPOTESES.md`, o histórico de prompts e os seis CSVs são os entregáveis públicos; nenhuma credencial, cache ou referência bancária sem revisão é publicada.

Os leitores reconhecem os papéis e cabeçalhos atuais de Orbe, contratos, e-mail Ponte, plano de contas, Aurora CSV e Meridiano OFX 1.02/SGML. Não prometem aceitar layouts diferentes. Uma pasta deve ter exatamente um arquivo por papel; múltiplos candidatos geram erro explícito. CNPJs fictícios são normalizados sem validar dígitos verificadores.

A geração assistida usa OpenAI com respostas estruturadas. No fluxo bancário, propostas e RCA precisam de revisão explícita antes de afetar a classificação. O fechamento padrão sempre faz chamadas novas à LLM contratual e falha sem credencial ou rede; sem referência bancária aprovada, usa regras bancárias e mantém pendências. Comandos separados de normalização podem reutilizar seu próprio cache. Nenhuma ação financeira externa é autorizada.
