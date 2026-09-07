ifeq ($(origin FC),default)
FC := gfortran
endif
FFLAGS ?= -O2
LDFLAGS ?=
HARNESS ?= ../../../services/builder/harness
MODFLAG ?= -J
COMMON = $(HARNESS)/npy_io.f90 src/potential.f90 src/initial.f90

# The operator selects the toolchains. These rules are frozen at onboarding;
# a port changes the permitted sources, not the build recipe.
ifneq ($(strip $(PTXAS)),)
ACCEL = potential_gpu.o potential.cubin
potential_gpu.o: src/potential.cpp
	$(CXX) $(CXXFLAGS) -c $< -o $@
potential.cubin: src/potential.ptx
	$(PTXAS) $(PTXASFLAGS) $< -o $@
else ifneq ($(strip $(NVCC)),)
ACCEL = potential_gpu.o
potential_gpu.o: src/potential.cu
	$(NVCC) $(NVCCFLAGS) -c $< -o $@
endif
OBJECTS = $(filter %.o,$(ACCEL))

replay: $(COMMON) harness/replay.f90 $(ACCEL)
	$(FC) $(FFLAGS) $(MODFLAG) . $(COMMON) harness/replay.f90 $(OBJECTS) -o replay $(LDFLAGS)
capture: $(COMMON) harness/capture.f90 $(ACCEL)
	$(FC) $(FFLAGS) $(MODFLAG) . $(COMMON) harness/capture.f90 $(OBJECTS) -o capture $(LDFLAGS)
timing: $(COMMON) harness/main.f90 $(ACCEL)
	$(FC) $(FFLAGS) $(MODFLAG) . $(COMMON) harness/main.f90 $(OBJECTS) -o timing $(LDFLAGS)
