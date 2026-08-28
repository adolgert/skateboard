module kh2d_module
  use iso_fortran_env, only: int32, real64
  use euler_module, only: dp, gam
  implicit none

  private
  public :: kh2d_step, prims, flux_x, flux_y, rusanov_x, rusanov_y, sweep_x, sweep_y, bc_periodic

contains

  subroutine kh2d_step(Q, Nx, Ny, ng, dx, dy, cfl, dt_max, dt)
    integer,  intent(in) :: Nx, Ny, ng
    real(dp), intent(in) :: dx, dy, cfl, dt_max
    real(dp), intent(inout) :: Q(4, 1-ng:Nx+ng, 1-ng:Ny+ng)
    real(dp), intent(out) :: dt

    real(dp) :: smax, rho, vx, vy, pr, sound
    integer :: i, j

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
    if (dt > dt_max) dt = dt_max

    call sweep_x(Q, Nx, Ny, ng, 0.5_dp*dt, dx)
    call bc_periodic(Q, Nx, Ny, ng)
    call sweep_y(Q, Nx, Ny, ng, dt, dy)
    call bc_periodic(Q, Nx, Ny, ng)
    call sweep_x(Q, Nx, Ny, ng, 0.5_dp*dt, dx)
  end subroutine kh2d_step

  pure subroutine prims(Qv, r, vx, vy, p)
    real(dp), intent(in)  :: Qv(4)
    real(dp), intent(out) :: r, vx, vy, p
    r  = Qv(1)
    vx = Qv(2)/r
    vy = Qv(3)/r
    p  = (gam-1.0_dp)*(Qv(4) - 0.5_dp*r*(vx*vx+vy*vy))
  end subroutine prims

  pure subroutine flux_x(Qv, Fv)
    real(dp), intent(in)  :: Qv(4)
    real(dp), intent(out) :: Fv(4)
    real(dp) :: r,u,v,p
    call prims(Qv, r, u, v, p)
    Fv(1) = r*u
    Fv(2) = r*u*u + p
    Fv(3) = r*u*v
    Fv(4) = (Qv(4)+p)*u
  end subroutine flux_x

  pure subroutine flux_y(Qv, Fv)
    real(dp), intent(in)  :: Qv(4)
    real(dp), intent(out) :: Fv(4)
    real(dp) :: r,u,v,p
    call prims(Qv, r, u, v, p)
    Fv(1) = r*v
    Fv(2) = r*u*v
    Fv(3) = r*v*v + p
    Fv(4) = (Qv(4)+p)*v
  end subroutine flux_y

  pure subroutine rusanov_x(QL_, QR_, F)
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

  pure subroutine rusanov_y(QL_, QR_, F)
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

end module kh2d_module
