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
  ! GPU port: uses `do concurrent` loops with inlined 2nd-order centered
  ! differencing (periodic boundaries), suitable for `-stdpar=gpu`.
  !
  ! SEMANTIC CONTRACT that any port MUST preserve:
  !   1. The momentum (`u`) update is computed first, from the OLD h and u.
  !   2. The continuity (`h`) update is computed SECOND, and uses the
  !      FRESHLY-UPDATED `u` together with the OLD `h`.
  !   3. Differencing is 2nd-order centered with PERIODIC boundaries
  !      (see mod_diff::diff_centered).
  ! ==========================================================================

  use iso_fortran_env, only: int32, real32
  use mod_params, only: g, dx, dt, hmean
  implicit none

  private
  public :: step

contains

  subroutine step(h, u)
    real(real32), intent(inout) :: h(:), u(:)

    real(real32), allocatable :: du(:), dh(:), duh(:), uh(:)
    integer(int32) :: n, i, ip, im

    n = size(h)
    allocate(du(n), dh(n), duh(n), uh(n))

    ! ---- momentum: uses old u and old h ----
    do concurrent (i = 1:n)
      block
        integer(int32) :: ipp, imm
        ipp = i + 1
        if (ipp > n) ipp = 1
        imm = i - 1
        if (imm < 1) imm = n
        du(i) = 0.5_real32 * (u(ipp) - u(imm))
        dh(i) = 0.5_real32 * (h(ipp) - h(imm))
      end block
    end do

    do concurrent (i = 1:n)
      u(i) = u(i) - (u(i) * du(i) + g * dh(i)) / dx * dt
    end do

    ! ---- continuity: uses freshly-updated u and old h ----
    do concurrent (i = 1:n)
      uh(i) = u(i) * (hmean + h(i))
    end do

    do concurrent (i = 1:n)
      block
        integer(int32) :: ipp, imm
        ipp = i + 1
        if (ipp > n) ipp = 1
        imm = i - 1
        if (imm < 1) imm = n
        duh(i) = 0.5_real32 * (uh(ipp) - uh(imm))
      end block
    end do

    do concurrent (i = 1:n)
      h(i) = h(i) - duh(i) / dx * dt
    end do

  end subroutine step

end module mod_kernel
