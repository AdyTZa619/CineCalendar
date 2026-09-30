import ctypes
import os
import sys

os.environ["CINECALENDAR_V5_ALPHA"] = "1"


def _runtime_self_check() -> int:
    import numpy as np
    from numpy.linalg import norm
    from scipy.sparse import csr_matrix
    import implicit

    value = float(norm(np.asarray([3.0, 4.0], dtype=np.float64)))
    matrix = csr_matrix(np.asarray([[1.0, 0.0]], dtype=np.float32))
    if abs(value - 5.0) > 1e-9 or matrix.shape != (1, 2) or not getattr(implicit, "__version__", None):
        raise RuntimeError("Runtime numeric self-check failed.")
    return 0


def _show_runtime_import_error(exc: BaseException) -> None:
    message = (
        "CineCalendar V5 Alpha nu poate porni deoarece runtime-ul numeric din folderul portable "
        "este incomplet sau amestecat cu altă versiune.\n\n"
        f"Eroare: {exc}\n\n"
        "Șterge numai folderul programului V5 Alpha, păstrează CineCalendarV5AlphaData, "
        "apoi extrage ZIP-ul complet într-un folder NOU și gol. Nu copia EXE-ul peste un "
        "folder _internal existent."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "CineCalendar V5 Alpha — runtime invalid", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    if "--runtime-self-check" in sys.argv:
        raise SystemExit(_runtime_self_check())
    try:
        from cinecalendar.app import main
    except (ImportError, ModuleNotFoundError) as exc:
        _show_runtime_import_error(exc)
        raise SystemExit(1)
    raise SystemExit(main())
