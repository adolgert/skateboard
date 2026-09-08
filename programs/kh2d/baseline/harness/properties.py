"""Invariants of this code's region, checked by searching for inputs that break them.

Trust role: this file states what must be true of the region for any
input, and a port that breaks one of them is refused. The capture-replay
checks around it compare a port against recorded answers, which says a
port is right on the inputs someone happened to capture; a property here
says what the code is supposed to do. So a property written too loosely
makes a port look searched when it was not.

It runs inside the builder, against the replay binary built from the
submitted tree. `harness_properties` is the builder's own library, not
part of this code: it turns "run the region on these arrays" into an
invocation of that binary, and holds the draw from the captured cases
and the properties every code shares. Nothing here names a path, a
binary, or a seed.

What this code adds of its own is still to be written: the two-dimensional
shear layer has invariants worth stating -- what a step does to the total
mass and to the kinetic energy of a periodic domain -- and until they are
written down, only the property below is asked.
"""
import harness_properties as harness

# The region is a function of its inputs and nothing else. Drawn from the
# captured states, because a Kelvin-Helmholtz state is not any array: it
# is a density and a velocity field the code's own time step is stable
# for.
test_the_region_is_a_function_of_its_inputs = harness.determinism_property()
