#!/usr/bin/env python3
"""Generate a Serialbox capture-replay harness for a region spec.

Reads a region.yaml (notes/regions/*.yaml) and emits, deterministically:

  <entry>_capture_mod.f90   module that serializes live_in at region entry and
                            live_out + clobbers at region exit (m_ser_ftg).
  ftg_<entry>_test.f90      standalone replay driver: read a captured case, call
                            the region, compare with m_ser_ftg_cmp at tolerance 0.
  build.sh / apply.sh       build the replay binaries / instrument the real tree.

It also patches the anchor file in place, inserting four marker-delimited blocks
(visibility / use / capture entry / capture exit). The patch is idempotent and
reversible.

Everything that names a variable, an extent or a type comes from the yaml; the
only target-specific knowledge in this file is the shape of a Fortran source
file. `defined_when` on a live_out entry is emitted verbatim as a Fortran
logical expression guarding that field's comparison.

Usage:
  gen_harness.py <region.yaml> [--out-dir D] [--replay-dir D]   # generate
  gen_harness.py <region.yaml> --patch [--dry-run] [--target F]
  gen_harness.py <region.yaml> --restore [--dry-run] [--target F]
  gen_harness.py <region.yaml> --check [--target F]
Exit 0 on success.
"""
import argparse
import difflib
import re
import sys
from pathlib import Path

import yaml

REPO_DEFAULT = Path(__file__).resolve().parents[2]
DATA_DIR_DEFAULT = "/home/adolgert/dev/skateboard/ftgdata"
SB_DEFAULT = "/home/adolgert/dev/skateboard/tools/serialbox/install"

STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
DECL_KW = re.compile(
    r"^\s*(use|implicit|integer|real|double|complex|logical|character|type|class|dimension|"
    r"parameter|save|data|common|external|intrinsic|namelist|equivalence|allocatable|pointer|"
    r"target|optional|procedure|import|interface|end\s*interface|#)\b",
    re.IGNORECASE,
)


def code_part(line):
    """Strip string literals, then the trailing ! comment."""
    return STRING.sub("''", line).split("!", 1)[0]


def read_src(path):
    """Read preserving line terminators and any non-UTF-8 bytes.

    The CoarseAIR sources are CRLF; Path.read_text() would silently rewrite the
    whole file to LF on the way back out.
    """
    with open(str(path), newline="", encoding="utf-8", errors="surrogateescape") as fh:
        return fh.read()


