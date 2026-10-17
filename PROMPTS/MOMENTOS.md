## 1. Corrigir o diagnóstico antes de programar

Ao analisar o pacote, a IA afirmou que o XLSX tinha colunas desalinhadas. Conferimos as coordenadas e descobrimos que o leitor textual havia omitido células vazias; não tratamos isso como erro financeiro. Na primeira entrega do backend, a leitura de 1.943 lançamentos de checkout, 233 linhas cambiais e 184 movimentos bancários serviu como **diagnóstico**, não como comissão ou faturamento aprovados.

Registros: 014–016 e 021–025 (`216521a3`, `6b479da5`, `8e399976`, `b6931516`, `f297c95e`, `70e1a727`).

## 2. Delimitar onde a IA pode decidir

Questionamos a sugestão inicial de usar LLM apenas no desenvolvimento. Redefinimos seu papel para investigar exceções e estruturar referências; valores e regras de fechamento permaneceram determinísticos, e ausência de contrato não virou aprovação implícita. Essa separação orientou as perguntas ao financeiro antes de usar hipóteses como regras.

Registros: 031–036 (`26754388`, `05887062`, `b9513493`, `a19fa54c`).

## 3. Trocar premissas por regras verificáveis

Após receber as threads do financeiro, distinguimos decisões confirmadas de propostas: incorporamos arredondamento por operação, isenção da Casa e bloqueio da Atlas, com seis CSVs rastreáveis. Para o estorno de venda de agosto durante a isenção de setembro, formulamos uma pergunta específica sobre o impacto de −R$ 5,50 na nota, em vez de presumir resposta. Também preservamos as duas vigências válidas da Bridge: a taxa depende da data da venda, não da remoção de uma linha considerada duplicada pela IA.

Registros: 043, 045–046, 051–057 (`a75b8a03`, `08ce5960`, `50be6a64`, `a0f00c0c`, `9e2379a1`, `7285ae45`, `8920db8d`, `6045b399`, `bd879c2b`, `44f62014`); pergunta em `docs/PERGUNTA_FINANCEIRO_CASA_ESTUDANTE.md`.

## 4. Auditar propostas e reprocessar fontes novas

O feedback de Manuel exigiu referências fundamentadas antes da apuração, sem escolha de taxa pela IA a cada número. Na avaliação cega bancária, 18 de 20 classificações estavam certas, mas havia citação de linha inválida, TED sem vínculo às remessas do dia e premissa do time atribuída ao financeiro. Restringimos IDs e contas, incluímos candidatos cambiais e distinguimos as duas autoridades no dossiê humano. Com o aviso das 14 linhas tardias da Orbe, preservamos a origem na ingestão e consultamos o EML da Ponte na normalização e RCA; a execução documentada registrou 28 contribuições adicionais no rastro.

Registros: 076–078, 088–089 e 091 (`1d81721e`, `99ef0eab`, `3ad6cdc1`, `d63ffb22`); detalhes em `docs/PLANO_SOLUCAO_TARVO.md` e `docs/NOTAS_SOLUCAO.md`, seções 12–13.
