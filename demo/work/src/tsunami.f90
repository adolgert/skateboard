program tsunami

  ! Non-linear 1-D shallow water driver at a LARGE problem size, so the GPU port
  ! has enough work to beat the CPU. The domain is `num_tiles` copies of the
  ! original 100-point ch04 domain laid end to end; with the periodic boundary
  ! each tile evolves identically, so the answer per tile is exactly the ch04
  ! answer and `num_tiles` is a pure size knob (technique from codes/tsunami/
  ! src/ch04_large). This is the end-to-end run the honest timing gate measures.
  !
  ! The per-step update lives in mod_kernel::step (the port target), so the same
  ! ported kernel that passes the size-100 capture-replay correctness tests is
  ! what runs here at scale.
  !
  ! num_tiles may be overridden by an optional first command-line argument.

  use iso_fortran_env, only: int32, real32
  use mod_initial, only: set_gaussian
  use mod_kernel, only: step

  implicit none

  integer(int32) :: n, itile, num_tiles, grid_size
  integer(int32), parameter :: tile_size = 100       ! grid points per tile (one ch04 domain)
  integer(int32), parameter :: num_time_steps = 5000
  integer(int32), parameter :: icenter = 25
  real(real32),   parameter :: decay = 0.02
  integer(int32), parameter :: default_tiles = 20000 ! 2,000,000 points

  real(real32), allocatable :: h(:), u(:)
  character(len=32) :: arg

  num_tiles = default_tiles
  if (command_argument_count() >= 1) then
    call get_command_argument(1, arg)
    read(arg, *) num_tiles
  end if
  grid_size = tile_size * num_tiles

  allocate(h(grid_size), u(grid_size))

  ! one Gaussian blob per tile; velocity starts at rest
  do itile = 0, num_tiles - 1
    call set_gaussian(h(itile*tile_size + 1 : (itile+1)*tile_size), icenter, decay)
  end do
  u = 0

  time_loop: do n = 1, num_time_steps
    call step(h, u)
  end do time_loop

  ! mean(h) is conserved per tile; print as a cheap smoke check
  print '(a,i0,a,es15.8e2)', 'num_tiles=', num_tiles, ' mean_h=', sum(h) / grid_size

end program tsunami
