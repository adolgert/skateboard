program charged_cloud
  use iso_fortran_env, only: real32
  use potential_module, only: potential
  use initial_module, only: initial
  use npy_io, only: npy_save
  implicit none
  real(real32), allocatable :: x(:), y(:), z(:), q(:), phi(:)
  integer :: n, shift, steps, step, unit
  character(len=128) :: arg
  call get_command_argument(1,arg)
  read(arg,*) n
  call get_command_argument(2,arg)
  read(arg,*) shift
  call get_command_argument(3,arg)
  read(arg,*) steps
  allocate(x(n),y(n),z(n),q(n),phi(n))
  call initial(n,shift,x,y,z,q)
  do step=1,steps
    call potential(x,y,z,q,phi)
    ! Feed each answer into the next configuration; every solve is needed.
    x = x + phi*0.000001_real32
  end do
  open(newunit=unit,file='phi.bin',access='stream',form='unformatted',status='replace')
  write(unit) phi
  close(unit)
  call npy_save("phi.npy",phi)
end program charged_cloud
