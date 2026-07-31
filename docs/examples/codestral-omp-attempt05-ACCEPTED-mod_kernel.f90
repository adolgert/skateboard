 module mod_kernel
       use iso_fortran_env, only: int32, real32
       use mod_params, only: g, dx, dt, hmean
       implicit none

       private
       public :: step

     contains

       subroutine step(h, u)
         real(real32), intent(inout) :: h(:), u(:)
         integer(int32) :: n, i
         real(real32), allocatable :: du_dx(:), dh_dx(:), flux(:)

         n = size(h)
         allocate(du_dx(n), dh_dx(n), flux(n))

         !$omp target data map(tofrom: h, u) map(alloc: du_dx, dh_dx, flux)

         ! momentum calculation
         !$omp target teams distribute parallel do
         do i = 1, n
            if (i == 1) then
               du_dx(i) = (u(2) - u(n)) / (2.0*dx)
               dh_dx(i) = (h(2) - h(n)) / (2.0*dx)
            else if (i == n) then
               du_dx(i) = (u(1) - u(n-1)) / (2.0*dx)
               dh_dx(i) = (h(1) - h(n-1)) / (2.0*dx)
            else
               du_dx(i) = (u(i+1) - u(i-1)) / (2.0*dx)
               dh_dx(i) = (h(i+1) - h(i-1)) / (2.0*dx)
            end if
         end do
         !$omp end target teams distribute parallel do

         !$omp target teams distribute parallel do
         do i = 1, n
            u(i) = u(i) - (u(i) * du_dx(i) + g * dh_dx(i)) * dt
         end do
         !$omp end target teams distribute parallel do

         ! continuity calculation
         !$omp target teams distribute parallel do
         do i = 1, n
            flux(i) = u(i) * (hmean + h(i))
         end do
         !$omp end target teams distribute parallel do

         !$omp target teams distribute parallel do
         do i = 1, n
            if (i == 1) then
               dh_dx(i) = (flux(2) - flux(n)) / (2.0*dx)
            else if (i == n) then
               dh_dx(i) = (flux(1) - flux(n-1)) / (2.0*dx)
            else
               dh_dx(i) = (flux(i+1) - flux(i-1)) / (2.0*dx)
            end if
         end do
         !$omp end target teams distribute parallel do

         !$omp target teams distribute parallel do
         do i = 1, n
            h(i) = h(i) - dh_dx(i) * dt
         end do
         !$omp end target teams distribute parallel do

         !$omp end target data

         deallocate(du_dx, dh_dx, flux)
       end subroutine step

     end module mod_kernel
