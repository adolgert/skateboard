! Small deterministic Fortran 2008 program independent of the demonstration codes.
program heat_step
  use iso_fortran_env, only: real64
  implicit none
  real(real64), allocatable :: u(:,:), v(:,:)
  real(real64) :: dx, dy
  integer :: nx, ny, i, j, step
  character(32) :: arg
  call get_command_argument(1, arg)
  read(arg, *) nx
  call get_command_argument(2, arg)
  read(arg, *) ny
  dx = 0.5_real64
  dy = 0.25_real64
  allocate(u(0:nx+1,0:ny+1), v(0:nx+1,0:ny+1))
  do j = 0, ny+1
    do i = 0, nx+1
      u(i,j) = real(i*i + 3*j*j, real64)
    end do
  end do
  do step = 1, 3
    v = u
    do j = 1, ny
      do i = 1, nx
        v(i,j) = u(i,j) + 0.001_real64 * ( &
          (u(i-1,j) - 2*u(i,j) + u(i+1,j))/dx**2 + &
          (u(i,j-1) - 2*u(i,j) + u(i,j+1))/dy**2)
      end do
    end do
    u = v
  end do
  open(unit=10, file='answer.txt', status='replace', action='write')
  do j = 1, ny
    write(10, '(*(ES25.16E3,1X))') u(1:nx,j)
  end do
  close(10)
end program heat_step
