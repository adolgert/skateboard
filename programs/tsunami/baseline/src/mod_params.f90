module mod_params
  ! Physical and numerical parameters shared by the driver and the
  ! capture/replay harness. Keeping these as named constants (rather than
  ! captured per-case values) guarantees the reference run and any replay
  ! use byte-identical parameters, so the only thing under test is the
  ! numerical kernel itself.
  use iso_fortran_env, only: real32
  implicit none
  private
  public :: g, dx, dt, hmean

  real(real32), parameter :: g     = 9.8   ! gravitational acceleration [m/s^2]
  real(real32), parameter :: dx    = 1.0   ! grid spacing [m]
  real(real32), parameter :: dt    = 0.02  ! time step [s]
  real(real32), parameter :: hmean = 10.0  ! mean water depth [m]

end module mod_params
