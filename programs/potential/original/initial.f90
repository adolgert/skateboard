module initial_module
  use iso_fortran_env, only: real32
  implicit none
contains
  subroutine initial(n, shift, x, y, z, q)
    integer, intent(in) :: n, shift
    real(real32), intent(out) :: x(n), y(n), z(n), q(n)
    integer :: i
    do i=1,n
      x(i) = real(mod(i*13+shift,101),real32)/101.0_real32
      y(i) = real(mod(i*17+shift,103),real32)/103.0_real32
      z(i) = real(mod(i*19+shift,107),real32)/107.0_real32
      q(i) = real(mod(i+shift,7)-3,real32)/7.0_real32
    end do
  end subroutine initial
end module initial_module