def write_src(path, text):
    with open(str(path), "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# spec -> field model
# ---------------------------------------------------------------------------


class Field:
    """One serialized quantity: a name, a rank/bounds list and a Fortran type."""

    def __init__(self, name, extent, decl_type=None, defined_when=None):
        self.name = name
        self.dims = parse_extent(extent)
        self.is_int = bool(decl_type) and decl_type.strip().lower().startswith("integer")
        self.defined_when = defined_when

    @property
    def ftype(self):
        return "INTEGER" if self.is_int else "REAL(KIND=wp)"

    def dimension_attr(self):
        if not self.dims:
            return ""
        parts = [str(hi) if lo == 1 else "%d:%d" % (lo, hi) for lo, hi in self.dims]
        return ", DIMENSION(%s)" % ", ".join(parts)

    def declare(self, intent=None):
        attrs = self.dimension_attr()
        if intent:
            attrs += ", INTENT(%s)" % intent
        return "%s%s :: %s" % (self.ftype, attrs, self.name)

    def write_call(self):
        if not self.dims:
            return "CALL ftg_write('%s', %s)" % (self.name, self.name)
        return "CALL ftg_write('%s', %s, LBOUND(%s), UBOUND(%s))" % (
            self.name, self.name, self.name, self.name)

    def read_call(self):
        return "CALL ftg_read('%s', %s)" % (self.name, self.name)

    def compare_call(self, counter):
        if not self.dims:
            return "CALL ftg_compare('%s', %s, ok, %s)" % (self.name, self.name, counter)
        return "CALL ftg_compare('%s', %s, ok, %s, LBOUND(%s), UBOUND(%s))" % (
            self.name, self.name, counter, self.name, self.name)


def parse_extent(extent):
    """'scalar'/None -> []; '1:6' -> [(1,6)]; '6,0:111' -> [(1,6),(0,111)]."""
    if extent is None or str(extent).strip().lower() == "scalar":
        return []
    dims = []
    for part in str(extent).split(","):
        part = part.strip()
        if ":" in part:
            lo, hi = part.split(":")
            dims.append((int(lo), int(hi)))
        else:
            dims.append((1, int(part)))
    return dims


class Region:
    """The parts of the yaml the generator needs, resolved once."""

    def __init__(self, spec, repo_root, spec_path):
        self.spec = spec
        self.spec_path = spec_path
        self.name = spec["region"]
        anchor = spec["anchor"]
        self.entry = anchor["entry_symbol"]
        self.src = Path(repo_root) / anchor["file"]
        m = re.search(r"@(\d+)-(\d+)", anchor["pst_node"])
        if not m:
            die("anchor.pst_node has no @lo-hi line range: %r" % anchor["pst_node"])
        self.lo, self.hi = int(m.group(1)), int(m.group(2))

        self.inputs = [
            Field(f["name"], f.get("extent"), f.get("type"))
            for f in spec.get("live_in", [])
            if f.get("src") == "argument"
        ]
        self.outputs = [
            Field(f["name"], f.get("extent"), f.get("type"), f.get("defined_when"))
            for f in spec.get("live_out", [])
        ]
        self.clobbers = [
            Field(f["name"], f.get("extent"), f.get("type"), f.get("defined_when"))
            for f in spec.get("clobbers", {}).get("arrays", [])
        ]

    @property
    def exit_fields(self):
        return self.outputs + self.clobbers

    @property
    def module(self):
        """Name of the Fortran module containing the anchor."""
        for line in read_src(self.src).splitlines():
            m = re.match(r"\s*module\s+([A-Za-z]\w*)\s*$", code_part(line), re.IGNORECASE)
            if m and m.group(1).lower() != "procedure":
                return m.group(1)
        die("no MODULE statement found in %s" % self.src)

    @property
    def call_args(self):
        """Dummy-argument names of the entry, in declaration order, from the source.

        Guessing the order from the yaml would be wrong: live_in and live_out are
        interleaved in the real signature.
        """
        lines = read_src(self.src).splitlines()
        idx = find_unique(lines, r"\s*subroutine\s+%s\s*\(" % self.entry, self.lo, self.hi,
                          "SUBROUTINE %s statement" % self.entry)
        stmt, i = code_part(lines[idx]).rstrip(), idx
        while stmt.endswith("&"):
            i += 1
            stmt = stmt[:-1] + code_part(lines[i]).strip().rstrip()
        args = [a.strip() for a in stmt[stmt.index("(") + 1:stmt.rindex(")")].split(",")]
        spec_args = {f.name for f in self.inputs + self.outputs}
        if {a.lower() for a in args} != {a.lower() for a in spec_args}:
            die("spec live_in+live_out %s does not match the %s signature %s"
                % (sorted(spec_args), self.entry, args))
        return args

    @property
    def cmake_root(self):
        """Nearest ancestor of the anchor file holding a CMakeLists.txt."""
        for d in self.src.parents:
            if (d / "CMakeLists.txt").is_file() and (d / "src").is_dir():
                return d
        die("no CMake project root found above %s" % self.src)

    @property
    def capture_mod(self):
        return "%s_capture_mod" % self.entry

    @property
    def test_prog(self):
        return "ftg_%s_test" % self.entry

    @property
    def case_stem(self):
        """Directory name under FTG_DATA_DIR holding the captured cases."""
        return self.test_prog


def die(msg):
    sys.stderr.write("gen_harness: error: %s\n" % msg)
    sys.exit(2)


# ---------------------------------------------------------------------------
# generated Fortran: capture module
# ---------------------------------------------------------------------------


def banner(region, what):
    return [
        "! %s" % what,
        "! GENERATED by tools/regionharness/gen_harness.py -- DO NOT EDIT.",
        "! Region: %s   entry symbol: %s" % (region.name, region.entry),
        "! Spec:   %s" % region.spec_path,
        "!",
    ]


def gen_capture_mod(region, data_dir):
    e, L = region.entry, []
    L += banner(region, "Capture module for region entry/exit serialization.")
    L += [
        "! Standalone: USEs only m_ser_ftg and receives every field as an argument,",
        "! so it can be compiled before the instrumented module (no circular USE).",
        "!",
        "! Environment:",
        "!   FTG_CAPTURE_N     number of consecutive calls to capture (default 0 = off)",
        "!   FTG_CAPTURE_ROUND 1-based index of the first call to capture (default 1)",
        "!   FTG_DATA_DIR      root for captured cases (default %s)" % data_dir,
        "MODULE %s" % region.capture_mod,
        "",
        "  USE m_ser_ftg",
        "  IMPLICIT NONE",
        "  PRIVATE",
        "  PUBLIC :: %s_capture_entry, %s_capture_exit" % (e, e),
        "",
        "  ! rkp is 8 in the target build; declared here so this module does not",
        "  ! depend on the target's parameters module.",
        "  INTEGER, PARAMETER :: wp = 8",
        "",
        "  INTEGER, SAVE            :: capture_round = 0",
        "  LOGICAL, SAVE            :: cfg_done      = .FALSE.",
        "  INTEGER, SAVE            :: cfg_first     = 1",
        "  INTEGER, SAVE            :: cfg_count     = 0",
        "  CHARACTER(LEN=512), SAVE :: cfg_dir       = '%s'" % data_dir,
        "",
        "CONTAINS",
        "",
        "  SUBROUTINE read_config()",
        "    IF (cfg_done) RETURN",
        "    cfg_done  = .TRUE.",
        "    cfg_first = env_int('FTG_CAPTURE_ROUND', 1)",
        "    cfg_count = env_int('FTG_CAPTURE_N', 0)",
        "    cfg_dir   = env_str('FTG_DATA_DIR', cfg_dir)",
        "  END SUBROUTINE read_config",
        "",
        "  INTEGER FUNCTION env_int(name, fallback)",
        "    CHARACTER(LEN=*), INTENT(IN) :: name",
        "    INTEGER, INTENT(IN)          :: fallback",
        "    CHARACTER(LEN=64)            :: buf",
        "    INTEGER                      :: ln, st, ios, val",
        "    env_int = fallback",
        "    CALL GET_ENVIRONMENT_VARIABLE(name, buf, ln, st)",
        "    IF (st == 0 .AND. ln > 0 .AND. ln <= LEN(buf)) THEN",
        "      READ (buf(1:ln), *, IOSTAT=ios) val",
        "      IF (ios == 0) env_int = val",
        "    END IF",
        "  END FUNCTION env_int",
        "",
        "  FUNCTION env_str(name, fallback) RESULT(res)",
        "    CHARACTER(LEN=*), INTENT(IN) :: name, fallback",
        "    CHARACTER(LEN=512)           :: res",
        "    INTEGER                      :: ln, st",
        "    res = fallback",
        "    CALL GET_ENVIRONMENT_VARIABLE(name, res, ln, st)",
        "    IF (st /= 0 .OR. ln == 0) res = fallback",
        "  END FUNCTION env_str",
        "",
        "  LOGICAL FUNCTION in_window()",
        "    in_window = (cfg_count > 0) .AND. (capture_round >= cfg_first) &",
        "                .AND. (capture_round < cfg_first + cfg_count)",
        "  END FUNCTION in_window",
        "",
        "  FUNCTION case_dir() RESULT(res)",
        "    CHARACTER(LEN=640) :: res",
        "    CHARACTER(LEN=8)   :: tag",
        "    WRITE (tag, '(A1,I0.4)') 'r', capture_round",
        "    res = TRIM(cfg_dir)//'/%s/'//TRIM(tag)" % region.case_stem,
        "  END FUNCTION case_dir",
        "",
        "  SUBROUTINE make_dir(path)",
        "    CHARACTER(LEN=*), INTENT(IN) :: path",
        "    INTEGER                      :: estat, cstat",
        "    CALL EXECUTE_COMMAND_LINE('mkdir -p \"'//TRIM(path)//'\"', .TRUE., estat, cstat)",
        "    IF (estat /= 0 .OR. cstat /= 0) THEN",
        "      WRITE (*,'(A,A)') ' [ftg] WARNING: could not create ', TRIM(path)",
        "    END IF",
        "  END SUBROUTINE make_dir",
        "",
    ]

    # --- entry
    L += [
        "  SUBROUTINE %s_capture_entry(%s)" % (e, ", ".join(f.name for f in region.inputs)),
    ]
    for f in region.inputs:
        L.append("    %s" % f.declare("IN"))
    L += [
        "    CHARACTER(LEN=648) :: dir",
        "",
        "    CALL read_config()",
        "    capture_round = capture_round + 1",
        "    IF (.NOT. in_window()) RETURN",
        "",
        "    dir = TRIM(case_dir())//'/input'",
        "    CALL make_dir(dir)",
        "    CALL ftg_set_serializer(TRIM(dir), '%s', 'w')" % e,
        "    CALL ftg_set_savepoint('entry')",
    ]
    for f in region.inputs:
        L.append("    %s" % f.write_call())
    L += [
        "    CALL ftg_destroy_savepoint()",
        "    CALL ftg_destroy_serializer()",
        "    WRITE (*,'(A,A)') ' [ftg] captured entry -> ', TRIM(dir)",
        "  END SUBROUTINE %s_capture_entry" % e,
        "",
    ]

    # --- exit
    L += [
        "  SUBROUTINE %s_capture_exit(%s)" % (e, ", ".join(f.name for f in region.exit_fields)),
    ]
    for f in region.exit_fields:
        L.append("    %s" % f.declare("IN"))
    L += [
        "    CHARACTER(LEN=648) :: dir",
        "",
        "    IF (.NOT. in_window()) RETURN",
        "",
        "    dir = TRIM(case_dir())//'/output'",
        "    CALL make_dir(dir)",
        "    CALL ftg_set_serializer(TRIM(dir), '%s', 'w')" % e,
        "    CALL ftg_set_savepoint('exit')",
    ]
    for f in region.exit_fields:
        L.append("    %s" % f.write_call())
    L += [
        "    CALL ftg_destroy_savepoint()",
        "    CALL ftg_destroy_serializer()",
        "    WRITE (*,'(A,A)') ' [ftg] captured exit  -> ', TRIM(dir)",
        "  END SUBROUTINE %s_capture_exit" % e,
        "",
        "END MODULE %s" % region.capture_mod,
    ]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# generated Fortran: replay driver
# ---------------------------------------------------------------------------


def gen_driver(region):
    e, L = region.entry, []
    imports = [region.entry] + [f.name for f in region.clobbers]
    call_args = ", ".join(region.call_args)
    L += banner(region, "Replay driver: re-run one captured case and compare.")
    L += [
        "! argv(1) = case directory (contains input/ and output/)",
        "! argv(2) = 'poison' to prefill the clobbered scratch arrays with",
        "!           signaling NaN before the call (write-before-read witness).",
        "!",
        "! Bitwise comparison: ftg_cmp_default_tolerance = 0, so ftg_compare fails on",
        "! ABS(actual-stored) > 0. ftg_compare treats NaN==NaN as equal and never",
        "! flags NaN-vs-number (the comparison is false for NaN), so the driver adds",
        "! an explicit IEEE_IS_NAN guard on every compared field.",
        "PROGRAM %s" % region.test_prog,
        "",
        "  USE %s, ONLY: %s" % (region.module, ", ".join(imports)),
        "  USE m_ser_ftg",
        "  USE m_ser_ftg_cmp",
        "  USE, INTRINSIC :: IEEE_ARITHMETIC",
        "  USE, INTRINSIC :: IEEE_EXCEPTIONS",
        "  IMPLICIT NONE",
        "",
        "  INTEGER, PARAMETER :: wp = 8",
        "",
    ]
    for f in region.inputs + region.outputs:
        L.append("  %s" % f.declare())
    L += [
        "  CHARACTER(LEN=1024) :: casedir, mode",
        "  INTEGER             :: ln, st, failures",
        "  LOGICAL             :: ok, poison, halt_inv, flag_inv",
        "  REAL(KIND=wp)       :: snan",
        "",
        "  CALL GET_COMMAND_ARGUMENT(1, casedir, ln, st)",
        "  IF (st /= 0 .OR. ln == 0) THEN",
        "    WRITE (*,'(A)') 'usage: %s <case-dir> [poison]'" % region.test_prog,
        "    CALL EXIT(2)",
        "  END IF",
        "  mode = ''",
        "  CALL GET_COMMAND_ARGUMENT(2, mode, ln, st)",
        "  poison = (TRIM(mode) == 'poison')",
        "",
        "  ! A missing or misspelled field must be an error, not a silent no-op.",
        "  ignore_not_existing = .FALSE.",
        "  ftg_cmp_default_tolerance              = 0.0",
        "  ftg_cmp_quiet                          = .FALSE.",
        "  ftg_cmp_count_missing_field_as_failure = .TRUE.",
        "  ftg_cmp_message_prefix                 = 'FTG %s ***'" % e,
        "",
        "  ! ---- inputs -------------------------------------------------------",
        "  CALL ftg_set_serializer(TRIM(casedir)//'/input', '%s', 'r')" % e,
        "  CALL ftg_set_savepoint('entry')",
    ]
    for f in region.inputs:
        L.append("  %s" % f.read_call())
    L += [
        "  CALL ftg_destroy_savepoint()",
        "  CALL ftg_destroy_serializer()",
        "",
        "  ! ---- poison the clobbered scratch ---------------------------------",
        "  IF (poison) THEN",
        "    halt_inv = .FALSE.",
        "    IF (IEEE_SUPPORT_HALTING(IEEE_INVALID)) THEN",
        "      CALL IEEE_GET_HALTING_MODE(IEEE_INVALID, halt_inv)",
        "      CALL IEEE_SET_HALTING_MODE(IEEE_INVALID, .FALSE.)",
        "    END IF",
        "    snan = IEEE_VALUE(1.0_wp, IEEE_SIGNALING_NAN)",
    ]
    for f in region.clobbers:
        L.append("    %s = snan" % f.name)
    L += [
        "    CALL IEEE_SET_FLAG(IEEE_INVALID, .FALSE.)",
        "    CALL IEEE_SET_FLAG(IEEE_DIVIDE_BY_ZERO, .FALSE.)",
        "    CALL IEEE_SET_FLAG(IEEE_OVERFLOW, .FALSE.)",
        "    IF (IEEE_SUPPORT_HALTING(IEEE_INVALID)) THEN",
        "      CALL IEEE_SET_HALTING_MODE(IEEE_INVALID, halt_inv)",
        "    END IF",
        "  END IF",
        "",
        "  ! ---- the region ---------------------------------------------------",
        "  CALL %s(%s)" % (e, call_args),
        "",
        "  IF (poison) THEN",
        "    CALL IEEE_GET_FLAG(IEEE_INVALID, flag_inv)",
        "    WRITE (*,'(A,L1)') ' [ftg] IEEE_INVALID raised during the call: ', flag_inv",
        "    WRITE (*,'(A)')    ' [ftg] (a raised flag is reported, not a failure;'// &",
        "                       ' the gate is that the outputs still match)'",
        "  END IF",
        "",
        "  ! ---- compare against the captured outputs -------------------------",
        "  ! The trap has done its job by now (it fires inside the region). Disable it",
        "  ! so a signaling NaN that survived into a scratch array is reported by",
        "  ! nan_guard as a failure instead of killing the process in the comparison.",
        "  IF (IEEE_SUPPORT_HALTING(IEEE_INVALID)) CALL IEEE_SET_HALTING_MODE(IEEE_INVALID, .FALSE.)",
        "  failures = 0",
        "  CALL ftg_set_serializer(TRIM(casedir)//'/output', '%s', 'r')" % e,
        "  CALL ftg_set_savepoint('exit')",
    ]
    for f in region.exit_fields:
        body = ["  %s" % f.compare_call("failures")]
        if not f.is_int:
            flat = "[%s]" % f.name if not f.dims else "RESHAPE(%s, [SIZE(%s)])" % (f.name, f.name)
            body.append("  CALL nan_guard('%s', %s, failures)" % (f.name, flat))
        if f.defined_when:
            L.append("  IF (%s) THEN   ! live_out.defined_when" % f.defined_when)
            L += ["  " + b for b in body]
            L += [
                "  ELSE",
                "    WRITE (*,'(A)') ' [ftg] skipped %s: not defined when .NOT.(%s)'"
                % (f.name, f.defined_when),
                "  END IF",
            ]
        else:
            L += body
    L += [
        "  CALL ftg_destroy_savepoint()",
        "  CALL ftg_destroy_serializer()",
        "",
        "  IF (failures == 0) THEN",
        "    WRITE (*,'(A)') 'TEST PASSED'",
        "  ELSE",
        "    WRITE (*,'(A,I0)') 'TEST FAILED, failures: ', failures",
        "    CALL EXIT(1)",
        "  END IF",
        "",
        "CONTAINS",
        "",
        "  ! ftg_compare cannot see a NaN that replaced a finite reference value:",
        "  ! ABS(NaN - x) > tol is .FALSE.. Count NaN in a replayed field as a failure.",
        "  SUBROUTINE nan_guard(name, values, failure_count)",
        "    CHARACTER(LEN=*), INTENT(IN)  :: name",
        "    REAL(KIND=wp), INTENT(IN)     :: values(:)",
        "    INTEGER, INTENT(INOUT)        :: failure_count",
        "    IF (ANY(IEEE_IS_NAN(values))) THEN",
        "      WRITE (*,'(A,A,A,I0,A)') ' FTG %s *** ', name, ' : NaN in replayed field (', &" % e,
        "        COUNT(IEEE_IS_NAN(values)), ' element(s))'",
        "      failure_count = failure_count + 1",
        "    END IF",
        "  END SUBROUTINE nan_guard",
        "",
        "END PROGRAM %s" % region.test_prog,
    ]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# generated shell: build.sh, apply.sh
# ---------------------------------------------------------------------------

# Production flags, from codes/CoarseAIR/config/cmake/SetFortranFlags.cmake
# (gfortran, CMAKE_BUILD_TYPE=release).
CA_FLAGS = ("-cpp -fno-unsafe-math-optimizations -fdefault-double-8 -fdefault-real-8 "
            "-ffree-line-length-none -frealloc-lhs -O3 -fPIC -fbounds-check -fbacktrace")


def gen_build_sh(region, repo, gen_dir, replay_dir):
    e = region.entry
    return """#!/usr/bin/env bash
# GENERATED by tools/regionharness/gen_harness.py -- DO NOT EDIT.
# Build the replay driver for region %(region)s.
#
# MODE=tree (default): link the instrumented CoarseAIR build. The target module
#   comes from libcoarseair.a, so replay runs the *same object code* that
#   produced the capture -- the only mode in which bitwise equality is meaningful.
# MODE=copy: compile a patched copy of the target given by TARGET_SRC. Used to
#   validate the generator without touching codes/.
#
# Produces two binaries from the same source:
#   %(test)s   plain
#   %(poison)s -ffpe-trap=invalid, so a signaling NaN reaching arithmetic dies at
#              the offending instruction instead of propagating.
set -euo pipefail

REPO=${REPO:-%(repo)s}
SB=${SB:-%(sb)s}
CA=${CA:-$REPO/codes/CoarseAIR}
GEN=${GEN:-%(gen)s}
OUT=${OUT:-%(replay)s/build}
MODE=${MODE:-tree}
TARGET_SRC=${TARGET_SRC:-%(target)s}

FLAGS="%(flags)s"
LIBS_SB="$SB/lib/libSerialboxFortran.a $SB/lib/libSerialboxC.a $SB/lib/libSerialboxCore.a"

# Locate the build's module directory via a module the target USEs.
CA_MOD=${CA_MOD:-$(dirname "$(find "$CA/build" -name 'parameters_module.mod' | head -1)")}
CA_LIB=${CA_LIB:-$(find "$CA/build" -name 'libcoarseair.a' | head -1)}
if [ ! -d "$CA_MOD" ]; then
    echo "error: parameters_module.mod not found under $CA/build -- build CoarseAIR first" >&2
    exit 1
fi

mkdir -p "$OUT"
echo "MODE=$MODE  CA_MOD=$CA_MOD  CA_LIB=${CA_LIB:-<none>}"

# 1. capture module (needed by the patched target; harmless in the driver link)
gfortran $FLAGS -I "$SB/include" -J "$OUT" -c "$GEN/%(capmod)s.f90" -o "$OUT/%(capmod)s.o"

TARGET_OBJ=""
TARGET_MOD="$CA_MOD"
if [ "$MODE" = "copy" ]; then
    # -I $CA/src/PESs lets cpp resolve the target's #include "../qct.inc".
    gfortran $FLAGS -I "$CA/src/PESs" -I "$OUT" -I "$CA_MOD" -I "$SB/include" \\
        -J "$OUT" -c "$TARGET_SRC" -o "$OUT/target.o"
    TARGET_OBJ="$OUT/target.o"
    TARGET_MOD="$OUT"
fi

# 2. driver, twice: the poison binary differs only by the trap flag.
gfortran $FLAGS -I "$TARGET_MOD" -I "$OUT" -I "$CA_MOD" -I "$SB/include" -J "$OUT" \\
    -c "$GEN/%(test)s.f90" -o "$OUT/driver.o"
gfortran $FLAGS -ffpe-trap=invalid -I "$TARGET_MOD" -I "$OUT" -I "$CA_MOD" -I "$SB/include" \\
    -J "$OUT" -c "$GEN/%(test)s.f90" -o "$OUT/driver_poison.o"

if [ "${COMPILE_ONLY:-0}" = "1" ] || [ -z "$CA_LIB" ]; then
    echo "objects built in $OUT (no link: COMPILE_ONLY or libcoarseair.a missing)"
    exit 0
fi

# 3. link
for pair in "driver.o:%(test)s" "driver_poison.o:%(poison)s"; do
    obj=${pair%%%%:*}; exe=${pair##*:}
    gfortran $FLAGS -o "$OUT/$exe" "$OUT/$obj" "$OUT/%(capmod)s.o" $TARGET_OBJ \\
        "$CA_LIB" $LIBS_SB -lstdc++ -lpthread -llapack -lblas
    echo "built $OUT/$exe"
done

echo
echo "run:  $OUT/%(test)s   <FTG_DATA_DIR>/%(case)s/rNNNN"
echo "      $OUT/%(poison)s <FTG_DATA_DIR>/%(case)s/rNNNN poison"
""" % dict(region=region.name, repo=repo, sb=SB_DEFAULT, gen=gen_dir, replay=replay_dir,
           target=region.src, flags=CA_FLAGS, capmod=region.capture_mod,
           test=region.test_prog, poison="ftg_%s_poison" % e, case=region.case_stem)


def gen_apply_sh(region, repo, gen_dir, replay_dir, spec_rel):
    e = region.entry
    ca = region.cmake_root
    src_rel = region.src.relative_to(ca)
    pes_rel = region.src.parent.relative_to(ca)
    return """#!/usr/bin/env bash
# GENERATED by tools/regionharness/gen_harness.py -- DO NOT EDIT.
# Documentation-as-script: instrument the REAL CoarseAIR tree for region
# %(region)s. Read it before running it; it modifies codes/.
#
# How CoarseAIR collects sources (config/cmake/add_sources.cmake, verified):
#   there is NO glob. Each directory's CMakeLists.txt calls add_sources() with an
#   explicit file list, which appends absolute paths to the global SRCS_LIST
#   property; the top-level CMakeLists.txt calls get_sources(SRCS) and builds
#   libcoarseair from that list. A new source file is therefore invisible to the
#   build until its name is added to src/PESs/CMakeLists.txt by hand. CMake's
#   Fortran dependency scanner orders the compilation, so position in the list
#   does not matter.
set -euo pipefail

REPO=${REPO:-%(repo)s}
SB=${SB:-%(sb)s}
CA=${CA:-%(ca)s}
GEN=${GEN:-%(gen)s}
HARNESS=$REPO/tools/regionharness/gen_harness.py
SPEC=$REPO/%(spec)s

# --- 0. regenerate (deterministic; no-op if already current)
python3 "$HARNESS" "$SPEC" --out-dir "$GEN"

# --- 1. patch the target in place (idempotent; reverse with --restore)
python3 "$HARNESS" "$SPEC" --patch
python3 "$HARNESS" "$SPEC" --check

# --- 2. drop the capture module into the tree and register it with CMake
cp "$GEN/%(capmod)s.f90" "$CA/%(pes)s/%(capmod_f)s"
if ! grep -q '%(capmod_f)s' "$CA/%(pes)s/CMakeLists.txt"; then
    python3 - "$CA/%(pes)s/CMakeLists.txt" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p).read()
# Insert into the first add_sources( ... ) list in this file.
t = re.sub(r'(add_sources\\s*\\(\\s*\\n)', r'\\1  %(capmod_f)s\\n', t, count=1)
open(p, 'w').write(t)
PY
fi

# --- 3. reconfigure so every compile sees the serialbox .mod files and every
#        link pulls the serialbox archives. SetFortranFlags.cmake *appends* to
#        CMAKE_Fortran_FLAGS, so a user value survives. CMAKE_Fortran_STANDARD_LIBRARIES
#        is already used by this project for -llapack -lblas; extend it.
cmake -S "$CA" -B "$CA/build" \\
  -DCMAKE_BUILD_TYPE=release \\
  -DCMAKE_Fortran_FLAGS="-I $SB/include" \\
  -DCMAKE_Fortran_STANDARD_LIBRARIES="-llapack -lblas $SB/lib/libSerialboxFortran.a $SB/lib/libSerialboxC.a $SB/lib/libSerialboxCore.a -lstdc++ -lpthread"

# --- 4. rebuild
make -C "$CA/build" -j"$(nproc)"

# --- 5. capture. The instrumented binary is inert unless FTG_CAPTURE_N > 0.
cat <<EOF

Instrumented. To capture 3 calls starting at the 500th call to %(entry)s:

  export FTG_DATA_DIR=$REPO/ftgdata
  export FTG_CAPTURE_ROUND=500
  export FTG_CAPTURE_N=3
  <run the CoarseAIR case as usual>

Cases land in \\$FTG_DATA_DIR/%(case)s/rNNNN/{input,output}.
Then:  MODE=tree %(replay)s/build.sh
       %(replay)s/build/%(test)s \\$FTG_DATA_DIR/%(case)s/r0500

To un-instrument:
  python3 "$HARNESS" "$SPEC" --restore
  rm -f "$CA/%(pes)s/%(capmod_f)s"   # and remove its line from %(pes)s/CMakeLists.txt
  git -C "$CA" diff -- %(src)s       # must be empty
EOF
""" % dict(region=region.name, repo=repo, sb=SB_DEFAULT, gen=gen_dir, replay=replay_dir,
           spec=spec_rel, capmod=region.capture_mod,
           capmod_f="%s.F90" % region.capture_mod, ca=ca,
           pes=pes_rel, entry=e, case=region.case_stem,
           test=region.test_prog, src=src_rel)


# ---------------------------------------------------------------------------
# patcher
# ---------------------------------------------------------------------------

BEGIN = "!FTG-BEGIN "
END = "!FTG-END "


def indent_of(line):
    return line[: len(line) - len(line.lstrip())]


def find_unique(lines, pattern, lo, hi, what):
    """0-based indices of lines in [lo,hi] (1-based, inclusive) matching pattern."""
    rx = re.compile(pattern, re.IGNORECASE)
    hits = [i for i in range(lo - 1, min(hi, len(lines))) if rx.match(code_part(lines[i]))]
    if not hits:
        die("anchor not found: %s (searched lines %d-%d)" % (what, lo, hi))
    if len(hits) > 1:
        die("anchor is ambiguous: %s matches lines %s"
            % (what, ", ".join(str(i + 1) for i in hits)))
    return hits[0]


def find_first_executable(lines, start, hi):
    """First executable statement after 0-based index `start`, within 1-based hi."""
    for i in range(start + 1, min(hi, len(lines))):
        code = code_part(lines[i]).strip()
        if not code or DECL_KW.match(code) or "::" in code:
            continue
        return i
    die("no executable statement found after line %d" % (start + 1))


def block(tag, indent, body):
    return [BEGIN + tag, indent + body, END + tag]


def plan_patch(region, lines):
    """List of (0-based anchor index, 'before'|'after', tag, [lines]) insertions."""
    e = region.entry
    contains = next((i for i, l in enumerate(lines)
                     if re.match(r"\s*contains\s*$", code_part(l), re.IGNORECASE)), len(lines))
    vis = find_unique(lines, r"\s*public\s*::", 1, contains, "module PUBLIC statement")
    sub = find_unique(lines, r"\s*subroutine\s+%s\s*\(" % e, region.lo, region.hi,
                      "SUBROUTINE %s statement" % e)
    endsub = find_unique(lines, r"\s*end\s*(subroutine\b.*)?$", region.lo, region.hi,
                         "END SUBROUTINE of %s" % e)
    first = find_first_executable(lines, sub, region.hi)

    names = ", ".join([e] + [f.name for f in region.clobbers])
    entry_args = ", ".join(f.name for f in region.inputs)
    exit_args = ", ".join(f.name for f in region.exit_fields)
    return [
        (vis, "after", "visibility",
         block("visibility", indent_of(lines[vis]), "public :: %s" % names)),
        (sub, "after", "use",
         block("use", indent_of(lines[first]),
               "use %s, only: %s_capture_entry, %s_capture_exit"
               % (region.capture_mod, e, e))),
        (first, "before", "entry",
         block("entry", indent_of(lines[first]),
               "call %s_capture_entry(%s)" % (e, entry_args))),
        (endsub, "before", "exit",
         block("exit", indent_of(lines[endsub]) + "  ",
               "call %s_capture_exit(%s)" % (e, exit_args))),
    ]


def line_ending(line):
    """The target sources are CRLF; inserted lines must not silently convert them."""
    for nl in ("\r\n", "\n", "\r"):
        if line.endswith(nl):
            return nl
    return "\n"


def apply_patch(region, text):
    keep = text.splitlines(True)          # with line terminators, for reconstruction
    lines = text.splitlines()             # without, for matching
    out = list(keep)
    for idx, where, _tag, body in sorted(plan_patch(region, lines), key=lambda p: -p[0]):
        nl = line_ending(keep[idx])
        at = idx + 1 if where == "after" else idx
        out[at:at] = [b + nl for b in body]
    return "".join(out)


def strip_patch(text):
    out, depth = [], 0
    for line in text.splitlines(True):
        if line.startswith(BEGIN):
            depth += 1
            continue
        if line.startswith(END):
            depth -= 1
            if depth < 0:
                die("unbalanced FTG markers: %s without a matching begin" % line.strip())
            continue
        if depth == 0:
            out.append(line)
    if depth != 0:
        die("unbalanced FTG markers: %d block(s) left open" % depth)
    return "".join(out)


def is_patched(text):
    return any(l.startswith(BEGIN) for l in text.splitlines())


def show_diff(old, new, path):
    sys.stdout.writelines(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile=str(path), tofile=str(path) + " (patched)", n=3))


