"""Portable analysis kernels, including Docker on Apple Silicon."""

import os

# LLVM host-feature detection can generate unsupported ARM instructions in Docker.
# A caller can explicitly override this before starting Python.
os.environ.setdefault("NUMBA_CPU_NAME", "generic")
