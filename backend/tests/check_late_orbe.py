from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil

from src.application.checkout import close_checkout
from src.application.closing import close_month, validate_closing
from src.domain.models import DataPackError
from src.infrastructure.readers import _select_files, _checkout, load_dataset


def check() -> None:
    data_dir = Path(__file__).resolve().parents[2] / "dados_tarvo"
    period = date(2026, 9, 1)

    files = _select_files(data_dir)
    assert "adjustments" in files
    assert files["adjustments"].name == "06_orbe_ajustes_tardios_set2026.csv"

    dataset = load_dataset(data_dir)
    assert len(dataset.checkout) == 1943 + 14

    late_entries = [entry for entry in dataset.checkout if entry.source.file == "06_orbe_ajustes_tardios_set2026.csv"]
    assert len(late_entries) == 14
    assert [entry.source.row for entry in late_entries] == list(range(2, 16))

    kinds = {entry.kind for entry in late_entries}
    assert kinds == {"ESTORNO", "REVERSAO ANTECIPACAO", "VENDA"}

    part = close_checkout(dataset, period)
    assert sum(c.comissao_brl for c in part.commissions) == Decimal("203735.34")
    assert len(part.divergences) == 210

    late_traces = [t for t in part.traces if t.arquivo_origem == "06_orbe_ajustes_tardios_set2026.csv"]
    assert len(late_traces) == 28
    for entry in late_entries:
        entry_traces = [t for t in late_traces if t.ref_origem == entry.source.ref]
        assert len(entry_traces) == 2
        assert {t.linha_origem for t in entry_traces} == {entry.source.row}
        assert {t.id_saida.split(":")[0] for t in entry_traces} == {"comissao-checkout", "nota-checkout"}
    closing = close_month(dataset, period)
    validate_closing(closing, dataset)
    assert sum(c.comissao_brl for c in closing.comissoes_por_lojista) == Decimal("243278.79")
    assert sum(c.comissao_brl for c in closing.comissoes_por_lojista if c.linha_negocio == "CHECKOUT") == Decimal("203735.34")
    assert sum(c.comissao_brl for c in closing.comissoes_por_lojista if c.linha_negocio == "REMESSA") == Decimal("39543.45")
    assert len(closing.notas_a_emitir) == 21
    assert len(closing.pendencias) == 301
    assert len(closing.rastro) == 5224

    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for role, path in files.items():
            if role != "adjustments":
                shutil.copy(path, tmp_path / path.name)
        without_adjustments = _select_files(tmp_path)
        assert "adjustments" not in without_adjustments
        ds_without = load_dataset(tmp_path)
        assert len(ds_without.checkout) == 1943

        shutil.copy(files["adjustments"], tmp_path / "06_orbe_ajustes_tardios_set2026_copy.csv")
        shutil.copy(files["adjustments"], tmp_path / "06_orbe_ajustes_tardios_set2026_another.csv")
        try:
            _select_files(tmp_path)
        except DataPackError as err:
            assert "esperado no máximo um arquivo" in str(err)
        else:
            raise AssertionError("Múltiplos arquivos de ajustes aceitos sem erro.")

        bad_csv = tmp_path / "06_orbe_ajustes_tardios_malformed.csv"
        bad_csv.write_text("coluna1;coluna2\nvalor1;valor2\n", encoding="utf-8")
        try:
            _checkout(bad_csv)
        except DataPackError as err:
            assert "Cabeçalho não corresponde" in str(err)
        else:
            raise AssertionError("Arquivo de ajustes com cabeçalho incorreto aceito sem erro.")


if __name__ == "__main__":
    check()
