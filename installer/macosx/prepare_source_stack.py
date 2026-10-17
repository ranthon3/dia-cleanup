#!/usr/bin/env python3
"""Configure an isolated GTK-OSX source stack for the macOS 12.6 target."""
import argparse
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--gtk-osx', type=Path, required=True)
parser.add_argument('--root', type=Path, required=True)
args = parser.parse_args()
root = args.root.resolve()
root.mkdir(parents=True, exist_ok=True)
for directory in ('modulesets-stable', 'patches'):
    shutil.copytree(args.gtk_osx / directory, root / directory, dirs_exist_ok=True)
# These upstream entries omitted checksums. Values are published in Homebrew's
# pixman and readline formulae for the identical upstream release archives.
missing_hashes = {
    'pixman': 'd09c44ebc3bd5bee7021c79f922fe8fb2fb57f7320f55e97ff9914d2346a591c',
    'readline': 'fe5383204467828cd495ee8d1d3c037a7eba1389c22bc6a041f627976f9061cc',
    'hicolor-icon-theme': 'db0e50a80aa3bf64bb45cbca5cf9f75efd9348cf2ac690b907435238c3cf81d7',
}
for manifest in (root / 'modulesets-stable').glob('*.modules'):
    tree = ET.parse(manifest)
    for repository in tree.iter('repository'):
        url = repository.get('href', '')
        if url.startswith('http://'):
            repository.set('href', 'https://' + url[7:])
    for module in tree.getroot().iter():
        branch = module.find('branch')
        if branch is None:
            continue
        if module.get('id') in missing_hashes:
            assert not branch.get('hash')
            branch.set('hash', 'sha256:' + missing_hashes[module.get('id')])
        if branch.get('hssh') and not branch.get('hash'):
            branch.set('hash', branch.attrib.pop('hssh'))
        digest = branch.get('hash', '')
        if len(digest) == 64 and ':' not in digest:
            branch.set('hash', 'sha256:' + digest)
    tree.write(manifest, encoding='utf-8', xml_declaration=True)
# Fail before compiling if any selected runtime source has no integrity pin.
modules = {}
for manifest in (root / 'modulesets-stable').glob('*.modules'):
    for module in ET.parse(manifest).getroot().iter():
        if module.get('id'):
            modules[module.get('id')] = module
selected = set()
def check_module(name):
    if name in selected or name in ('nasm', 'gtk-doc'):
        return
    selected.add(name)
    module = modules[name]
    branch = module.find('branch')
    if branch is not None and branch.get('module') and not branch.get('hash', '').startswith('sha256:'):
        raise RuntimeError(f'Missing SHA256 for runtime source {name}')
    for dependency in module.findall('./dependencies/dep'):
        check_module(dependency.get('package'))
for name in ('gettext', 'libffi', 'sqlite', 'python3', 'gtk+-3.0', 'pygobject3', 'graphene', 'libxml2', 'adwaita-icon-theme'):
    check_module(name)
print(f'Validated SHA256 pins for {len(selected)} source modules')
(root / 'dia.modules').write_text('''<?xml version="1.0"?>
<moduleset>
  <include href="modulesets-stable/bootstrap.modules"/>
  <include href="modulesets-stable/gtk-osx.modules"/>
</moduleset>
''')
custom = f'''
checkoutroot = {str(root / 'sources')!r}
prefix = {str(root / 'prefix')!r}
buildroot = {str(root / 'build')!r}
moduleset = {str(root / 'dia.modules')!r}
modulesets_dir = {str(root / 'modulesets-stable')!r}
use_local_modulesets = True
modules = ['gettext', 'libffi', 'sqlite', 'python3', 'gtk+-3.0', 'pygobject3', 'graphene', 'libxml2', 'adwaita-icon-theme']
skip = ['nasm', 'gtk-doc']
ignore_suggests = True
makeargs = '-j4'
os.environ['CFLAGS'] = '-O2 -g0'
os.environ['CXXFLAGS'] = '-O2 -g0'
setup_sdk(target='12.6', architectures=['arm64'])
cmakeargs += ' -DCMAKE_OSX_DEPLOYMENT_TARGET=12.6'
append_autogenargs('libxml2', '--with-legacy --without-python')
append_autogenargs('python3', '--with-openssl=' + prefix)
os.environ['PKG_CONFIG_LIBDIR'] = prefix + '/lib/pkgconfig:' + prefix + '/share/pkgconfig'
os.environ['PKG_CONFIG_PATH'] = os.environ['PKG_CONFIG_LIBDIR']
'''
(root / 'jhbuildrc-custom').write_text(custom)
# Load upstream setup without executing its TLS-insecure bootstrap shell script.
config = f'''
import os, sys
os.environ['JHBUILDRC_CUSTOM'] = {str(root / 'jhbuildrc-custom')!r}
exec(compile(open({str(args.gtk_osx.resolve() / 'jhbuildrc-gtk-osx')!r}, 'rb').read(), {str(args.gtk_osx.resolve() / 'jhbuildrc-gtk-osx')!r}, 'exec'))
if not os.path.exists(prefix + '/bin/python3'):
    os.environ['PYTHON'] = sys.executable
'''
(root / 'jhbuildrc').write_text(config)
print(root / 'jhbuildrc')
