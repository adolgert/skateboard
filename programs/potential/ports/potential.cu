#include <cuda_runtime.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>

static void checked(cudaError_t status) {
    if (status != cudaSuccess) {
        std::fprintf(stderr, "CUDA: %s\n", cudaGetErrorString(status));
        std::abort();
    }
}

__global__ void charged_cloud(int n, const float* x, const float* y,
                             const float* z, const float* q, float* phi) {
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= n) return;
    float total = 0;
    for (int j = 0; j < n; ++j) {
        const float dx = x[i] - x[j], dy = y[i] - y[j], dz = z[i] - z[j];
        total += q[j] / sqrtf(dx*dx + dy*dy + dz*dz + 0.125f);
    }
    phi[i] = total;
}

extern "C" void potential_accelerated(int n, const float* x, const float* y,
                                     const float* z, const float* q, float* phi) {
    float* device;
    const size_t bytes = size_t(n) * sizeof(float);
    checked(cudaMalloc(&device, 5 * bytes));
    checked(cudaMemcpy(device, x, bytes, cudaMemcpyHostToDevice));
    checked(cudaMemcpy(device+n, y, bytes, cudaMemcpyHostToDevice));
    checked(cudaMemcpy(device+2*n, z, bytes, cudaMemcpyHostToDevice));
    checked(cudaMemcpy(device+3*n, q, bytes, cudaMemcpyHostToDevice));
    charged_cloud<<<(n+127)/128, 128>>>(n, device, device+n, device+2*n, device+3*n, device+4*n);
    checked(cudaGetLastError());
    checked(cudaMemcpy(phi, device+4*n, bytes, cudaMemcpyDeviceToHost));
    checked(cudaFree(device));
}
