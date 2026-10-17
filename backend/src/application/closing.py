from collections import defaultdict
from datetime import date
from decimal import Decimal
import re

from src.application.banking import classify_banks
from src.application.checkout import close_checkout
from src.application.remittances import close_remittances
from src.domain.banking_reference import BankProposal, ReviewedBankDecision
from src.domain.closing import ClosingResult, Pending
from src.domain.models import Dataset
from src.domain.policies import ZERO, period_key, in_period, output_id
from src.infrastructure.slack_inbox import SlackInbox

TECHNICAL_ASSUMPTIONS = (
    "Somente dados_tarvo é fonte de fatos. Resultados são preliminares até validar políticas não documentadas no pacote.",
    "Convenção técnica: Decimal e HALF_UP por linha, sinais preservados; não apresentada como confirmação do financeiro.",
    "Checkout: projeção bruto × taxa da planilha; mês do movimento e data original para reversões são premissas de apuração.",
    "Remessas: data de contratação para agrupamento; repetição econômica idêntica conta uma vez como premissa técnica sinalizada.",
    "Notas são propostas agregadas; regras fiscais e tratamento de ajustes precisam de evidência adicional no pacote.",
)


def validate_closing(result: ClosingResult, dataset: Dataset) -> None:
    expected: dict[str, Decimal | None] = {}
    collections = (
        (result.comissoes_por_lojista, "comissao_brl"),
        (result.divergencias_taxa, "diferenca_brl"),
        (result.notas_a_emitir, "valor_brl"),
        (result.lancamentos_classificados, "valor"),
        (result.pendencias, "valor_brl"),
    )
    for rows, amount_field in collections:
        for row in rows:
            if not row.id_saida or row.id_saida in expected:
                raise RuntimeError("Identificador de saída vazio ou repetido.")
            amount = getattr(row, amount_field)
            if amount is not None and (not amount.is_finite() or amount != amount.quantize(Decimal("0.01"))):
                raise RuntimeError("Saída monetária inválida ou sem precisão de centavos.")
            expected[row.id_saida] = amount
    known_sources = {
        (entry.source.file, entry.source.ref, entry.source.row, entry.source.sheet)
        for entries in (dataset.checkout, dataset.contracts, dataset.remittances, dataset.banks, dataset.accounts)
        for entry in entries
    }
    actual: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for contribution in result.rastro:
        if contribution.id_saida not in expected or expected[contribution.id_saida] is None:
            raise RuntimeError("Rastro aponta para saída inexistente ou sem valor apurado.")
        source = (contribution.arquivo_origem, contribution.ref_origem, contribution.linha_origem, contribution.aba_origem)
        if source not in known_sources:
            raise RuntimeError("Rastro aponta para origem não encontrada no pacote.")
        if not contribution.valor.is_finite():
            raise RuntimeError("Rastro contém valor não finito.")
        actual[contribution.id_saida] += contribution.valor
    for identifier, amount in expected.items():
        if amount is not None and (identifier not in actual or actual[identifier] != amount):
            raise RuntimeError(f"Rastro não fecha para {identifier}.")
    for row in (*result.comissoes_por_lojista, *result.divergencias_taxa):
        if not re.fullmatch(r"[0-9]{14}", row.cnpj):
            raise RuntimeError("CNPJ inválido em saída financeira.")
    for row in result.notas_a_emitir:
        if not re.fullmatch(r"[0-9]{14}", row.tomador_cnpj) or row.valor_brl <= ZERO:
            raise RuntimeError("Nota com tomador inválido ou valor não positivo.")
    posting_accounts = {
        account.code for account in dataset.accounts
        if account.posting_allowed and account.kind == "ANALITICA"
    }
    for row in result.lancamentos_classificados:
        if row.conta_codigo and row.conta_codigo not in posting_accounts:
            raise RuntimeError("Conta de lançamento inexistente ou não analítica.")
        if not ZERO <= row.confianca <= Decimal("1"):
            raise RuntimeError("Confiança de classificação fora do intervalo permitido.")


def close_month(dataset: Dataset, period: date, *,
    bank_decisions: dict[str, ReviewedBankDecision] | None = None,
    bank_proposals: dict[str, BankProposal] | None = None,
    policies: SlackInbox | None = None) -> ClosingResult:
    if period.day != 1:
        raise ValueError("A competência deve ser o primeiro dia do mês.")
    parts = (
        close_checkout(dataset, period),
        close_remittances(dataset, period, policies=policies),
        classify_banks(dataset, period, reviewed_decisions=bank_decisions, proposals=bank_proposals),
    )
    policy_pending = []
    if not policies:
        for name, entries, date_field in (("CHECKOUT", dataset.checkout, "occurred_on"),
            ("REMESSA", dataset.remittances, "contracted_on")):
            first = next((e for e in entries if in_period(getattr(e, date_field), period)), None)
            if first:
                policy_pending.append(Pending(output_id("politica-pacote", period, name), "VALIDAR_POLITICA_APURACAO",
                    first.source.ref, None,
                    f"{name}: o pacote não confirma integralmente corte, arredondamento, ajustes e política fiscal. "
                    "Resultados são projeções sob premissas técnicas declaradas, não fechamento aprovado.", "Financeiro"))
    pending = tuple(row for part in parts for row in part.pending) + tuple(policy_pending)
    result = ClosingResult(
        competencia=period_key(period),
        status="PRELIMINAR_COM_PENDENCIAS" if pending else "CONCLUIDO",
        comissoes_por_lojista=tuple(row for part in parts for row in part.commissions),
        divergencias_taxa=tuple(row for part in parts for row in part.divergences),
        notas_a_emitir=tuple(row for part in parts for row in part.invoices),
        lancamentos_classificados=tuple(row for part in parts for row in part.movements),
        pendencias=pending,
        rastro=tuple(row for part in parts for row in part.traces),
        premissas_tecnicas=TECHNICAL_ASSUMPTIONS,
    )
    validate_closing(result, dataset)
    return result
