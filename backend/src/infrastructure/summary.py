from collections import Counter
from decimal import Decimal

from src.domain.closing import ClosingResult
from src.domain.policies import ZERO


def _brl(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.01")), ",.2f")
    return "R$ " + text.translate(str.maketrans({",": ".", ".": ","}))


def render_summary(result: ClosingResult, applied: tuple[str, ...], warnings: tuple[str, ...], output_dir: str,
    late_adjustments: int = 0) -> str:
    by_line = {
        line: sum((row.comissao_brl for row in result.comissoes_por_lojista if row.linha_negocio == line), ZERO)
        for line in ("CHECKOUT", "REMESSA")
    }
    divergences = Counter(row.linha_negocio for row in result.divergencias_taxa)
    pending = Counter(row.tipo for row in result.pendencias)
    methods = Counter(row.metodo for row in result.lancamentos_classificados)
    unclassified = sum(1 for row in result.lancamentos_classificados if not row.conta_codigo)
    deciders = Counter(row.quem_decide for row in result.pendencias)
    lines = [
        f"FECHAMENTO TARVO {result.competencia} — {result.status}",
        *( (f"Ajustes tardios 06_orbe: {late_adjustments} linhas integradas ao checkout",) if late_adjustments > 0 else () ),
        "",
        "Referências aplicadas: " + ("; ".join(applied) if applied else "nenhuma (somente dados originais)"),
        *(f"ATENÇÃO: {warning}" for warning in warnings),
        "",
        "Comissões por linha de negócio",
        f"  Checkout  {_brl(by_line['CHECKOUT']):>18}",
        f"  Remessa   {_brl(by_line['REMESSA']):>18}",
        f"  Total     {_brl(sum(by_line.values(), ZERO)):>18}   ({len(result.comissoes_por_lojista)} registros)",
        "",
        f"Notas a emitir: {len(result.notas_a_emitir)}   "
        f"{_brl(sum((row.valor_brl for row in result.notas_a_emitir), ZERO))}",
        f"Divergências de taxa: {len(result.divergencias_taxa)}   "
        f"(checkout {divergences['CHECKOUT']}, remessa {divergences['REMESSA']})   "
        f"{_brl(sum((row.diferenca_brl for row in result.divergencias_taxa), ZERO))}",
        f"Lançamentos classificados: {len(result.lancamentos_classificados)}   "
        f"(regra {methods['REGRA']}, IA aprovada {methods['IA']}, sem conta {unclassified})",
        f"Pendências: {len(result.pendencias)}   (rastro: {len(result.rastro)} contribuições)",
    ]
    for kind, count in pending.most_common():
        lines.append(f"  {count:>4}  {kind}")
    lines.extend(["", "Quem decide as pendências"])
    for decider, count in deciders.most_common():
        lines.append(f"  {count:>4}  {decider}")
    lines.extend(["", f"Arquivos gerados em {output_dir}/: comissoes_por_lojista.csv, divergencias_taxa.csv, "
        "notas_a_emitir.csv, lancamentos_classificados.csv, pendencias.csv, rastro.csv"])
    return "\n".join(lines) + "\n"
