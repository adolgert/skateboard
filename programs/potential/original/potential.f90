module potential_module
  use iso_fortran_env, only: real32
  implicit none
contains
  subroutine potential(x, y, z, q, phi)
    real(real32), intent(in) :: x(:), y(:), z(:), q(:)
    real(real32), intent(out) :: phi(:)
    integer :: i
    do concurrent (i=1:size(x))
      phi(i) = point_potential(x,y,z,q,i)
    end do
  end subroutine potential
  pure function point_potential(x,y,z,q,i) result(total)
    real(real32), intent(in) :: x(:),y(:),z(:),q(:)
    integer, intent(in) :: i
    integer :: j
    real(real32) :: total, dx, dy, dz
    total = 0.0_real32
    do j=1,size(x)
      dx = x(i)-x(j)
      dy = y(i)-y(j)
      dz = z(i)-z(j)
      total = total + q(j)/sqrt(dx*dx + dy*dy + dz*dz + 0.125_real32)
    end do
  end function point_potential
end module potential_module
