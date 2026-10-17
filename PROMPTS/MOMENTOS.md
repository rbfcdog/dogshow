# Momentos relevantes de uso de IA

Os momentos abaixo descrevem pedidos e resultados dos [registros literais](HISTORICO_LITERAL.md). Os 28 prompts em [HISTORICO.md](HISTORICO.md) são reescritas desses pedidos, não execuções adicionais.

## 1. Conferir a fonte antes de confiar na IA

Pedimos a análise dos arquivos (temas dos prompts reescritos 1 e 4); a IA apontou colunas desalinhadas, mas a conferência das coordenadas do XLSX mostrou que o leitor textual havia omitido células vazias. Corrigimos o diagnóstico e não tratamos um defeito da extração como defeito dos dados.

Registros literais: 014–016 (`216521a3`, `6b479da5`, `8e399976`).

## 2. Delimitar a autonomia da IA

Questionamos se o case precisava de LLM e corrigimos a recomendação inicial de não usá-la no produto (temas dos prompts reescritos 5–6 e 16); a IA passou a propor investigação de exceções sem inventar contratos ou valores. A decisão adotada foi manter cálculos determinísticos e distinguir ausência de intervenção durante o lote de autorização para alterar regras financeiras.

Registros literais: 031–036 (`05887062`, `c396c40b`, `b9513493`).

## 3. Trocar hipótese por resposta do financeiro

Fornecemos as threads e pedimos a aplicação das regras confirmadas (temas dos prompts reescritos 8–9); a IA havia antes recomendado critérios ainda não confirmados. Substituímos as hipóteses por arredondamento por operação, isenção da Casa e bloqueio da Atlas, e conferimos a geração dos seis CSVs com rastro.

Registros literais: 043, 046 e 051 (`a75b8a03`, `50be6a64`, `a0f00c0c`).

## 4. Testar a IA bancária além da taxa de acerto

Pedimos avaliação cega de movimentos bancários e correção das falhas (temas dos prompts reescritos 19–20); em 20 casos, a IA acertou 18, mas citou uma linha inválida, não conectou uma TED às remessas do dia e atribuiu ao financeiro uma conta que era premissa do time. Restringimos IDs de evidência e códigos ao conjunto válido, fornecemos candidatos cambiais do dia e separamos autoridade confirmada de hipótese no dossiê de revisão.

Registros literais: 088–089; detalhes do teste e das correções ficam nas notas locais não versionadas.
