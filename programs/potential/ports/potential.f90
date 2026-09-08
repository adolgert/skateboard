module potential_module
  use iso_c_binding, only: c_int, c_float
  implicit none
  interface
    subroutine potential_accelerated(n,x,y,z,q,phi) bind(C)
      import c_int, c_float
      integer(c_int), value :: n
      real(c_float), intent(in) :: x(*),y(*),z(*),q(*)
      real(c_float), intent(out) :: phi(*)
    end subroutine potential_accelerated
  end interface
contains
  subroutine potential(x,y,z,q,phi)
    real(c_float), intent(in), contiguous :: x(:),y(:),z(:),q(:)
    real(c_float), intent(out), contiguous :: phi(:)
    call potential_accelerated(int(size(x),c_int),x,y,z,q,phi)
  end subroutine potential
end module potential_module
