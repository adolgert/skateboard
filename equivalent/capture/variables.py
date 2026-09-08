"""What one captured value is: a name, a type, and a rank.

Trust role: this is the vocabulary a capture is written and read in. A
manifest declares a region's inputs and outputs as these, the harness
writes an .npy file per variable, and the comparator reads one back and
checks it is the type and shape that was declared. All three have to
mean the same thing by "f64, rank 2", so the type list and the rank
ceiling are spelled once, here, below every reader of them.

The list is short on purpose. A type the capture format cannot carry, or
a rank the generated Fortran reader has no case for, would reach the
harness as something no reader knows the width of.
"""
from __future__ import annotations

from dataclasses import dataclass

# The types the capture format and the comparator can carry, spelled the
# way the manifest writes them.
DTYPES = ("f32", "f64", "i32", "i64", "l")
MAX_RANK = 4


@dataclass(frozen=True)
class Variable:
    name: str
    dtype: str  # one of DTYPES
    rank: int  # 0..MAX_RANK
