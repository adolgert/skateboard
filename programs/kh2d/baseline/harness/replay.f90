program replay
  use iso_fortran_env, only: int32, real64
  use euler_module, only: dp
  use kh2d_module, only: kh2d_step
  use npy_io, only: npy_load, npy_save
  implicit none

  character(len=1024) :: case_dir
  integer(int32) :: Nx, Ny, ng
  real(dp) :: dx, dy, cfl, dt_max, dt
  real(dp), allocatable :: Q(:,:,:)

  if (command_argument_count() < 1) then
     write(*,*) "Usage: replay <case_dir>"
     stop 1
  end if

  call get_command_argument(1, case_dir)

  call npy_load(trim(case_dir)//'/Nx.npy', Nx)
  call npy_load(trim(case_dir)//'/Ny.npy', Ny)
  call npy_load(trim(case_dir)//'/ng.npy', ng)
  call npy_load(trim(case_dir)//'/dx.npy', dx)
  call npy_load(trim(case_dir)//'/dy.npy', dy)
  call npy_load(trim(case_dir)//'/cfl.npy', cfl)
  call npy_load(trim(case_dir)//'/dt_max.npy', dt_max)
  call npy_load(trim(case_dir)//'/Q.npy', Q)

  call kh2d_step(Q, Nx, Ny, ng, dx, dy, cfl, dt_max, dt)

  call npy_save(trim(case_dir)//'/Q.out.npy', Q)
  call npy_save(trim(case_dir)//'/dt.out.npy', dt)

end program replay
