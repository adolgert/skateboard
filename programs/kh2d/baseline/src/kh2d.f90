! kh2d.f90
!
! 2D Kelvin-Helmholtz instability on a periodic domain using the
! compressible Euler equations, solved with Rusanov (local Lax-
! Friedrichs) flux and Strang dimensional splitting.
!
! Rusanov flux (Rusanov 1961, J. Comput. Math. Math. Phys. 1, 267):
!   F_i+1/2 = 1/2 (F_L + F_R) - 1/2 a_max (U_R - U_L)
! where a_max is the maximum signal speed across the interface.
! Simpler and more dissipative than HLLC but unconditionally stable
! and trivially generalizes to multiple dimensions.
!
! Initial condition follows McNally, Lyra & Passy (2012, ApJS 201, 18),
! "A well-posed Kelvin-Helmholtz instability test and comparison":
!   - Smooth tanh ramp in density and shear to avoid grid-aligned
!     instabilities
!   - Small single-mode perturbation in v_y
!
! Outputs final density to kh2d.dat.

program kh2d
  use euler_module, only: dp, gam
  implicit none

  integer,  parameter :: Nx = 128, Ny = 128
  integer,  parameter :: ng = 2
  real(dp), parameter :: Lx = 1.0_dp, Ly = 1.0_dp
  real(dp), parameter :: t_end = 1.5_dp
  real(dp), parameter :: cfl = 0.4_dp
  real(dp), parameter :: pi = 3.141592653589793_dp

  real(dp), allocatable :: Q(:,:,:)
  real(dp) :: dx, dy, dt, t, smax
  integer :: i, j, step_count, io
  real(dp) :: xc, yc, rho, vx, vy, pr, sound, mass, ke

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
     call bc_periodic(Q, Nx, Ny, ng)
     smax = 1.0e-6_dp
     do j = 1, Ny
        do i = 1, Nx
           call prims(Q(:,i,j), rho, vx, vy, pr)
           sound = sqrt(gam*max(pr,0.0_dp)/max(rho,1.0e-12_dp))
           smax  = max(smax, abs(vx)+sound, abs(vy)+sound)
        end do
     end do
     dt = cfl*min(dx,dy)/smax
     if (t+dt > t_end) dt = t_end - t

     call sweep_x(Q, Nx, Ny, ng, 0.5_dp*dt, dx)
     call bc_periodic(Q, Nx, Ny, ng)
     call sweep_y(Q, Nx, Ny, ng, dt, dy)
     call bc_periodic(Q, Nx, Ny, ng)
     call sweep_x(Q, Nx, Ny, ng, 0.5_dp*dt, dx)

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

  ! --- Machine-checkable sanity assertions ---
  ! For the McNally KH IC the mean density is ~1.5 on [0,1]^2 (half
  ! box at rho=1, half at rho=2). Periodic BCs + conservative flux
  ! => mass is conserved to round-off. Require no NaN and <1% drift.
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

