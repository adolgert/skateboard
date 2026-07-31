module mod_kernel

  use iso_fortran_env, only: int32, real32
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
      du(i) = -u(i) * (0.5 * (u(mod(i+1,n)+1) + u(mod(i-1,n)+1)) - u(i)) / dx &
              - g * (0.5 * (h(mod(i+1,n)+1) + h(mod(i-1,n)+1)) - h(i)) / dx
    end do

    do concurrent (i = 1:n)
      u(i) = u(i) + du(i) * dt
    end do

    ! Compute the new h using freshly updated u and old h
    do i = 1, n
      dh(i) = -0.5 * ((u(mod(i+1,n)+1) + u(mod(i-1,n)+1)) &
                      *(hmean + 0.5*(h(mod(i+1,n)+1) + h(mod(i-1,n)+1)))) / dx   
    end do

    do concurrent (i = 1:n)
      h(i) = h(i) + dh(i) * dt
    end do

  end subroutine step

end module mod_kernel
