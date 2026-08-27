#!/usr/bin/env bash
# Generate -O0 -g assembler (and preprocessed source) for FortranCallGraph
# analysis of N4_UMN_PES_Class. Reuses .mod files from the CoarseAIR build.
set -euo pipefail

REPO=/home/adolgert/dev/skateboard
CA=$REPO/codes/CoarseAIR
OUT=$(cd "$(dirname "$0")" && pwd)

# Locate the build's module directory via a module this file USEs.
MODDIR=$(dirname "$(find "$CA/build" -name 'parameters_module.mod' | head -1)")
if [ -z "$MODDIR" ]; then
    echo "error: parameters_module.mod not found under $CA/build — build CoarseAIR first" >&2
    exit 1
fi

# Production kind/preprocessor flags, but -O0 -g as FCG requires.
FLAGS="-cpp -fdefault-double-8 -fdefault-real-8 -ffree-line-length-none -frealloc-lhs -O0 -g"

mkdir -p "$OUT/asm" "$OUT/pre" "$OUT/modtmp"

# -J to scratch so the production .mod files are not clobbered.
# Assembler file name must equal the module name for FCG to find it.
gfortran $FLAGS -S -I "$MODDIR" -J "$OUT/modtmp" \
    -o "$OUT/asm/n4_umn_pes_class.s" "$CA/src/PESs/N4_UMN_PES_Class.F90"

# Preprocessed source: fallback for FCG's source parser (SOURCE_FILES_PREPROCESSED).
gfortran $FLAGS -E "$CA/src/PESs/N4_UMN_PES_Class.F90" > "$OUT/pre/n4_umn_pes_class.f90"

echo "wrote $OUT/asm/n4_umn_pes_class.s ($(wc -l < "$OUT/asm/n4_umn_pes_class.s") lines)"
echo "wrote $OUT/pre/n4_umn_pes_class.f90"
echo "MODDIR=$MODDIR"
