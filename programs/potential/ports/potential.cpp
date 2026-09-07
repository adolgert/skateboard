#include <cuda.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <unistd.h>

static void checked(CUresult status) {
    if (status != CUDA_SUCCESS) {
        const char* message = nullptr;
        cuGetErrorString(status, &message);
        std::fprintf(stderr, "CUDA driver: %s\n", message ? message : "unknown error");
        std::abort();
    }
}

struct Kernel {
    CUdevice device;
    CUcontext context;
    CUmodule module;
    CUfunction function;
    Kernel() {
        checked(cuInit(0));
        checked(cuDeviceGet(&device, 0));
        checked(cuDevicePrimaryCtxRetain(&context, device));
        checked(cuCtxSetCurrent(context));
        // Resolve beside the immutable executable, never in writable timing scratch.
        char executable[4096];
        const auto length = readlink("/proc/self/exe", executable, sizeof(executable)-1);
        if (length <= 0 || length == sizeof(executable)-1) std::abort();
        executable[length] = '\0';
        std::string module_path(executable);
        module_path = module_path.substr(0, module_path.find_last_of('/')+1) + "potential.cubin";
        checked(cuModuleLoad(&module, module_path.c_str()));
        checked(cuModuleGetFunction(&function, module, "charged_cloud"));
    }
    ~Kernel() {
        cuModuleUnload(module);
        cuDevicePrimaryCtxRelease(device);
    }
};

extern "C" void potential_accelerated(int n, const float* x, const float* y,
                                     const float* z, const float* q, float* phi) {
    static Kernel kernel;
    checked(cuCtxSetCurrent(kernel.context));
    CUdeviceptr storage;
    const size_t bytes = size_t(n) * sizeof(float);
    checked(cuMemAlloc(&storage, 5*bytes));
    CUdeviceptr dx=storage, dy=storage+bytes, dz=storage+2*bytes;
    CUdeviceptr dq=storage+3*bytes, output=storage+4*bytes;
    checked(cuMemcpyHtoD(dx, x, bytes));
    checked(cuMemcpyHtoD(dy, y, bytes));
    checked(cuMemcpyHtoD(dz, z, bytes));
    checked(cuMemcpyHtoD(dq, q, bytes));
    void* parameters[] = {&n, &dx, &dy, &dz, &dq, &output};
    checked(cuLaunchKernel(kernel.function, (n+127)/128,1,1, 128,1,1, 0,nullptr,parameters,nullptr));
    checked(cuMemcpyDtoH(phi, output, bytes));
    checked(cuMemFree(storage));
}