def run_patch(region, target, dry_run):
    text = read_src(target)
    if is_patched(text):
        print("already patched: %s (no change)" % target)
        return 0
    new = apply_patch(region, text)
    show_diff(text, new, target)
    if dry_run:
        print("--dry-run: %s not modified" % target)
        return 0
    write_src(target, new)
    print("patched %s" % target)
    return 0


def run_restore(region, target, dry_run):
    text = read_src(target)
    if not is_patched(text):
        print("not patched: %s (no change)" % target)
        return 0
    new = strip_patch(text)
    show_diff(text, new, target)
    if dry_run:
        print("--dry-run: %s not modified" % target)
        return 0
    write_src(target, new)
    print("restored %s" % target)
    return 0


# ---------------------------------------------------------------------------


def write_if_changed(path, content, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = (not path.exists()) or path.read_text() != content
    if changed:
        path.write_text(content)
    if executable:
        path.chmod(0o755)
    print("  %s %s" % ("wrote  " if changed else "same   ", path))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("region_yaml")
    ap.add_argument("--repo-root", default=str(REPO_DEFAULT))
    ap.add_argument("--out-dir", help="directory for generated Fortran (default cases/<region>/gen)")
    ap.add_argument("--replay-dir", help="directory for build.sh/apply.sh (default cases/<region>/replay)")
    ap.add_argument("--data-dir", default=DATA_DIR_DEFAULT, help="default FTG_DATA_DIR baked into the capture module")
    ap.add_argument("--target", help="file to patch (must have the anchor's basename)")
    ap.add_argument("--patch", action="store_true", help="insert the marker blocks")
    ap.add_argument("--restore", action="store_true", help="remove the marker blocks")
    ap.add_argument("--check", action="store_true", help="report patched/unpatched")
    ap.add_argument("--dry-run", action="store_true", help="with --patch/--restore: diff only")
    args = ap.parse_args()

    if sum([args.patch, args.restore, args.check]) > 1:
        die("--patch, --restore and --check are mutually exclusive")

    repo = Path(args.repo_root).resolve()
    spec_path = Path(args.region_yaml).resolve()
    spec = yaml.safe_load(spec_path.read_text())
    try:
        spec_rel = spec_path.relative_to(repo)
    except ValueError:
        spec_rel = spec_path
    region = Region(spec, repo, spec_rel)

    if args.patch or args.restore or args.check:
        target = Path(args.target).resolve() if args.target else region.src
        # Never open a same-named routine in another file: the basename must be
        # the anchor's. (There is a different n4pes in N3_UMN_PES_Class.F90.)
        if target.name != region.src.name:
            die("--target %s does not match the anchor file name %s" % (target.name, region.src.name))
        if not target.is_file():
            die("target not found: %s" % target)
        if args.check:
            print("%s: %s" % (target, "patched" if is_patched(read_src(target)) else "unpatched"))
            return 0
        if args.patch:
            return run_patch(region, target, args.dry_run)
        return run_restore(region, target, args.dry_run)

    case = repo / "tools/regionharness/cases" / region.name.split(".")[0]
    gen_dir = Path(args.out_dir).resolve() if args.out_dir else case / "gen"
    replay_dir = Path(args.replay_dir).resolve() if args.replay_dir else case / "replay"

    print("region %s (entry %s) -> %s" % (region.name, region.entry, gen_dir))
    write_if_changed(gen_dir / ("%s.f90" % region.capture_mod),
                     gen_capture_mod(region, args.data_dir))
    write_if_changed(gen_dir / ("%s.f90" % region.test_prog), gen_driver(region))
    write_if_changed(replay_dir / "build.sh",
                     gen_build_sh(region, repo, gen_dir, replay_dir), executable=True)
    write_if_changed(replay_dir / "apply.sh",
                     gen_apply_sh(region, repo, gen_dir, replay_dir, spec_rel), executable=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
