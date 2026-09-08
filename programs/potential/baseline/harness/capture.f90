program capture
  use iso_fortran_env, only: real32
  use potential_module, only: potential
  use initial_module, only: initial
  use npy_io, only: npy_save
  implicit none
  real(real32), allocatable :: x(:),y(:),z(:),q(:),phi(:)
  integer :: n, shift, k, unit
  character(len=1024) :: arg, directory, case_dir
  call get_command_argument(1,arg)
  read(arg,*) n
  call get_command_argument(2,arg)
  read(arg,*) shift
  call get_command_argument(3,directory)
  allocate(x(n),y(n),z(n),q(n),phi(n))
  call initial(n,shift,x,y,z,q)
  do k=1,2
    write(case_dir,'(a,"/case",i1)') trim(directory),k
    call execute_command_line('mkdir -p '//trim(case_dir))
    call npy_save(trim(case_dir)//'/x.npy',x)
    call npy_save(trim(case_dir)//'/y.npy',y)
    call npy_save(trim(case_dir)//'/z.npy',z)
    call npy_save(trim(case_dir)//'/q.npy',q)
    call potential(x,y,z,q,phi)
    call npy_save(trim(case_dir)//'/phi.out.npy',phi)
    open(newunit=unit,file=trim(case_dir)//'/case.json',status='replace')
    write(unit,'(a)') '{"inputs":["x","y","z","q"],"outputs":["phi"]}'
    close(unit)
    x = x + phi*0.000001_real32
  end do
end program capture
