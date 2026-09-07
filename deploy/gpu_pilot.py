#!/usr/bin/env python3
"""Exercise a fresh application through real gateway and disposable GPU jobs.

Run from the repository root with ``.venv/bin/python deploy/gpu_pilot.py``.
Docker and an NVIDIA GPU are required. State is retained under deploy/state;
each invocation needs an unused --state directory. No existing deployment is
reconfigured. The driver is an operator/client, and only gateway receipts count
as application evidence. Promotion runs in the exact gateway image.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from equivalent.manifest.layout import in_tree_manifest_text

ONBOARD = ('manifest_check', 'harness_build', 'harness_capture', 'harness_replay',
           'harness_determinism', 'harness_timing', 'harness_original',
           'harness_self_check', 'harness_property')
PORT = ('sese_check', 'build_replay', 'run_replay', 'sanitize',
        'regression_visible', 'property_check', 'regression_holdout',
        'time_baseline', 'program_regression', 'time_port', 'performance_check')


def command(args, *, log=None, check=True):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if log:
        Path(log).write_text(result.stdout + result.stderr)
    if check and result.returncode:
        raise RuntimeError(f"{args[0:4]} failed: {(result.stdout + result.stderr)[-6000:]}")
    return result.stdout


class PilotClient:
    """Use the installed public client from the agent's isolated network."""

    def __init__(self, pilot):
        self.pilot = pilot

    def _call(self, method, *args, **kwargs):
        code = (
            'import json,os,sys; from equivalent.client import connect; '
            'c=connect("http://gateway:8000",os.environ["EQUIVALENT_TOKEN"],'
            'os.environ["PILOT_SESSION"],"scripted-pilot"); '
            'print(json.dumps(getattr(c,sys.argv[1])(*json.loads(sys.argv[2]),**json.loads(sys.argv[3]))))'
        )
        return json.loads(self.pilot.docker('exec', self.pilot.client_container, 'python3', '-c', code,
                                           method, json.dumps(args), json.dumps(kwargs)))

    def submit(self, region):
        return self._call('submit', region)

    def run(self, action, region, config=None, **kwargs):
        return self._call('run', action, region, config, **kwargs)

    def status(self, region):
        return self._call('status', region)

    def table(self, region):
        return self._call('table', region)


