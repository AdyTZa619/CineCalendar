from __future__ import annotations

import os

# implicit/NumPy warn that a multi-threaded BLAS underneath ALS can cause severe
# oversubscription for this small local fold-in workload. Set the process limits before any
# recommender module imports NumPy/OpenBLAS. PyInstaller launches through this package too.
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

__version__ = "4.9.2"
