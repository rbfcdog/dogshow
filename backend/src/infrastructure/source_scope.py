from pathlib import Path

from src.domain.models import DataPackError

DATA_ROOT = Path(__file__).resolve().parents[3] / "dados_tarvo"
SOURCE_SCOPE = "dados_tarvo_only_v2"


def require_data_dir(path: Path) -> Path:
    if path.is_symlink() or path.is_junction() or DATA_ROOT.is_symlink() or DATA_ROOT.is_junction() or path.resolve() != DATA_ROOT.absolute():
        raise DataPackError("Fonte de dados não permitida: use apenas dados_tarvo/ deste projeto.")
    if not path.is_dir():
        raise DataPackError("Diretório dados_tarvo/ inexistente.")
    return path.resolve()


def require_source_file(path: Path) -> Path:
    root = require_data_dir(DATA_ROOT)
    if path.is_symlink() or path.is_junction() or path.resolve().parent != root or not path.is_file():
        raise DataPackError("Arquivo fora do pacote permitido ou vínculo externo não permitido.")
    return path
