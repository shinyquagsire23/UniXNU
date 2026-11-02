#!/usr/bin/env python3
"""Fetch and prepare the pinned sources the GPU userland builds read, and nothing else: no compile.

  .cache/mesa-work                     Mesa at scripts/data/adreno-compiler-inputs.json (commit and tree checked), sparse, with
                                       every recorded patches/mesa patch applied once to a pristine checkout
  .cache/mesa-venv                     meson and Mesa's Python generators at scripts/requirements-adreno-compiler.txt
  .cache/mesa-tools/bin/bison          GNU Bison 3.8.2 from its release archive (size and sha256 checked)
  .cache/gpu-reference/metal2vulkan    the translator at scripts/data/gpu-metal-vulkan-inputs.json, fetched by SHA (it is on no
                                       branch build-adreno-metal-bridge.py could follow); that script applies its patches

The source half of UniXNU-sheng's build-adreno-compiler-host.py, without its IR3 host compiler build, plus the translator checkout.
Then: build-turnip-darwin.py --target host and --target ios, build-adreno-metal-plugin.py, make-owner-trust-bundle.py,
make-gpu-addon.py."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request
from mesa_patch_identity import validate_recorded_patches

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache'
pins = json.loads((ROOT / 'scripts/data/adreno-compiler-inputs.json').read_text())
translator = json.loads((ROOT / 'scripts/data/gpu-metal-vulkan-inputs.json').read_text())['translator']
source = CACHE / 'mesa-work'
venv = CACHE / 'mesa-venv'
tools = CACHE / 'mesa-tools'


def run(command, *, cwd=ROOT, log=None, timeout=60):
    command = [str(x) for x in command]
    if log:
        with (CACHE / log).open('w') as stream:
            result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise SystemExit(log + ':\n' + '\n'.join((CACHE / log).read_text().splitlines()[-20:]))
        return ''
    return subprocess.check_output(command, cwd=cwd, text=True, timeout=timeout).strip()


CACHE.mkdir(exist_ok=True)
if not (source / '.git').exists():
    run(['git', 'init', '-q', source])
    run(['git', '-C', source, 'remote', 'add', 'origin', pins['mesa']['mirror']])
    run(['git', '-C', source, 'fetch', '-q', '--depth=1', '--filter=blob:none', 'origin', pins['mesa']['commit']], timeout=600)
    run(['git', '-C', source, 'sparse-checkout', 'init', '--cone'])
    run(['git', '-C', source, 'sparse-checkout', 'set', *pins['mesa']['sparse_directories']], timeout=600)
    run(['git', '-C', source, 'checkout', '-q', '--detach', 'FETCH_HEAD'], timeout=600)
if run(['git', '-C', source, 'rev-parse', 'HEAD']) != pins['mesa']['commit'] or \
        run(['git', '-C', source, 'rev-parse', 'HEAD^{tree}']) != pins['mesa']['tree']:
    raise SystemExit('Pinned Mesa commit/tree differs')
# A pristine checkout receives the whole sequence once; existing work must equal all recorded patches.
clean = subprocess.run(['git', '-C', str(source), 'diff', '--quiet', 'HEAD'], capture_output=True).returncode == 0
extras = run(['git', '-C', source, 'ls-files', '--others', '--exclude-standard'])
if clean and not extras:
    for name in pins['patches']:
        run(['git', '-C', source, 'apply', ROOT / name])
validate_recorded_patches(ROOT, source, pins)
print('mesa: %s with %d patches' % (pins['mesa']['commit'][:12], len(pins['patches'])))

if not (venv / 'bin/python').exists():
    run([sys.executable, '-m', 'venv', venv])
requirements = (ROOT / 'scripts/requirements-adreno-compiler.txt').read_text().splitlines()
expected = {line.split('==')[0].lower(): line.split('==')[1] for line in requirements if line.strip()}
installed = json.loads(run([venv / 'bin/python', '-c', 'import importlib.metadata,json; print(json.dumps({d.metadata["Name"].lower():d.version for d in importlib.metadata.distributions()}))']))
if any(installed.get(name) != version for name, version in expected.items()):
    run([venv / 'bin/pip', 'install', '-r', ROOT / 'scripts/requirements-adreno-compiler.txt'], log='mesa-python-install.log', timeout=300)
print('venv: ' + ', '.join('%s %s' % item for item in sorted(expected.items())))

bison = tools / 'bin/bison'
if not bison.exists() or run([bison, '--version']).splitlines()[0] != 'bison (GNU Bison) 3.8.2':
    archive = CACHE / 'tool-sources/bison-3.8.2.tar.xz'
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists():
        with urllib.request.urlopen(pins['bison']['url'], timeout=60) as stream:
            archive.write_bytes(stream.read(pins['bison']['bytes'] + 1))
    data = archive.read_bytes()
    if len(data) != pins['bison']['bytes'] or hashlib.sha256(data).hexdigest() != pins['bison']['sha256']:
        raise SystemExit('GNU Bison release input differs')
    tool_source = archive.parent / 'bison-3.8.2'
    if not tool_source.exists():
        with tarfile.open(archive) as stream:
            stream.extractall(archive.parent, filter='data')
    tool_build = CACHE / 'tool-build/bison'
    tool_build.mkdir(parents=True, exist_ok=True)
    run([tool_source / 'configure', '--prefix=' + str(tools), '--disable-nls'], cwd=tool_build, log='bison-configure.log', timeout=600)
    run(['make', '-j4'], cwd=tool_build, log='bison-build.log', timeout=600)
    run(['make', 'install'], cwd=tool_build, log='bison-install.log', timeout=120)
print('bison: ' + run([bison, '--version']).splitlines()[0])

m2v = CACHE / 'gpu-reference/metal2vulkan'
if not (m2v / '.git').exists():
    m2v.parent.mkdir(parents=True, exist_ok=True)
    run(['git', 'init', '-q', m2v])
    run(['git', '-C', m2v, 'remote', 'add', 'origin', translator['repository']])
    run(['git', '-C', m2v, 'fetch', '-q', '--depth=1', '--filter=blob:none', 'origin', translator['commit']], timeout=600)
    run(['git', '-C', m2v, 'checkout', '-q', '--detach', 'FETCH_HEAD'], timeout=600)
if run(['git', '-C', m2v, 'rev-parse', 'HEAD']) != translator['commit']:
    raise SystemExit('Translator checkout differs from its pin')
for name, digest in translator.get('files', {}).items():
    # build-adreno-metal-bridge.py patches the source in place; the recorded digests are of the pinned revision.
    pinned = subprocess.check_output(['git', '-C', str(m2v), 'show', 'HEAD:' + name])
    if hashlib.sha256(pinned).hexdigest() != digest:
        raise SystemExit('Translator file %s differs from its recorded digest' % name)
print('translator: %s' % translator['commit'][:12])
