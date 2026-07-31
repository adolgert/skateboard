module mod_kernel

  use iso_fortran_env, only: int32, real32
  use mod_params, only: g, dx, dt, hmean
  implicit none

  private
  public :: step

contains

  subroutine step(h, u)
    real(real32), intent(inout) :: h(:), u(:)
    integer(int32) :: i, n, im
    real(real32) :: du_dx(size(u)), dh_dx(size(h))

    n = size(h)
    im = n - 1

    ! momentum: uses old u and old h (gradient computation)
    du_dx(1) = u(2) - u(im)
    du_dx(n) = u(1) - u(im-1)
    dh_dx(1) = h(2) - h(im)
    dh_dx(n) = h(1) - h(im-1)
    !$omp target teams distribute parallel do simd map(to: u, im, n) map(du_dx)
    do i = 2, im
      du_dx(i) = u(i+1) - u(i-1)
      dh_dx(i) = h(i+1) - h(i-1)
    end do
    !$omp end target teams distribute parallel do simd
    du_dx = 0.5 * du_dx
    dh_dx = 0.5 * dh_dx

    ! momentum: uses old u and old h (update step)
    !$omp target teams distribute parallel do simd map(to: u, du_dx, dh_dx, g, dx, dt) map(u)
    do i = 1, n
      u(i) = u(i) - (u(i)*du_dx(i) + g*dh_dx(i)) / dx * dt
    end do
    !$omp end target teams distribute parallel do simd

    ! continuity: uses the just-updated u and old h
    du_dx = 0.5 * (u(2:n) - u(:im))
    du_dx(1) = 0.5 * (u(2) - u(n))

    !$omp target teams distribute parallel do simd map(to: h, u, du_dx, dx, dt, hmean) map(h)
    do i = 1, n
      h(i) = h(i) - (du_dx(i)*(hmean + h(i))) / dx * dt
    end do
    !$omp end target teams distribute parallel do simd

  end subroutine step

end module mod_kernel
