# Local CPU baseline build of the tsunami driver.
#
# NOTE: this Makefile is a developer convenience only. The gated pipeline does
# NOT use it -- the builder service compiles the sources with its own pinned
# command lines (see builder/stages.py), so the agent editing this file cannot
# change how the gates build the code.

# Force gfortran (make predefines FC=f77 as a builtin, which ?= cannot override).
ifeq ($(origin FC),default)
FC := gfortran
endif
FFLAGS ?= -O2 -ffree-line-length-none

SRC = src/mod_params.f90 src/mod_diff.f90 src/mod_initial.f90 src/mod_kernel.f90 src/tsunami.f90

tsunami: $(SRC)
	$(FC) $(FFLAGS) -o $@ $(SRC)

run: tsunami
	./tsunami

clean:
	rm -f tsunami *.mod

.PHONY: run clean