class Pilot:
    def __init__(self, state):
        self.state = state.resolve()
        if self.state.exists():
            raise ValueError(f"{state} already exists; use a fresh --state directory")
        if not self.state.is_relative_to(ROOT / 'deploy/state'):
            raise ValueError('--state must be beneath this repository\'s deploy/state')
        self.state.mkdir(parents=True)
        self.prefix = 'skateboard-pilot-' + secrets.token_hex(4)
        self.token = secrets.token_hex(32)
        self.containers = []
        self.networks = []
        self.builder_image = self.prefix + '-builder'
        self.gateway_image = self.prefix + '-gateway'
        self.oracle_image = self.prefix + '-oracle'
        self.events = []

    def docker(self, *args, **kwargs):
        return command(['docker', *args], **kwargs)

    def mount(self, source, destination, readonly=False):
        return ['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if readonly else '')]

    def run_container(self, name, args):
        actual = self.prefix + '-' + name
        self.docker('run', '-d', '--name', actual, *args)
        self.containers.append(actual)
        return actual

    def stop(self, actual):
        self.docker('logs', actual, log=self.state / (actual + '.log'), check=False)
        self.docker('rm', '-f', actual, check=False)
        if actual in self.containers:
            self.containers.remove(actual)

    def close(self):
        for container in list(reversed(self.containers)):
            self.stop(container)
        for network in self.networks:
            self.docker('network', 'rm', network, check=False)

    def internal_json(self, container, url):
        code = 'import urllib.request; print(urllib.request.urlopen(' + repr(url) + ').read().decode())'
        return json.loads(self.docker('exec', container, 'python3', '-c', code))

    def build(self):
        sources = [ROOT / 'deploy/gpu_pilot.py', ROOT / 'deploy/gateway/Dockerfile',
                   ROOT / 'pyproject.toml']
        for directory in ('equivalent', 'services', 'programs/potential'):
            sources.extend(path for path in (ROOT / directory).rglob('*')
                           if path.is_file() and '__pycache__' not in path.parts
                           and not path.name.endswith('.pyc'))
        (self.state / 'source-provenance.json').write_text(json.dumps(dict(
            started_at=datetime.now(timezone.utc).isoformat(),
            base_commit=command(['git', '-C', ROOT, 'rev-parse', 'HEAD']).strip(),
            note='Working-tree inputs at image build time; hashes identify changes independently of base_commit.',
            sha256={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(set(sources))}), indent=2))
        for name, file in [('builder', 'services/builder/Dockerfile'),
                           ('gateway', 'deploy/gateway/Dockerfile')]:
            print('Building ' + name, flush=True)
            self.docker('build', '-f', ROOT / file, '-t', getattr(self, name + '_image'), ROOT,
                        log=self.state / (name + '-build.log'))
        self.jobs = self.state / 'jobs'
        self.jobs.mkdir()
        self.volume = self.prefix + '-work'
        self.docker('volume', 'create', '--driver', 'local', '--opt', 'type=none',
                    '--opt', 'o=bind', '--opt', 'device=' + str(self.jobs), self.volume)
        for name in ('agent', 'build', 'oracle'):
            network = self.prefix + '-' + name
            self.docker('network', 'create', '--internal', network)
            self.networks.append(network)
        self.builder = self.run_container('builder', [
            '--network', self.prefix + '-build', '--network-alias', 'builder', '--gpus', 'all',
            '--read-only', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
            '--cap-add', 'FOWNER', '--security-opt', 'no-new-privileges:true',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777',
            '--tmpfs', '/run:rw,noexec,nosuid,nodev,size=16m,mode=755',
            *self.mount('/var/run/docker.sock', '/var/run/docker.sock'),
            '--mount', f'type=volume,src={self.volume},dst=/work',
            '-e', 'SKATEBOARD_TOKEN=' + self.token,
            '-e', 'SKATEBOARD_JOB_IMAGE=' + self.builder_image,
            '-e', 'SKATEBOARD_WORK_VOLUME=' + self.volume,
            self.builder_image])
        print('Qualifying the actual GPU and job boundary', flush=True)
        report = self.docker('exec', self.builder, 'python3', '-m', 'services.builder.preflight', '--require-gpu', check=False)
        (self.state / 'qualification.json').write_text(report)
        report = json.loads(report)
        if not report['ok'] or not report['gpu_ready']:
            raise RuntimeError('GPU qualification failed')
        self.executor = report['isolation']['executor_identity']
        versions = {compiler: self.docker('exec', self.builder, 'sh', '-c',
                    compiler + ' --version 2>&1').strip()
                    for compiler in ('nvfortran', 'nvc++', 'nvcc', 'ptxas')}
        (self.state / 'compiler-versions.json').write_text(json.dumps(versions, indent=2))
        self.client_container = self.run_container('client', [
            '--network', self.prefix + '-agent', '-e', 'EQUIVALENT_TOKEN=' + self.token,
            '-e', 'PILOT_SESSION=' + self.prefix, '--entrypoint', 'sleep', self.gateway_image, 'infinity'])

    def configuration(self, phase, programs, seed, strategy='stdpar_managed', oracle=None):
        phase_dir = self.state / phase
        phase_dir.mkdir(exist_ok=True)
        for directory in ('repo', 'ledger', 'working'):
            (phase_dir / directory).mkdir(exist_ok=True)
        region = 'potential:' + ('onboard' if phase == 'onboarding' else 'solve')
        spec = dict(code='potential', phase='onboarding' if phase == 'onboarding' else 'porting',
                    strategy='onboarding' if phase == 'onboarding' else strategy,
                    baseline_strategy='cpu_reference', executor_identity=self.executor)
        if phase != 'onboarding':
            spec.update(spec_path='notes/regions/potential.yaml', visible_dataset='visible', oracle_identity=oracle)
        config = dict(version=1, paths=dict(repo='/repo', ledger_root='/ledger', working_copy='/working',
                      programs='/programs', strategies='/strategies', seed='/seed'),
                      codes={'potential': {'manifest': 'potential/manifest.yaml'}}, regions={region: spec})
        if phase == 'onboarding':
            config['codes']['potential']['original_reference'] = 'potential/original-reference.yaml'
        (phase_dir / 'gateway.yaml').write_text(yaml.safe_dump(config))
        self.phase_dir, self.region, self.seed = phase_dir, region, seed
        self.mounts = [*self.mount(phase_dir / 'repo', '/repo'),
                       *self.mount(phase_dir / 'ledger', '/ledger'),
                       *self.mount(phase_dir / 'working', '/working', True),
                       *self.mount(programs, '/programs', True), *self.mount(seed, '/seed', True),
                       *self.mount(phase_dir / 'gateway.yaml', '/etc/gateway.yaml', True)]
        return phase_dir

    def start_gateway(self):
        gateway = self.run_container('gateway', [
            '--network', self.prefix + '-agent', '--network-alias', 'gateway',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '-e', 'EQUIVALENT_CONFIG=/etc/gateway.yaml', '-e', 'EQUIVALENT_TOKEN=' + self.token,
            '-e', 'EQUIVALENT_BUILDER_URL=http://builder:9090',
            '-e', 'EQUIVALENT_ORACLE_URL=' + ('http://oracle:7070' if hasattr(self, 'oracle') else ''),
            *self.mounts, '--entrypoint', 'sh', self.gateway_image,
            '-c', 'sleep 3; exec equivalent-gateway'])
        for net in ('build', 'oracle'):
            self.docker('network', 'connect', self.prefix + '-' + net, gateway)
        for _ in range(60):
            try:
                self.internal_json(gateway, 'http://localhost:8000/healthz')
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError('Gateway did not become ready')
        self.client = PilotClient(self)
        return gateway

    def check(self, action, expected='pass'):
        settings = {'seed': 42, 'max_examples': 100} if action in ('harness_property', 'property_check') else {}
        result = self.client.run(action, self.region, settings, tool_call_id=f'{len(self.events):04d}-{action}')
        self.events.append(dict(phase=self.phase_dir.name, action=action, result=result))
        (self.state / 'receipts.json').write_text(json.dumps(self.events, indent=2))
        verdict = result.get('verdict')
        if verdict is None and 'claims' in result:
            verdict = 'pass' if all(c.get('verdict') == 'pass' for c in result['claims']) else 'fail'
        print(f'{self.phase_dir.name}: {action}: {verdict}', flush=True)
        if verdict != expected:
            raise RuntimeError(json.dumps(result, indent=2)[-12000:])
        return result

    def submit(self):
        result = self.client.submit(self.region)
        for rejected in result.get('rejected', []):
            relative = rejected['path']
            original = self.seed / relative
            candidate = self.phase_dir / 'working' / relative
            # A full working copy includes frozen files. Submission reports
            # these as ignored even when they are identical to the baseline.
            if not original.is_file() or original.read_bytes() != candidate.read_bytes():
                raise RuntimeError(f'Submission rejected a changed file: {result}')
        if self.phase_dir.name == 'onboarding' and result.get('not_sent'):
            raise RuntimeError(f'Onboarding working copy omitted baseline files: {result}')
        self.events.append(dict(phase=self.phase_dir.name, action='submit', result=result))
        return result

    def onboard(self):
        template = ROOT / 'programs/potential'
        programs = self.state / 'initial-programs'
        code = programs / 'potential'
        code.mkdir(parents=True)
        shutil.copytree(template / 'original', code / 'original')
        shutil.copytree(template / 'original', code / 'baseline')
        shutil.copyfile(template / 'original-reference.yaml', code / 'original-reference.yaml')
        (code / 'manifest.yaml').write_text(yaml.safe_dump(dict(version=1, name='potential',
                                           source=dict(root='baseline', patterns=['*.f90', 'Makefile']))))
        phase = self.configuration('onboarding', programs, code / 'baseline')
        # Submission overlays the baseline; omitted paths are retained, not
        # deleted. Keep the original sources visible in the working copy so
        # promotion can require exact equality with the reviewed tree.
        shutil.copytree(code / 'baseline', phase / 'working', dirs_exist_ok=True)
        shutil.copytree(template / 'baseline', phase / 'working', dirs_exist_ok=True)
        (phase / 'working/harness/manifest.yaml').write_text(in_tree_manifest_text((template / 'manifest.yaml').read_text()))
        gateway = self.start_gateway()
        self.submit()
        for action in ONBOARD:
            self.check(action)
        status = self.client.status(self.region)
        (phase / 'status.json').write_text(json.dumps(status, indent=2))
        if not status['accepted']:
            raise RuntimeError('Onboarding did not reach ONBOARDED')
        self.promoted = self.state / 'promoted'
        self.promoted.mkdir()
        result = self.docker('run', '--rm', '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
                             *self.mounts, *self.mount(self.promoted, '/promotion'),
                             '--entrypoint', 'ledger', self.gateway_image, 'promote', '--config',
                             '/etc/gateway.yaml', '--region-id', self.region, '--programs', '/promotion')
        (phase / 'promotion.txt').write_text(result)
        self.stop(gateway)

    def start_oracle(self):
        context = self.state / 'oracle-context'
        shutil.copytree(ROOT / 'services/oracle', context / 'services/oracle', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(self.promoted / 'potential', context / 'program')
        (context / 'equivalent/capture').mkdir(parents=True)
        shutil.copyfile(ROOT / 'equivalent/capture/compare.py', context / 'equivalent/capture/compare.py')
        dockerfile = (ROOT / 'services/oracle/Dockerfile').read_text().replace('COPY programs/${CODE} /program', 'COPY program /program')
        (context / 'Dockerfile').write_text(dockerfile)
        self.docker('build', '-t', self.oracle_image, context, log=self.state / 'oracle-build.log')
        self.oracle = self.run_container('oracle', ['--network', self.prefix + '-oracle', '--network-alias', 'oracle',
                                        '--read-only', '--tmpfs', '/tmp', '-e', 'SKATEBOARD_TOKEN=' + self.token,
                                        self.oracle_image])
        for _ in range(30):
            try:
                status = self.internal_json(self.oracle, 'http://localhost:7070/healthz')
                if status.get('ready'):
                    self.oracle_identity = status['oracle_identity']
                    return
            except RuntimeError:
                pass
            time.sleep(1)
        raise RuntimeError('Oracle did not become ready')

    def port_fortran(self):
        baseline = self.promoted / 'potential/baseline'
        phase = self.configuration('fortran', self.promoted, baseline, oracle=self.oracle_identity)
        working = phase / 'working'
        shutil.copytree(baseline, working, dirs_exist_ok=True)
        (working / 'notes/regions').mkdir(parents=True)
        shutil.copyfile(ROOT / 'programs/potential/regions/potential.sese.yaml', working / 'notes/regions/potential.yaml')
        gateway = self.start_gateway()
        self.submit()
        self.check('sese_check')
        source = (working / 'src/potential.f90').read_text()
        # First attempt: an ordinary host loop. It must fail the GPU gate.
        (working / 'src/potential.f90').write_text(source.replace('do concurrent (i=1:size(x))', 'do i=1,size(x)'))
        self.submit()
        self.check('sese_check')
        self.check('build_replay')
        self.check('run_replay', 'fail')
        # Second attempt launches a GPU kernel but implements the wrong equation.
        (working / 'src/potential.f90').write_text(source.replace('0.125_real32', '0.25_real32'))
        self.submit()
        for action in PORT[:4]:
            self.check(action)
        self.check('regression_visible', 'fail')
        # Restore the intended equation and collect the complete evidence chain.
        (working / 'src/potential.f90').write_text(source)
        self.submit()
        for action in PORT:
            self.check(action)
        status = self.client.status(self.region)
        (phase / 'status.json').write_text(json.dumps(status, indent=2))
        if not status['accepted']:
            raise RuntimeError('Fortran port did not reach ACCEPTED')
        self.stop(gateway)

    def port_mixed(self, variant):
        baseline = self.promoted / 'potential/baseline'
        phase = self.configuration(variant, self.promoted, baseline,
                                   strategy='fortran_' + variant, oracle=self.oracle_identity)
        working = phase / 'working'
        shutil.copytree(baseline, working, dirs_exist_ok=True)
        (working / 'notes/regions').mkdir(parents=True)
        suffixes = ['cu'] if variant == 'cuda' else ['cpp', 'ptx']
        foreign = ['src/potential.' + suffix for suffix in suffixes]
        bootstrap = yaml.safe_load((ROOT / 'programs/potential/regions/potential.sese.yaml').read_text())
        bootstrap['files'].extend(foreign)
        bootstrap['opaque_sources'] = foreign
        (working / 'notes/regions/potential.yaml').write_text(yaml.safe_dump(bootstrap))
        gateway = self.start_gateway()
        self.submit()
        self.check('sese_check')
        for suffix in ['f90', *suffixes]:
            shutil.copyfile(ROOT / ('programs/potential/ports/potential.' + suffix),
                            working / ('src/potential.' + suffix))
        spec = dict(region='potential:solve', files=['src/potential.f90', *foreign],
                    opaque_sources=foreign, anchor=dict(file='src/potential.f90',
                    pst_node='potential@13-17', entry_symbol='potential'))
        (working / 'notes/regions/potential.yaml').write_text(yaml.safe_dump(spec))
        self.submit()
        built = None
        for action in PORT:
            result = self.check(action)
            if action == 'build_replay':
                built = result
        status = self.client.status(self.region)
        (phase / 'status.json').write_text(json.dumps(status, indent=2))
        if not status['accepted']:
            raise RuntimeError(variant + ' port did not reach ACCEPTED')
        if variant == 'ptx':
            # Operator-side fault injection into this isolated pilot only.
            # The executable stays unchanged; changing its module must retire
            # acceptance. Restore exact bytes afterward, preserving the run.
            attempt = built['detail']['attempt_id']
            module = '/work/' + attempt + '/tree/potential.cubin'
            original = self.docker('exec', self.builder, 'python3', '-c',
                'import base64,pathlib; print(base64.b64encode(pathlib.Path(' + repr(module) + ').read_bytes()).decode())').strip()
            try:
                self.docker('exec', self.builder, 'python3', '-c',
                    'from pathlib import Path; p=Path(' + repr(module) + '); b=p.read_bytes(); p.write_bytes(b[:-1]+bytes([b[-1]^1]))')
                changed = self.client.status(self.region)
                if changed['accepted']:
                    raise RuntimeError('Changing only the GPU module did not invalidate acceptance')
            finally:
                self.docker('exec', self.builder, 'python3', '-c',
                    'import base64,pathlib; pathlib.Path(' + repr(module) + ').write_bytes(base64.b64decode(' + repr(original) + '))')
            restored = self.client.status(self.region)
            (phase / 'module-tamper.json').write_text(json.dumps(dict(
                changed_module_status=changed, restored_module_status=restored), indent=2))
            if not restored['accepted']:
                raise RuntimeError('Restoring the exact GPU module bytes did not restore valid status')
            print('ptx: altered-module rejection and exact-byte restoration: pass', flush=True)
        self.stop(gateway)

    def run(self):
        self.build()
        self.onboard()
        self.start_oracle()
        self.port_fortran()
        self.port_mixed('cuda')
        self.port_mixed('ptx')
        performance = {
            event['phase']: event['result']['detail'] for event in self.events
            if event['action'] == 'performance_check'
        }
        images = {name: self.docker('image', 'inspect', '--format', '{{.Id}}', image).strip()
                  for name, image in [('builder', self.builder_image), ('gateway', self.gateway_image),
                                      ('oracle', self.oracle_image)]}
        (self.state / 'completed.json').write_text(json.dumps(dict(prefix=self.prefix,
            executor_identity=self.executor, oracle_identity=self.oracle_identity,
            images=images, platform=platform.platform(), logical_cpus=os.cpu_count(),
            gpu=self.docker('exec', self.builder, 'nvidia-smi', '--query-gpu=name,driver_version',
                            '--format=csv,noheader').strip(),
            performance=performance), indent=2))
        print('All three implementations reached ACCEPTED; records: ' + str(self.state), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT / 'deploy/state/potential-gpu')
    args = parser.parse_args()
    pilot = Pilot(args.state)
    print('Retaining experiment state in ' + str(pilot.state), flush=True)
    try:
        pilot.run()
    finally:
        pilot.close()


if __name__ == '__main__':
    main()
