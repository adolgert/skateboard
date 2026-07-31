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
        real(real32) :: du_dx(size(u)), dh_dx(size(h)), dudx_upd(size(u))

        n = size(h)
        im = n - 1

        ! Compute spatial gradients directly in the loops for offloading to GPU.
        ! Differences are computed with periodic boundaries using temporary arrays.

        ! momentum: uses old u and old h (gradient computation)
        du_dx(1) = u(2) - u(im)
        du_dx(n) = u(1) - u(im-1)
        dh_dx(1) = h(2) - h(im)
        dh_dx(n) = h(1) - h(im-1)
        du_dx(2:n-1) = u(3:n) - u(1:im)
        dh_dx(2:n-1) = h(3:n) - h(1:im)
        du_dx = 0.5 * du_dx
        dh_dx = 0.5 * dh_dx

        ! momentum: uses old u and old h (update step)
        !$acc parallel loop present(u, du_dx, dh_dx)
        do i = 1, n
            dudx_upd(i) = u(i) - (u(i)*du_dx(i) + g*dh_dx(i)) / dx * dt
        end do

        ! continuity: uses the just-updated u and old h
        du_dx = dudx_upd(2:n) - dudx_upd(:im)
        du_dx(1) = dudx_upd(2) - dudx_upd(n)
        du_dx = 0.5 * du_dx

        !$acc parallel loop present(h, u, du_dx)
        do i = 1, n
            h(i) = h(i) - (du_dx(i)*(hmean + h(i))) / dx * dt
        end do

        ! Sync updated arrays back to CPU for next iteration.
        !$acc update host(u, h)

    end subroutine step

end module mod_kernel
