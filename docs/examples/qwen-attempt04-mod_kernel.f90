module mod_kernel

  use iso_fortran_env, only: int32, real32
  use mod_diff, only: diff_centered
  use mod_params, only: g, dx, dt, hmean
  implicit none

  private
  public :: step

contains

  subroutine step(h, u)
    real(real32), intent(inout) :: h(:), u(:)

    integer(int32) :: n, i
    real(real32) :: du(size(u)), dh(size(h))
    n = size(u)

    ! momentum: uses old u and old h
    do concurrent (i = 1:n)
      if (i == 1) then
        du(i) = u(2) - u(n)
      else if (i == n) then
        du(i) = u(1) - u(n-1)
      else
        du(i) = u(i+1) - u(i-1)
      end if
      du(i) = 0.5 * du(i)
    end do

    u = u - (u * du + g * diff_centered(h)) / dx * dt

    ! continuity: uses the just-updated u and old h
    do concurrent (i = 1:n)
      if (i == 1) then
        dh(i) = u(1) * (hmean + h(2)) - u(n) * (hmean + h(n))
      else if (i == n) then
        dh(i) = u(n) * (hmean + h(1)) - u(n-1) * (hmean + h(n-1))
      else
        dh(i) = u(i) * (hmean + h(i+1)) - u(i-1) * (hmean + h(i-1))
      end if
      dh(i) = 0.5 * dh(i)
    end do

    h = h - dh / dx * dt

  end subroutine step

end module mod_kernel
