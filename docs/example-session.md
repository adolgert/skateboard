**User**
```
Read src/mod_kernel.f90. Write notes/regions/ch04-step.sese.yaml describing the region ch04:step: the anchor file, the entry symbol step, and pst_node as step@<first>-<last> where the lines are the inclusive body of the step subroutine, from subroutine step through end subroutine step. Then call submit, then sese_check.
```
**Pi agent**
```
 notes/regions/ch04-step.sese.yaml has been created with the SESE region definition for ch04:step:
 ```yaml
   region: ch04:step
   anchor:
     file: src/mod_kernel.f90
     entry: step
     pst_node: step@34-43
 ```
 Submitted the working copy and verified with sese_check:
 - sese/verified: pass (c-0001)
```
**User**
```
Nice.  Port the step subroutine to run on the GPU. The strategy compiles with -stdpar=gpu, so use do concurrent. Compile locally with nvfortran first to check it builds. When it builds, submit, then run build_replay, run_replay, sanitize, regression_visible, regression_holdout, and time_port in that order.
```
**Pi agent**
```
 The step subroutine in src/mod_kernel.f90 has been ported to execute on the GPU using do concurrent loops matching the semantic contract.

 ### Summary of Pipeline Results

 1. Local Build: Compiled and tested locally with nvfortran -stdpar=gpu.
 2. submit: Submitted working copy (tree 0fdc6fe69f3ff4788f86beac718d125d43ae0572bba44e99dbb62f64a6bd1dff).
 3. build_replay: pass (c-0002)
 4. run_replay: pass (c-0003)
 5. sanitize:
     - sanitize/memcheck: pass (c-0004)
     - sanitize/racecheck: pass (c-0005)
     - sanitize/initcheck: pass (c-0006)
 6. regression_visible: pass (c-0007)
 7. regression_holdout: pass (c-0008)
 8. time_port: pass (c-0009)

 Status: ACCEPTED
 ```
