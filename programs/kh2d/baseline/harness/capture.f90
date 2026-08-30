program capture
  use iso_fortran_env, only: int32, real64
  use euler_module, only: dp, gam, kh_init
  use kh2d_module, only: kh2d_step
  use npy_io, only: npy_save
  implicit none

  integer(int32) :: Nx, Ny, ng
  real(dp) :: Lx, Ly, t_end, cfl
  character(len=1024) :: outdir, case_dir, arg
  integer :: nargs, u
  real(dp), allocatable :: Q(:,:,:)
  real(dp) :: dx, dy, dt, dt_max, t
  real(dp) :: xc, yc, rho, vx, vy, pr
  integer :: i, j, step_count, case_count, capture_interval

  nargs = command_argument_count()
  if (nargs < 1) then
     write(*,*) "Usage: capture [Nx Ny t_end cfl] <outdir>"
     stop 1
  end if

  ! Default parameters
  Nx = 64
  Ny = 64
  t_end = 0.1_dp
  cfl = 0.4_dp
  Lx = 1.0_dp
  Ly = 1.0_dp
  ng = 2

  if (nargs == 1) then
     call get_command_argument(1, outdir)
  else if (nargs >= 4) then
     call get_command_argument(1, arg); read(arg, *) Nx
     call get_command_argument(2, arg); read(arg, *) Ny
     call get_command_argument(3, arg); read(arg, *) t_end
     if (nargs >= 5) then
        call get_command_argument(4, arg); read(arg, *) cfl
        call get_command_argument(5, outdir)
     else
        call get_command_argument(4, outdir)
     end if
  else
     call get_command_argument(nargs, outdir)
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
  case_count = 0
  capture_interval = 3

  do while (t < t_end)
     dt_max = t_end - t
     if (case_count < 10 .and. mod(step_count, capture_interval) == 0) then
        write(case_dir, '(A, "/case", I4.4)') trim(outdir), case_count
        call execute_command_line("mkdir -p " // trim(case_dir))

        open(newunit=u, file=trim(case_dir)//'/case.json', status='replace', action='write')
        write(u, '(A)') '{"inputs": ["Q", "Nx", "Ny", "ng", "dx", "dy", "cfl", "dt_max"], "outputs": ["Q", "dt"]}'
        close(u)

        call npy_save(trim(case_dir)//'/Q.npy', Q)
        call npy_save(trim(case_dir)//'/Nx.npy', Nx)
        call npy_save(trim(case_dir)//'/Ny.npy', Ny)
        call npy_save(trim(case_dir)//'/ng.npy', ng)
        call npy_save(trim(case_dir)//'/dx.npy', dx)
        call npy_save(trim(case_dir)//'/dy.npy', dy)
        call npy_save(trim(case_dir)//'/cfl.npy', cfl)
        call npy_save(trim(case_dir)//'/dt_max.npy', dt_max)

        call kh2d_step(Q, Nx, Ny, ng, dx, dy, cfl, dt_max, dt)

        call npy_save(trim(case_dir)//'/Q.out.npy', Q)
        call npy_save(trim(case_dir)//'/dt.out.npy', dt)

        case_count = case_count + 1
     else
        call kh2d_step(Q, Nx, Ny, ng, dx, dy, cfl, dt_max, dt)
     end if
     t = t + dt
     step_count = step_count + 1
  end do

  print *, "Captured", case_count, "cases in", trim(outdir)

end program capture
