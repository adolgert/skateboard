# How this code is built.
#
# The harness builds a submitted copy of this tree by running
#   make -f Makefile <target> ...
# in the tree root, with FC, FFLAGS, LDFLAGS, MODFLAG and HARNESS set in the
# environment. FC is a shim that records every compiler invocation and then
# runs the strategy's compiler, so what this file does with FFLAGS is visible
# afterwards: every compile below must pass $(FFLAGS) through, or the build is
# reported as one that ignored the strategy.
#
# The targets and the files they leave are named in ../manifest.yaml; the two
# must agree. Running make here by hand builds the same three programs with
# gfortran against the harness beside this checkout.
#
#   replay   -> replay          one case in, one case out; what the gates run
#   timing   -> tsunami         the whole program, at the size it is timed at
#   capture  -> gen_reference   the reference generator, run when onboarding

# make predefines FC=f77 as a builtin, which ?= cannot override; an FC from the
# environment or the command line has a different origin and wins.
ifeq ($(origin FC),default)
FC := gfortran
endif
FFLAGS ?= -O2 -ffree-line-length-none
LDFLAGS ?=
# Where npy_io.f90 lives: the capture format's Fortran side, which belongs to
# the harness and not to this code. The default reaches the copy in this
# checkout so a hand build works; the builder sets it to its own baked copy.
HARNESS ?= ../../../services/builder/harness
# How this compiler spells "put the .mod files here": -module for nvfortran,
# -J for gfortran. The builder fills it in from the compiler it was given.
MODFLAG ?= -module

NPY_IO = $(HARNESS)/npy_io.f90
KERNEL = src/mod_params.f90 src/mod_diff.f90 src/mod_initial.f90 src/mod_kernel.f90
PROGRAM = $(KERNEL) src/tsunami.f90

replay: $(NPY_IO) $(KERNEL) harness/replay.f90
	$(FC) $(FFLAGS) $(MODFLAG) . -o $@ $(NPY_IO) $(KERNEL) harness/replay.f90 $(LDFLAGS)

timing: tsunami

tsunami: $(PROGRAM)
	$(FC) $(FFLAGS) $(MODFLAG) . -o $@ $(PROGRAM) $(LDFLAGS)

capture: gen_reference

gen_reference: $(NPY_IO) $(KERNEL) harness/gen_reference.f90
	$(FC) $(FFLAGS) $(MODFLAG) . -o $@ $(NPY_IO) $(KERNEL) harness/gen_reference.f90 $(LDFLAGS)

run: tsunami
	./tsunami

clean:
	rm -f replay tsunami gen_reference *.mod

.PHONY: timing capture run clean
