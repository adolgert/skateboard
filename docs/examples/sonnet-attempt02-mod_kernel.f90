module mod_kernel

  ! ==========================================================================
  ! PORT TARGET
  ! --------------------------------------------------------------------------
  ! `step` advances the 1-D non-linear shallow water equations by one time
  ! step, updating water height `h` and velocity `u` in place.
  !
  !     du/dt + u du/dx = -g dh/dx           (momentum)
  !     dh/dt + d[u (hmean + h)]/dx = 0      (continuity)
  !
  ! This is a GPU port using `do concurrent` (stdpar) of the CPU baseline.
  ! The semantics (see contract below) are preserved exactly; only the
  ! execution model is changed.
  !
  ! SEMANTIC CONTRACT that any port MUST preserve:
  !   1. The momentum (`u`) update is computed first, from the OLD h and u.
  !   2. The continuity (`h`) update is computed SECOND, and uses the
  !      FRESHLY-UPDATED `u` together with the OLD `h`.
  !   3. Differencing is 2nd-order centered with PERIODIC boundaries
  !      (see mod_diff::diff_centered).
  ! ==========================================================================

  use iso_fortran_env, only: int32, real32
  use mod_diff, only: diff_centered
  use mod_params, only: g, dx, dt, hmean
  implicit none

  private
  public :: step

contains

  subroutine step(h, u)
    real(real32), intent(inout) :: h(:), u(:)
    real(real32), allocatable :: du(:), dh(:), u_old(:), hu(:), duh(:)
    integer(int32) :: n, i, ip, im

    n = size(h)
    allocate(du(n), dh(n), u_old(n), hu(n), duh(n))

    ! Save old u, since the momentum update must use OLD u and OLD h only.
    do concurrent (i = 1:n)
      u_old(i) = u(i)
    end do

    ! --- momentum: uses old u and old h ------------------------------------
    ! Inlined 2nd-order centered difference with periodic boundaries.
    do concurrent (i = 1:n)
      ip = mod(i, n) + 1
      im = mod(i - 2 + n, n) + 1
      du(i) = 0.5_real32 * (u_old(ip) - u_old(im))
      dh(i) = 0.5_real32 * (h(ip) - h(im))
    end do

    do concurrent (i = 1:n)
      u(i) = u_old(i) - (u_old(i) * du(i) + g * dh(i)) / dx * dt
    end do

    ! --- continuity: uses the just-updated u and old h ---------------------
    do concurrent (i = 1:n)
      hu(i) = u(i) * (hmean + h(i))
    end do

    do concurrent (i = 1:n)
      ip = mod(i, n) + 1
      im = mod(i - 2 + n, n) + 1
      duh(i) = 0.5_real32 * (hu(ip) - hu(im))
    end do

    do concurrent (i = 1:n)
      h(i) = h(i) - duh(i) / dx * dt
    end do

  end subroutine step

end module mod_kernel
