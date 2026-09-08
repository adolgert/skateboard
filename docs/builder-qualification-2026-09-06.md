# Builder qualification — 2026-09-06

This is the CPU boundary qualification run for the current uncommitted review
tree, based on commit `20a4d901ee67940c30d7a129139c8665a75a071a`. The image
digest, rather than that base commit, identifies the exact files exercised.

| field | observed value |
| --- | --- |
| local time | 2026-09-06 21:28 EDT |
| host | Linux 7.0.9-76070009-generic, x86_64 |
| Docker server | 29.1.3 |
| builder image | `sha256:9d13c7f0422bfb7d902ba6519600ce9adcb8b247aa07a0787143269730abd234` |
| executor identity | `b60c208f09c8db999090ef86ca59bb43c4b1e3fd5daac1705915e5820bf95a69` |
| CPU executable | `sha256:1e05db911ab99ebc30ff8a7a66c1b8fd8d277840d58341add1da05b504aa58c2`, 17,568 bytes |
| CPU boundary | qualified |
| GPU boundary | unavailable; no NVIDIA driver was visible |

The command was the CPU-only form documented in `deploy/README.md`, using the
deployment's isolated work volume:

```sh
./deploy/qualify.sh --cpu-only
```

Every CPU check passed:

- A real `nvfortran -O1 -o replay src/main.f90` build completed in a disposable
  job. Protected `strace/execve` evidence observed one compiler invocation and
  nine total executions. The declared flag reached the compile, and the only
  compiled Fortran input matched `src/*.f90` in the submitted tree.
- The submitted makefile tried to replace `/run/evidence/result.json`; the job
  user received `Permission denied`, and the protected observer supplied the
  accepted record.
- The bound executable ran in a later read-only job and printed
  `skateboard-cpu-job-ok`. Its digest and executor identity matched the build
  record.
- Service credentials and network routes were absent. The attempt appeared only
  under `/job`; the image's empty `/work` directory exposed no volume content.
- The submitted tree, `/opt/harness`, and the container root were read-only.
- A command received only its selected case scratch directory. A sibling case
  and a different attempt remained invisible.
- Containers and descendants were removed after normal completion and after a
  forced timeout.
- `nsys --version` succeeded as uid 65532 in an isolated job. No protected GPU
  profile, kernel launch, `memcheck`, `racecheck`, `initcheck`, correctness, or
  timing qualification was attempted because the NVIDIA driver was unavailable.
  On a capable host, the full command requires an actual profiled OpenACC kernel
  and clean results from all three sanitizer modes; their runtime and capability
  requirements therefore remain unconfirmed by this CPU record.

The protected exec trace establishes that the configured compiler ran with the
recorded flags and sources. It does not prove that every byte in the final
executable derives exclusively from those compiler outputs. GPU qualification
must be run on the target NVIDIA host with `deploy/qualify.sh` before this system
is used to make GPU execution, sanitizer, or performance claims.
