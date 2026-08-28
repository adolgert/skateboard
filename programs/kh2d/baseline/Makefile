FC ?= nvfortran
FFLAGS ?= -O2
LDFLAGS ?=
HARNESS ?= /opt/harness

MODULE_DIR_FLAG = $(if $(MODFLAG),$(MODFLAG) .,)

OBJS_COMMON = src/euler_module.o src/kh2d_module.o npy_io.o

.PHONY: all clean replay timing capture

all: replay timing capture

replay: harness/replay.o $(OBJS_COMMON)
	$(FC) $(FFLAGS) $(LDFLAGS) -o $@ $^

timing: src/kh2d.o $(OBJS_COMMON)
	$(FC) $(FFLAGS) $(LDFLAGS) -o $@ $^

capture: harness/capture.o $(OBJS_COMMON)
	$(FC) $(FFLAGS) $(LDFLAGS) -o $@ $^

src/euler_module.o: src/euler_module.f90
	$(FC) $(FFLAGS) $(MODULE_DIR_FLAG) -c $< -o $@

src/kh2d_module.o: src/kh2d_module.f90 src/euler_module.o
	$(FC) $(FFLAGS) $(MODULE_DIR_FLAG) -c $< -o $@

src/kh2d.o: src/kh2d.f90 src/kh2d_module.o src/euler_module.o npy_io.o
	$(FC) $(FFLAGS) $(MODULE_DIR_FLAG) -c $< -o $@

harness/replay.o: harness/replay.f90 src/kh2d_module.o src/euler_module.o npy_io.o
	$(FC) $(FFLAGS) $(MODULE_DIR_FLAG) -c $< -o $@

harness/capture.o: harness/capture.f90 src/kh2d_module.o src/euler_module.o npy_io.o
	$(FC) $(FFLAGS) $(MODULE_DIR_FLAG) -c $< -o $@

npy_io.o: $(HARNESS)/npy_io.f90
	$(FC) $(FFLAGS) $(MODULE_DIR_FLAG) -c $< -o $@

clean:
	rm -f replay timing capture *.o src/*.o harness/*.o *.mod kh2d.npy kh2d.dat
