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
  ! This is the CPU baseline. The agent's task is to make this routine execute
  ! on the GPU (e.g. `do concurrent` with -stdpar=gpu, or OpenMP `target`)
  ! while preserving its numerical meaning within the calibrated tolerance.
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

    ! momentum: uses old u and old h
    u = u - (u * diff_centered(u) + g * diff_centered(h)) / dx * dt

    ! continuity: uses the just-updated u and old h
    h = h - diff_centered(u * (hmean + h)) / dx * dt

  end subroutine step

end module mod_kernel
