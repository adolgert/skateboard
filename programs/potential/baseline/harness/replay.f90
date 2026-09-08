program replay
  use iso_fortran_env, only: real32
  use potential_module, only: potential
  use npy_io, only: npy_load, npy_save
  implicit none
  real(real32), allocatable :: x(:),y(:),z(:),q(:),phi(:)
  character(len=1024) :: directory
  call get_command_argument(1,directory)
  call npy_load(trim(directory)//'/x.npy',x)
  call npy_load(trim(directory)//'/y.npy',y)
  call npy_load(trim(directory)//'/z.npy',z)
  call npy_load(trim(directory)//'/q.npy',q)
  allocate(phi(size(x)))
  call potential(x,y,z,q,phi)
  call npy_save(trim(directory)//'/phi.out.npy',phi)
end program replay
