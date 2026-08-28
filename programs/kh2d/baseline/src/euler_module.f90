! euler_module.f90
!
! Shared machinery for 1D compressible Euler equations:
!   - primitive <-> conservative conversion
!   - physical flux
!   - Approximate Riemann solvers: HLL, HLLC, Roe (with entropy fix)
!   - Exact Riemann solver (Toro, "Riemann Solvers and Numerical
!     Methods for Fluid Dynamics", 2009, Ch. 4)
!
! All routines assume an ideal gas with constant gamma, stored as
! a module variable `gam`.
!
! References:
!   Toro (2009), "Riemann Solvers..."
!   Roe (1981), J. Comput. Phys. 43, 357
!   Harten, Lax, van Leer (1983), SIAM Rev. 25, 35
!   Toro, Spruce, Speares (1994), Shock Waves 4, 25 (HLLC)

module euler_module
  implicit none
  integer,  parameter :: dp = kind(1.0d0)
  real(dp), parameter :: gam_default = 1.4_dp
  real(dp) :: gam = gam_default

  private
  public :: dp, gam
  public :: cons_to_prim, prim_to_cons, phys_flux
  public :: hll_flux, hllc_flux, roe_flux
  public :: exact_riemann_sample
  public :: kh_init

contains

  pure subroutine cons_to_prim(Uc, rho, vel, pr)
    real(dp), intent(in)  :: Uc(3)
    real(dp), intent(out) :: rho, vel, pr
    rho = Uc(1)
    vel = Uc(2) / rho
    pr  = (gam - 1.0_dp) * (Uc(3) - 0.5_dp*rho*vel*vel)
  end subroutine cons_to_prim

  pure subroutine prim_to_cons(rho, vel, pr, Uc)
    real(dp), intent(in)  :: rho, vel, pr
    real(dp), intent(out) :: Uc(3)
    Uc(1) = rho
    Uc(2) = rho*vel
    Uc(3) = pr/(gam-1.0_dp) + 0.5_dp*rho*vel*vel
  end subroutine prim_to_cons

  pure subroutine phys_flux(Uc, Fv)
    real(dp), intent(in)  :: Uc(3)
    real(dp), intent(out) :: Fv(3)
    real(dp) :: rho, vel, pr
    call cons_to_prim(Uc, rho, vel, pr)
    Fv(1) = rho*vel
    Fv(2) = rho*vel*vel + pr
    Fv(3) = (Uc(3) + pr)*vel
  end subroutine phys_flux

  ! -------- HLL (Harten-Lax-van Leer) --------
  pure subroutine hll_flux(UL, UR, F)
    real(dp), intent(in)  :: UL(3), UR(3)
    real(dp), intent(out) :: F(3)
    real(dp) :: rL,vL,pL, rR,vR,pR, aL,aR, sL,sR, FL(3), FR(3)
    call cons_to_prim(UL, rL, vL, pL)
    call cons_to_prim(UR, rR, vR, pR)
    aL = sqrt(gam*pL/rL)
    aR = sqrt(gam*pR/rR)
    sL = min(vL - aL, vR - aR)
    sR = max(vL + aL, vR + aR)
    call phys_flux(UL, FL)
    call phys_flux(UR, FR)
    if (sL >= 0.0_dp) then
       F = FL
    else if (sR <= 0.0_dp) then
       F = FR
    else
       F = (sR*FL - sL*FR + sL*sR*(UR - UL)) / (sR - sL)
    end if
  end subroutine hll_flux

  ! -------- HLLC (Toro-Spruce-Speares 1994) --------
  pure subroutine hllc_flux(UL, UR, F)
    real(dp), intent(in)  :: UL(3), UR(3)
    real(dp), intent(out) :: F(3)
    real(dp) :: rL,vL,pL, rR,vR,pR, aL,aR
    real(dp) :: sL, sR, sStar, pStar, rStar, factL, factR
    real(dp) :: UstarL(3), UstarR(3), FL(3), FR(3)

    call cons_to_prim(UL, rL, vL, pL)
    call cons_to_prim(UR, rR, vR, pR)
    aL = sqrt(gam*pL/rL)
    aR = sqrt(gam*pR/rR)

    ! Einfeldt-style wave speed estimates with pressure-based correction
    pStar = max(0.0_dp, 0.5_dp*(pL+pR) - 0.5_dp*(vR-vL)*0.5_dp*(rL+rR)*0.5_dp*(aL+aR))
    if (pStar <= pL) then
       factL = 1.0_dp
    else
       factL = sqrt(1.0_dp + (gam+1.0_dp)/(2.0_dp*gam)*(pStar/pL - 1.0_dp))
    end if
    if (pStar <= pR) then
       factR = 1.0_dp
    else
       factR = sqrt(1.0_dp + (gam+1.0_dp)/(2.0_dp*gam)*(pStar/pR - 1.0_dp))
    end if
    sL = vL - aL*factL
    sR = vR + aR*factR

    sStar = (pR - pL + rL*vL*(sL - vL) - rR*vR*(sR - vR)) / &
            (rL*(sL - vL) - rR*(sR - vR))

    call phys_flux(UL, FL)
    call phys_flux(UR, FR)

    if (sL >= 0.0_dp) then
       F = FL
    else if (sR <= 0.0_dp) then
       F = FR
    else if (sStar >= 0.0_dp) then
       rStar = rL*(sL - vL)/(sL - sStar)
       UstarL(1) = rStar
       UstarL(2) = rStar*sStar
       UstarL(3) = rStar*(UL(3)/rL + (sStar - vL)*(sStar + pL/(rL*(sL - vL))))
       F = FL + sL*(UstarL - UL)
    else
       rStar = rR*(sR - vR)/(sR - sStar)
       UstarR(1) = rStar
       UstarR(2) = rStar*sStar
       UstarR(3) = rStar*(UR(3)/rR + (sStar - vR)*(sStar + pR/(rR*(sR - vR))))
       F = FR + sR*(UstarR - UR)
    end if
  end subroutine hllc_flux

  ! -------- Roe with Harten-Hyman entropy fix --------
  ! Falls back to HLL on vacuum / near-vacuum states where the Roe
  ! linearization is not positivity-preserving (e.g. Toro test 2).
  pure subroutine roe_flux(UL, UR, F)
    real(dp), intent(in)  :: UL(3), UR(3)
    real(dp), intent(out) :: F(3)
    real(dp) :: rL,vL,pL, rR,vR,pR, HL, HR, sqL, sqR
    real(dp) :: rt, vt, Ht, at2, at, dU(3), FL(3), FR(3)
    real(dp) :: alpha(3), lam(3), K(3,3), eps
    integer  :: i

    call cons_to_prim(UL, rL, vL, pL)
    call cons_to_prim(UR, rR, vR, pR)
    if (pL <= 0.0_dp .or. pR <= 0.0_dp .or. rL <= 0.0_dp .or. rR <= 0.0_dp) then
       call hll_flux(UL, UR, F); return
    end if
    HL = (UL(3) + pL)/rL
    HR = (UR(3) + pR)/rR
    sqL = sqrt(rL); sqR = sqrt(rR)

    rt = sqL*sqR
    vt = (sqL*vL + sqR*vR)/(sqL + sqR)
    Ht = (sqL*HL + sqR*HR)/(sqL + sqR)
    at2 = (gam-1.0_dp)*(Ht - 0.5_dp*vt*vt)
    if (at2 <= 0.0_dp) then
       call hll_flux(UL, UR, F); return
    end if
    at = sqrt(at2)

    dU = UR - UL
    alpha(2) = (gam-1.0_dp)/(at*at) * &
               (dU(1)*(Ht - vt*vt) + vt*dU(2) - dU(3))
    alpha(1) = 1.0_dp/(2.0_dp*at) * (dU(1)*(vt + at) - dU(2) - at*alpha(2))
    alpha(3) = dU(1) - alpha(1) - alpha(2)

    lam(1) = vt - at
    lam(2) = vt
    lam(3) = vt + at

    ! Harten entropy fix
    eps = 0.1_dp*at
    do i = 1, 3
       if (abs(lam(i)) < eps) lam(i) = 0.5_dp*(lam(i)*lam(i)/eps + eps)
    end do

    K(1,1) = 1.0_dp;   K(2,1) = vt - at;  K(3,1) = Ht - vt*at
    K(1,2) = 1.0_dp;   K(2,2) = vt;       K(3,2) = 0.5_dp*vt*vt
    K(1,3) = 1.0_dp;   K(2,3) = vt + at;  K(3,3) = Ht + vt*at

    call phys_flux(UL, FL)
    call phys_flux(UR, FR)
    F = 0.5_dp*(FL + FR)
    do i = 1, 3
       F = F - 0.5_dp*abs(lam(i))*alpha(i)*K(:,i)
    end do
  end subroutine roe_flux

  ! -------- Exact Riemann sampler (Toro 2009, Ch. 4) --------
  ! Returns (rho, vel, p) at self-similar coordinate s = x/t
  subroutine exact_riemann_sample(rL,vL,pL, rR,vR,pR, s, rho, vel, p)
    real(dp), intent(in)  :: rL,vL,pL, rR,vR,pR, s
    real(dp), intent(out) :: rho, vel, p

    real(dp) :: aL, aR, pStar, vStar
    real(dp) :: cL, cR, sHL, sTL, sHR, sTR
    real(dp) :: rStarL, rStarR
    real(dp) :: pg, cs
    real(dp) :: f, fp, change, p_old, fL, fR, fLp, fRp
    integer  :: it

    aL = sqrt(gam*pL/rL)
    aR = sqrt(gam*pR/rR)

    ! Initial guess: two-rarefaction approximation
    pg = 0.5_dp*(pL+pR)
    pg = max(pg, 1.0e-8_dp)

    do it = 1, 50
       call pressure_fn(pg, rL, pL, aL, fL, fLp)
       call pressure_fn(pg, rR, pR, aR, fR, fRp)
       f  = fL + fR + (vR - vL)
       fp = fLp + fRp
       p_old = pg
       pg = pg - f/fp
       if (pg < 0.0_dp) pg = 0.5_dp*p_old
       change = 2.0_dp*abs((pg - p_old)/(pg + p_old))
       if (change < 1.0e-10_dp) exit
    end do
    pStar = pg
    vStar = 0.5_dp*(vL + vR) + 0.5_dp*(fR - fL)

    ! Sample at s = x/t
    if (s <= vStar) then
       ! left of contact
       if (pStar <= pL) then
          ! left rarefaction
          sHL = vL - aL
          cL  = aL*(pStar/pL)**((gam-1.0_dp)/(2.0_dp*gam))
          sTL = vStar - cL
          if (s <= sHL) then
             rho = rL; vel = vL; p = pL
          else if (s >= sTL) then
             rStarL = rL*(pStar/pL)**(1.0_dp/gam)
             rho = rStarL; vel = vStar; p = pStar
          else
             rho = rL*(2.0_dp/(gam+1.0_dp) + (gam-1.0_dp)/((gam+1.0_dp)*aL)*(vL - s))**(2.0_dp/(gam-1.0_dp))
             vel = 2.0_dp/(gam+1.0_dp)*(aL + (gam-1.0_dp)/2.0_dp*vL + s)
             cs  = 2.0_dp/(gam+1.0_dp)*(aL + (gam-1.0_dp)/2.0_dp*(vL - s))
             p   = pL*(cs/aL)**(2.0_dp*gam/(gam-1.0_dp))
          end if
       else
          ! left shock
          rStarL = rL*((pStar/pL + (gam-1.0_dp)/(gam+1.0_dp)) / &
                       ((gam-1.0_dp)/(gam+1.0_dp)*pStar/pL + 1.0_dp))
          sHL = vL - aL*sqrt((gam+1.0_dp)/(2.0_dp*gam)*pStar/pL + (gam-1.0_dp)/(2.0_dp*gam))
          if (s <= sHL) then
             rho = rL; vel = vL; p = pL
          else
             rho = rStarL; vel = vStar; p = pStar
          end if
       end if
    else
       ! right of contact
       if (pStar <= pR) then
          ! right rarefaction
          sHR = vR + aR
          cR  = aR*(pStar/pR)**((gam-1.0_dp)/(2.0_dp*gam))
          sTR = vStar + cR
          if (s >= sHR) then
             rho = rR; vel = vR; p = pR
          else if (s <= sTR) then
             rStarR = rR*(pStar/pR)**(1.0_dp/gam)
             rho = rStarR; vel = vStar; p = pStar
          else
             rho = rR*(2.0_dp/(gam+1.0_dp) - (gam-1.0_dp)/((gam+1.0_dp)*aR)*(vR - s))**(2.0_dp/(gam-1.0_dp))
             vel = 2.0_dp/(gam+1.0_dp)*(-aR + (gam-1.0_dp)/2.0_dp*vR + s)
             cs  = 2.0_dp/(gam+1.0_dp)*(aR - (gam-1.0_dp)/2.0_dp*(vR - s))
             p   = pR*(cs/aR)**(2.0_dp*gam/(gam-1.0_dp))
          end if
       else
          ! right shock
          rStarR = rR*((pStar/pR + (gam-1.0_dp)/(gam+1.0_dp)) / &
                       ((gam-1.0_dp)/(gam+1.0_dp)*pStar/pR + 1.0_dp))
          sHR = vR + aR*sqrt((gam+1.0_dp)/(2.0_dp*gam)*pStar/pR + (gam-1.0_dp)/(2.0_dp*gam))
          if (s >= sHR) then
             rho = rR; vel = vR; p = pR
          else
             rho = rStarR; vel = vStar; p = pStar
          end if
       end if
    end if
  end subroutine exact_riemann_sample

  pure subroutine pressure_fn(p, rk, pk, ak, f, fp)
    real(dp), intent(in)  :: p, rk, pk, ak
    real(dp), intent(out) :: f, fp
    real(dp) :: Ak_, Bk_, qk
    if (p > pk) then
       ! shock
       Ak_ = 2.0_dp/((gam+1.0_dp)*rk)
       Bk_ = (gam-1.0_dp)/(gam+1.0_dp)*pk
       qk  = sqrt(Ak_/(p + Bk_))
       f   = (p - pk)*qk
       fp  = qk*(1.0_dp - 0.5_dp*(p - pk)/(Bk_ + p))
    else
       ! rarefaction
       f  = 2.0_dp*ak/(gam-1.0_dp) * ((p/pk)**((gam-1.0_dp)/(2.0_dp*gam)) - 1.0_dp)
       fp = 1.0_dp/(rk*ak) * (p/pk)**(-(gam+1.0_dp)/(2.0_dp*gam))
    end if
  end subroutine pressure_fn

  subroutine kh_init(x, y, rho, vx, vy, p)
    real(dp), intent(in)  :: x, y
    real(dp), intent(out) :: rho, vx, vy, p
    real(dp), parameter :: w = 0.025_dp
    real(dp), parameter :: pi = 3.141592653589793_dp
    real(dp) :: r1, r2
    r1 = 0.5_dp*(1.0_dp + tanh((y - 0.25_dp)/w))
    r2 = 0.5_dp*(1.0_dp + tanh((0.75_dp - y)/w))
    rho = 1.0_dp + r1*r2                    ! 1 outside layer, 2 inside
    vx  = -0.5_dp + r1*r2
    vy  = 0.01_dp*sin(4.0_dp*pi*x)
    p   = 2.5_dp
  end subroutine kh_init

end module euler_module
