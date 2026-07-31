    module mod_kernel
        use iso_fortran_env, only: int32, real32
        use mod_params, only: g, dx, dt, hmean
        implicit none

        private
        public :: step

    contains

        subroutine step(h, u)
            real(real32), intent(inout) :: h(:), u(:)
            integer(int32) :: i, n
            real(real32) :: temp_u(size(u)), temp_h(size(h))
            n = size(u)

            ! Momentum update: uses old u and old h, store results in temp_u
            do concurrent (i = 1:n)
                if (i == 1) then
                    temp_u(i) = u(i) - ((u(2) - u(n))*u(i) + g*(h(2) - h(n))) / dx * dt
                else if (i == n) then
                    temp_u(i) = u(i) - ((u(1) - u(n-1))*u(i) + g*(h(1) - h(n-1))) / dx * dt
                else
                    temp_u(i) = u(i) - ((u(i+1) - u(i-1))*0.5*u(i) + g*(h(i+1) - h(i-1))*0.5) / dx * dt
                end if
            end do

            ! Copy temporary array back to original u array
            u = temp_u

            ! Continuity update: uses the just-updated u and old h, store results in temp_h
            do concurrent (i = 1:n)
                if (i == 1) then
                    temp_h(i) = h(i) - (u(i)*((hmean + h(2)) - (hmean + h(n))) * 0.5) / dx * dt
                else if (i == n) then
                    temp_h(i) = h(i) - (u(i)*((hmean + h(1)) - (hmean + h(n-1))) * 0.5) / dx * dt
                else
                    temp_h(i) = h(i) - (u(i)*((hmean + h(i+1)) - (hmean + h(i-1))) * 0.5) / dx * dt
                end if
            end do

            ! Copy temporary array back to original h array
            h = temp_h
        end subroutine step

    end module mod_kernel
    
