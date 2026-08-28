program kh2d
  use iso_fortran_env, only: int32, real64
  use euler_module, only: dp, gam, kh_init
  use kh2d_module, only: kh2d_step, prims
  use npy_io, only: npy_save
  implicit none

  integer(int32) :: Nx, Ny
  integer(int32), parameter :: ng = 2
  real(dp), parameter :: Lx = 1.0_dp, Ly = 1.0_dp
  real(dp) :: t_end, cfl
  character(len=256) :: arg

  real(dp), allocatable :: Q(:,:,:)
  real(dp) :: dx, dy, dt, t
  integer :: i, j, step_count, io
  real(dp) :: xc, yc, rho, vx, vy, pr, mass, ke

  if (command_argument_count() >= 2) then
     call get_command_argument(1, arg)
     read(arg, *) Nx
     call get_command_argument(2, arg)
     read(arg, *) Ny
  else
     Nx = 128
     Ny = 128
  end if
  if (command_argument_count() >= 3) then
     call get_command_argument(3, arg)
     read(arg, *) t_end
  else
     t_end = 1.5_dp
  end if
  if (command_argument_count() >= 4) then
     call get_command_argument(4, arg)
     read(arg, *) cfl
  else
     cfl = 0.4_dp
  end if

  dx = Lx/real(Nx, dp)
  dy = Ly/real(Ny, dp)
  allocate(Q(4, 1-ng:Nx+ng, 1-ng:Ny+ng))

  do j = 1-ng, Ny+ng
     do i = 1-ng, Nx+ng
        xc = (real(i,dp) - 0.5_dp)*dx
        yc = (real(j,dp) - 0.5_dp)*dy
        call kh_init(xc, yc, rho, vx, vy, pr)
        Q(1,i,j) = rho
        Q(2,i,j) = rho*vx
        Q(3,i,j) = rho*vy
        Q(4,i,j) = pr/(gam-1.0_dp) + 0.5_dp*rho*(vx*vx+vy*vy)
     end do
  end do

  t = 0.0_dp
  step_count = 0
  do while (t < t_end)
     call kh2d_step(Q, Nx, Ny, ng, dx, dy, cfl, t_end - t, dt)
     t = t + dt
     step_count = step_count + 1
  end do

  mass = 0.0_dp
  ke = 0.0_dp
  do j = 1, Ny
     do i = 1, Nx
        call prims(Q(:,i,j), rho, vx, vy, pr)
        mass = mass + rho*dx*dy
        ke   = ke + 0.5_dp*rho*(vx*vx+vy*vy)*dx*dy
     end do
  end do

  print *, "=========================================="
  print *, " 2D Kelvin-Helmholtz (Rusanov, Strang split)"
  print *, "=========================================="
  write(*,'(A,I4,A,I4)') "  grid    = ", Nx, " x ", Ny
  write(*,'(A,F8.3)')    "  t_end   = ", t_end
  write(*,'(A,I8)')      "  steps   = ", step_count
  write(*,'(A,ES14.6)')  "  mass    = ", mass
  write(*,'(A,ES14.6)')  "  KE      = ", ke
  print *, " (mass should remain ~ 1.5 on [0,1]^2 with this IC)"
  print *, " density slice written to kh2d.dat"

  open(newunit=io, file="kh2d.dat", status="replace", action="write")
  write(io,'(A)') "# x  y  rho  vx  vy"
  do j = 1, Ny
     do i = 1, Nx
        xc = (real(i,dp)-0.5_dp)*dx
        yc = (real(j,dp)-0.5_dp)*dy
        call prims(Q(:,i,j), rho, vx, vy, pr)
        write(io,'(5ES16.7)') xc, yc, rho, vx, vy
     end do
     write(io,*)
  end do
  close(io)

  call npy_save("kh2d.npy", Q)

  ! --- Machine-checkable sanity assertions ---
  block
    real(dp) :: mean_rho, expected, tol, rhoij
    integer :: nanflag
    mean_rho = mass / (Lx*Ly)
    expected = 1.5_dp
    tol      = 0.01_dp * expected     ! 1 %
    print *, ""
    print *, " --- physics assertions ---"
    nanflag = 0
    do j = 1, Ny
       do i = 1, Nx
          call prims(Q(:,i,j), rhoij, vx, vy, pr)
          if (rhoij /= rhoij) nanflag = 1
          if (vx /= vx)       nanflag = 1
          if (vy /= vy)       nanflag = 1
          if (pr /= pr)       nanflag = 1
       end do
    end do
    if (nanflag /= 0) then
       print *, " FAIL: NaN detected in final state"
       error stop 1
    end if
    print *, " PASS: no NaN in final state"

    if (.not. (abs(mean_rho - expected) < tol)) then
       write(*,'(A,ES12.4,A,ES12.4,A,ES12.4)') &
            " FAIL: mean_rho=", mean_rho, " expected=", expected, " tol=", tol
       error stop 1
    end if
    write(*,'(A,ES12.4)') " PASS: mean_rho=", mean_rho
  end block

end program kh2d
