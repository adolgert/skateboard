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

    ! Temporary arrays for differences
    real(real32) :: du(size(u)), dh(size(h))
    integer(int32) :: n, i
    
    n = size(u)

    do concurrent (i = 1:n)
      ! Compute the new u using old h and u
      du(i) = - (u(i) * (0.5*(u(modulo(i+1,n)+1) - u(modulo(i-1,n)+1))) + g * 0.5*(h(modulo(i+1,n)+1) - h(modulo(i-1,n)+1))) / dx * dt
      u(i) = u(i) + du(i)
    end do

    do concurrent (i = 1:n)
      ! Compute the new h using freshly updated u and old h
      dh(i) = - 0.5*(u(i+1) * (hmean + h(i+1)) - u(modulo(i-1,n)+1) * (hmean + h(modulo(i-1,n)+1))) / dx * dt
      h(i) = h(i) + dh(i)
    end do

  end subroutine step

end module mod_kernel