contains

  subroutine kh_init(x, y, rho, vx, vy, p)
    real(dp), intent(in)  :: x, y
    real(dp), intent(out) :: rho, vx, vy, p
    real(dp), parameter :: w = 0.025_dp
    real(dp) :: r1, r2
    r1 = 0.5_dp*(1.0_dp + tanh((y - 0.25_dp)/w))
    r2 = 0.5_dp*(1.0_dp + tanh((0.75_dp - y)/w))
    rho = 1.0_dp + r1*r2                    ! 1 outside layer, 2 inside
    vx  = -0.5_dp + r1*r2
    vy  = 0.01_dp*sin(4.0_dp*pi*x)
    p   = 2.5_dp
  end subroutine kh_init

  subroutine prims(Qv, r, vx, vy, p)
    real(dp), intent(in)  :: Qv(4)
    real(dp), intent(out) :: r, vx, vy, p
    r  = Qv(1)
    vx = Qv(2)/r
    vy = Qv(3)/r
    p  = (gam-1.0_dp)*(Qv(4) - 0.5_dp*r*(vx*vx+vy*vy))
  end subroutine prims

  subroutine flux_x(Qv, Fv)
    real(dp), intent(in)  :: Qv(4)
    real(dp), intent(out) :: Fv(4)
    real(dp) :: r,u,v,p
    call prims(Qv, r, u, v, p)
    Fv(1) = r*u
    Fv(2) = r*u*u + p
    Fv(3) = r*u*v
    Fv(4) = (Qv(4)+p)*u
  end subroutine flux_x

  subroutine flux_y(Qv, Fv)
    real(dp), intent(in)  :: Qv(4)
    real(dp), intent(out) :: Fv(4)
    real(dp) :: r,u,v,p
    call prims(Qv, r, u, v, p)
    Fv(1) = r*v
    Fv(2) = r*u*v
    Fv(3) = r*v*v + p
    Fv(4) = (Qv(4)+p)*v
  end subroutine flux_y

  subroutine rusanov_x(QL_, QR_, F)
    real(dp), intent(in)  :: QL_(4), QR_(4)
    real(dp), intent(out) :: F(4)
    real(dp) :: r_l, u_l, v_l, p_l, r_r, u_r, v_r, p_r, a_l, a_r, amax
    real(dp) :: FL_(4), FR_(4)
    call prims(QL_, r_l, u_l, v_l, p_l)
    call prims(QR_, r_r, u_r, v_r, p_r)
    a_l  = sqrt(gam*max(p_l,0.0_dp)/max(r_l,1.0e-12_dp))
    a_r  = sqrt(gam*max(p_r,0.0_dp)/max(r_r,1.0e-12_dp))
    amax = max(abs(u_l)+a_l, abs(u_r)+a_r)
    call flux_x(QL_, FL_)
    call flux_x(QR_, FR_)
    F = 0.5_dp*(FL_ + FR_) - 0.5_dp*amax*(QR_ - QL_)
  end subroutine rusanov_x

  subroutine rusanov_y(QL_, QR_, F)
    real(dp), intent(in)  :: QL_(4), QR_(4)
    real(dp), intent(out) :: F(4)
    real(dp) :: r_l, u_l, v_l, p_l, r_r, u_r, v_r, p_r, a_l, a_r, amax
    real(dp) :: FL_(4), FR_(4)
    call prims(QL_, r_l, u_l, v_l, p_l)
    call prims(QR_, r_r, u_r, v_r, p_r)
    a_l  = sqrt(gam*max(p_l,0.0_dp)/max(r_l,1.0e-12_dp))
    a_r  = sqrt(gam*max(p_r,0.0_dp)/max(r_r,1.0e-12_dp))
    amax = max(abs(v_l)+a_l, abs(v_r)+a_r)
    call flux_y(QL_, FL_)
    call flux_y(QR_, FR_)
    F = 0.5_dp*(FL_ + FR_) - 0.5_dp*amax*(QR_ - QL_)
  end subroutine rusanov_y

  subroutine sweep_x(Q, Nx, Ny, ng, dt, dx)
    integer,  intent(in) :: Nx, Ny, ng
    real(dp), intent(inout) :: Q(:, 1-ng:, 1-ng:)
    real(dp), intent(in) :: dt, dx
    real(dp) :: F(4, 0:Nx)
    integer :: i, j
    do j = 1, Ny
       do i = 0, Nx
          call rusanov_x(Q(:,i,j), Q(:,i+1,j), F(:,i))
       end do
       do i = 1, Nx
          Q(:,i,j) = Q(:,i,j) - dt/dx*(F(:,i) - F(:,i-1))
       end do
    end do
  end subroutine sweep_x

  subroutine sweep_y(Q, Nx, Ny, ng, dt, dy)
    integer,  intent(in) :: Nx, Ny, ng
    real(dp), intent(inout) :: Q(:, 1-ng:, 1-ng:)
    real(dp), intent(in) :: dt, dy
    real(dp) :: F(4, 0:Ny)
    integer :: i, j
    do i = 1, Nx
       do j = 0, Ny
          call rusanov_y(Q(:,i,j), Q(:,i,j+1), F(:,j))
       end do
       do j = 1, Ny
          Q(:,i,j) = Q(:,i,j) - dt/dy*(F(:,j) - F(:,j-1))
       end do
    end do
  end subroutine sweep_y

  subroutine bc_periodic(Q, Nx, Ny, ng)
    integer,  intent(in) :: Nx, Ny, ng
    real(dp), intent(inout) :: Q(:, 1-ng:, 1-ng:)
    integer :: g
    do g = 1, ng
       Q(:, 1-g, :)  = Q(:, Nx-g+1, :)
       Q(:, Nx+g, :) = Q(:, g, :)
    end do
    do g = 1, ng
       Q(:, :, 1-g)  = Q(:, :, Ny-g+1)
       Q(:, :, Ny+g) = Q(:, :, g)
    end do
  end subroutine bc_periodic

end program kh2d
