#!/usr/bin/env python3
"""Build a relocatable macOS application from a configured Meson build."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
MACH_MAGICS = {
    b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
    b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
    b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca',
}


def run(*args):
    try:
        return subprocess.check_output([str(a) for a in args], text=True).strip()
    except subprocess.CalledProcessError as error:
        if error.output:
            sys.stderr.write(error.output)
        raise


def macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open('rb') as stream:
        return stream.read(4) in MACH_MAGICS


def dependencies(output):
    # otool emits a header per architecture for universal binaries.
    return list(dict.fromkeys(re.findall(r'^\s+(.+?) \(compatibility version ', output, re.M)))


def rpaths(output):
    return re.findall(r'cmd LC_RPATH\n.*?\n\s*path (.*?) \(offset', output)


def system_library(name):
    return name.startswith(('/System/Library/', '/usr/lib/'))


def make_writable(directory):
    if directory.exists():
        for path in [directory, *directory.rglob('*')]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o200))


def copy_tree(source, destination, origins=None):
    if source.exists():
        make_writable(destination)
        # Dereference package-manager symlinks: no references outside the bundle.
        shutil.copytree(source, destination, symlinks=False, dirs_exist_ok=True)
        make_writable(destination)
        if origins is not None:
            for original in source.rglob('*'):
                path = destination / original.relative_to(source)
                if macho(path):
                    origins[path] = original.resolve()


def dependency_prefixes(packages):
    pending = list(packages)
    visited = set()
    prefixes = set()
    while pending:
        package = pending.pop()
        if package in visited:
            continue
        visited.add(package)
        prefix = run('pkg-config', '--variable=prefix', package)
        if prefix and Path(prefix).is_absolute() and Path(prefix) not in (Path('/'), Path('/usr'), Path('/System')):
            prefixes.add(Path(prefix))
        for requirement in run('pkg-config', '--print-requires', package).splitlines():
            if requirement.strip():
                pending.append(requirement.split()[0])
    # Homebrew's shared data (notably icon themes) is outside individual keg
    # prefixes. Its pkg-config search directories identify the common prefix.
    for directory in run('pkg-config', '--variable=pc_path', 'pkg-config').split(':'):
        path = Path(directory)
        if path.is_absolute() and path.name == 'pkgconfig' and path.parent.name in ('lib', 'share'):
            common = path.parent.parent
            if common not in (Path('/'), Path('/usr'), Path('/System')) and any(p.is_relative_to(common) for p in prefixes):
                prefixes.add(common)
    return prefixes


class Relocator:
    def __init__(self, app, prefix, staged_prefix):
        self.app = app
        self.prefix = prefix
        self.staged_prefix = staged_prefix
        self.frameworks = app / 'Contents/Frameworks'
        self.frameworks.mkdir(parents=True)
        self.sources = {}
        self.names = {}
        self.done = set()

    def resolve(self, name, source, inherited):
        loader = source.parent
        executable = self.sources[self.app / 'Contents/MacOS/dia-bin'].parent
        def expand(value):
            return value.replace('@loader_path', str(loader)).replace('@executable_path', str(executable))
        paths = [expand(p) for p in rpaths(run('otool', '-l', source))] + inherited
        if name.startswith('@rpath/'):
            candidates = [Path(p) / name[len('@rpath/'):] for p in paths]
        else:
            candidates = [Path(expand(name))]
        for candidate in list(candidates):
            if candidate.is_absolute() and candidate.is_relative_to(self.prefix):
                candidates.insert(0, self.staged_prefix / candidate.relative_to(self.prefix))
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve(), paths
        raise RuntimeError(f'Cannot resolve {name} required by {source}; searched {candidates}')

    def visit(self, target, source, inherited=()):
        if target in self.done:
            return
        self.done.add(target)
        self.sources[target] = source
        target.chmod(target.stat().st_mode | 0o200)
        # Keep the existing signature load command during relocation: removing it
        # can leave arm64 __LINKEDIT layouts that install_name_tool rejects.
        # Relocation invalidates the old signature; final signing replaces it.
        own_id = run('otool', '-D', target).splitlines()[1:]
        for name in dependencies(run('otool', '-L', source)):
            if name in own_id or system_library(name):
                continue
            dependency, paths = self.resolve(name, source, list(inherited))
            dest = self.frameworks / dependency.name
            previous = self.names.setdefault(dependency.name, dependency)
            if previous != dependency:
                raise RuntimeError(f'Library name collision: {previous} and {dependency}')
            if dest not in self.sources:
                shutil.copy2(dependency, dest)
                self.sources[dest] = dependency
            relative = os.path.relpath(dest, target.parent)
            run('install_name_tool', '-change', name, '@loader_path/' + relative, target)
            self.visit(dest, dependency, paths)
        if own_id:
            run('install_name_tool', '-id', '@rpath/' + target.name, target)
        # Do not retain build-machine search paths in the shipped binaries.
        for path in dict.fromkeys(rpaths(run('otool', '-l', target))):
            run('install_name_tool', '-delete_rpath', path, target)

    def verify(self):
        for binary in self.app.rglob('*'):
            if not macho(binary):
                continue
            for name in dependencies(run('otool', '-L', binary)):
                if system_library(name) or name in run('otool', '-D', binary).splitlines()[1:]:
                    continue  # dylib's own install name
                if not name.startswith('@loader_path/'):
                    raise RuntimeError(f'Unrelocated dependency {name} in {binary}')
                if not (binary.parent / name[len('@loader_path/'):]).is_file():
                    raise RuntimeError(f'Missing bundled dependency {name} in {binary}')
            if rpaths(run('otool', '-l', binary)):
                raise RuntimeError(f'Unexpected runtime search paths in {binary}')
        for path in self.app.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(self.app.resolve()):
                raise RuntimeError(f'External symlink in bundle: {path}')


def python_runtime(python, resources, origins):
    info = json.loads(run(python, '-c', '''import json, sys, sysconfig, gi, cairo
print(json.dumps(dict(version=f"{sys.version_info.major}.{sys.version_info.minor}",
stdlib=sysconfig.get_path("stdlib"), gi=gi.__path__[0], cairo=cairo.__path__[0])))'''))
    expected = run('pkg-config', '--modversion', 'python3-embed').split('.')[:2]
    if info['version'].split('.') != expected:
        raise RuntimeError('The supplied Python must match Meson\'s python3-embed dependency')
    home = resources / 'python'
    stdlib = home / 'lib' / ('python' + info['version'])
    shutil.copytree(info['stdlib'], stdlib, symlinks=False,
                    ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'test', 'tests'))
    for path in stdlib.rglob('*'):
        if macho(path):
            origins[path] = (Path(info['stdlib']) / path.relative_to(stdlib)).resolve()
    for module in ('gi', 'cairo'):
        copy_tree(Path(info[module]), stdlib / 'site-packages' / module, origins)
    return info['version']


def relocate_font_config(resources, prefixes):
    for config in (resources / 'etc/fonts').rglob('*.conf'):
        text = config.read_text()
        # Fontconfig's external DTD can supply this namespace declaration.
        # ElementTree does not load external DTDs, so make it explicit.
        for name, uri in (('its', 'http://www.w3.org/2005/11/its'),
                          ('xsi', 'http://www.w3.org/2001/XMLSchema-instance')):
            root_tag = re.search(r'<fontconfig(?:\s[^>]*)?>', text)
            if name + ':' in text and root_tag and 'xmlns:' + name not in root_tag.group():
                text = re.sub(r'<fontconfig(?=[\s>])',
                              f'<fontconfig xmlns:{name}="{uri}"', text, count=1)
            ET.register_namespace(name, uri)
        try:
            tree = ET.ElementTree(ET.fromstring(text))
        except ET.ParseError as error:
            line = text.splitlines()[error.position[0] - 1]
            raise RuntimeError(f'Invalid font configuration {config}: {error}: {line}') from error
        for node in tree.getroot().iter():
            if node.tag == 'cachedir':
                node.set('prefix', 'xdg')
                node.text = 'fontconfig'
            elif node.tag in ('dir', 'include') and node.text:
                for prefix in sorted(prefixes, key=lambda p: len(str(p)), reverse=True):
                    if node.text.startswith(str(prefix) + '/'):
                        target = resources / Path(node.text).relative_to(prefix)
                        node.text = os.path.relpath(target, config.parent)
                        node.set('prefix', 'relative')
                        break
        tree.write(config, encoding='utf-8', xml_declaration=True)


def prepare_typelibs(prefixes, resources, relocator, origins, workdir):
    # Typelibs can contain absolute library paths. Recompile the GTK dependency
    # closure with bundle-relative names, rather than relying on DYLD variables
    # that hardened-runtime executables may ignore.
    ns = '{http://www.gtk.org/introspection/core/1.0}'
    ET.register_namespace('', ns[1:-1])
    ET.register_namespace('c', 'http://www.gtk.org/introspection/c/1.0')
    ET.register_namespace('glib', 'http://www.gtk.org/introspection/glib/1.0')
    ET.register_namespace('doc', 'http://www.gtk.org/introspection/doc/1.0')
    gir_dirs = [p / directory for p in sorted(prefixes) for directory in ('share/gir-1.0', 'lib/gir-1.0')]
    gir_dirs.append(Path(run('pkg-config', '--variable=girdir', 'gobject-introspection-1.0')))
    pending = ['Gtk-3.0', 'PangoCairo-1.0']
    processed = set()
    workdir.mkdir()
    target = resources / 'lib/girepository-1.0'
    target.mkdir(parents=True, exist_ok=True)
    while pending:
        name = pending.pop()
        if name in processed:
            continue
        processed.add(name)
        source = next((p / (name + '.gir') for p in gir_dirs if (p / (name + '.gir')).is_file()), None)
        if source is None:
            raise RuntimeError(f'Missing GIR source for {name}')
        tree = ET.parse(source)
        for include in tree.getroot().findall(ns + 'include'):
            pending.append(include.attrib['name'] + '-' + include.attrib['version'])
        namespace = tree.getroot().find(ns + 'namespace')
        names = []
        for library in namespace.attrib.get('shared-library', '').split(','):
            if not library:
                continue
            if system_library(library):
                names.append(library)
                continue
            candidates = [Path(library)] if Path(library).is_absolute() else [p / 'lib' / library for p in prefixes]
            original = next((p.resolve() for p in candidates if p.is_file()), None)
            if original is None:
                raise RuntimeError(f'Cannot locate typelib library {library} for {name}')
            dest = relocator.frameworks / original.name
            previous = relocator.names.setdefault(original.name, original)
            if previous != original:
                raise RuntimeError(f'Conflicting typelib libraries: {previous} and {original}')
            if dest not in origins:
                shutil.copy2(original, dest)
                origins[dest] = original
            names.append('@executable_path/../Frameworks/' + dest.name)
        namespace.set('shared-library', ','.join(names))
        tree.write(workdir / (name + '.gir'), encoding='utf-8', xml_declaration=True)
    includes = [flag for directory in [workdir, *gir_dirs] for flag in ('--includedir', str(directory))]
    for name in sorted(processed):
        run('g-ir-compiler', *includes, workdir / (name + '.gir'), '--output', target / (name + '.typelib'))


def build(args):
    if sys.platform != 'darwin':
        raise RuntimeError('Bundle creation requires macOS and Xcode command-line tools')
    app = args.output.absolute()
    if app.suffix != '.app' or app.exists():
        raise RuntimeError('Output must be a new .app path; existing output is never overwritten')
    options = {o['name']: o['value'] for o in json.loads(run('meson', 'introspect', '--buildoptions', args.builddir))}
    project = json.loads(run('meson', 'introspect', '--projectinfo', args.builddir))
    prefix = Path(options['prefix'])
    def staged(stage, option):
        path = Path(options[option])
        return stage / str(path if path.is_absolute() else prefix / path).lstrip('/')
    run('meson', 'compile', '-C', args.builddir, '-j', args.jobs)
    app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dia-bundle-') as temporary:
        temp = Path(temporary)
        stage = temp / 'stage'
        run('meson', 'install', '-C', args.builddir, '--no-rebuild', '--destdir', stage)
        # Build off to the side so a failed package is never mistaken for complete.
        bundle = temp / 'Dia.app'
        contents = bundle / 'Contents'
        resources = contents / 'Resources'
        macos = contents / 'MacOS'
        resources.mkdir(parents=True)
        macos.mkdir()
        shutil.copy2(staged(stage, 'bindir') / 'dia', macos / 'dia-bin')
        shutil.copy2(HERE / 'dia', resources / 'launch.sh')
        architectures = run('lipo', '-archs', macos / 'dia-bin').split()
        arch_flags = [flag for arch in architectures for flag in ('-arch', arch)]
        run('xcrun', 'clang', *arch_flags, '-mmacosx-version-min=' + args.minimum_macos,
            HERE / 'launcher.c', '-o', macos / 'dia')
        origins = {macos / 'dia-bin': staged(stage, 'bindir') / 'dia', macos / 'dia': macos / 'dia'}
        copy_tree(staged(stage, 'datadir'), resources / 'share', origins)
        copy_tree(staged(stage, 'libdir'), resources / 'lib', origins)
        (resources / 'data').symlink_to('share/dia', target_is_directory=True)
        shutil.copy2(HERE / 'Resources/Dia.icns', resources / 'Dia.icns')
        prefixes = dependency_prefixes(
            ('gtk+-3.0', 'glib-2.0', 'gdk-pixbuf-2.0', 'pango', 'fontconfig', 'gobject-introspection-1.0'))
        for dep_prefix in sorted(prefixes):
            for folder in ('glib-2.0/schemas', 'mime', 'icons', 'locale', 'fonts'):
                copy_tree(dep_prefix / 'share' / folder, resources / 'share' / folder)
            for folder in ('gdk-pixbuf-2.0', 'gtk-3.0', 'gio/modules'):
                copy_tree(dep_prefix / 'lib' / folder, resources / 'lib' / folder, origins)
            copy_tree(dep_prefix / 'etc/fonts', resources / 'etc/fonts')
        relocate_font_config(resources, prefixes)
        version = python_runtime(args.python, resources, origins)
        (resources / 'python-version').write_text(version + '\n')
        # Loader cache paths depend on where the user puts the app. Generate at launch.
        for pattern in ('lib/**/loaders.cache', 'lib/**/immodules.cache'):
            for cache in resources.glob(pattern):
                cache.unlink()
        (resources / 'bin').mkdir(exist_ok=True)
        query = Path(run('pkg-config', '--variable=gdk_pixbuf_query_loaders', 'gdk-pixbuf-2.0'))
        if not query.is_file():
            raise RuntimeError('gdk-pixbuf-query-loaders was not found through pkg-config')
        shutil.copy2(query, resources / 'bin/gdk-pixbuf-query-loaders')
        im_query = shutil.which('gtk-query-immodules-3.0')
        if im_query:
            tool = resources / 'bin/gtk-query-immodules-3.0'
            shutil.copy2(im_query, tool)
            origins[tool] = Path(im_query).resolve()
        elif any(resources.glob('lib/gtk-3.0/**/immodules/*')):
            raise RuntimeError('GTK input modules require gtk-query-immodules-3.0')
        run('glib-compile-schemas', resources / 'share/glib-2.0/schemas')
        with (HERE / 'Info.plist.in').open('rb') as stream:
            plist = plistlib.load(stream)
        plist.update(CFBundleShortVersionString=project['version'], CFBundleVersion=args.bundle_version,
                     LSMinimumSystemVersion=args.minimum_macos)
        with (contents / 'Info.plist').open('wb') as stream:
            plistlib.dump(plist, stream)
        relocator = Relocator(bundle, prefix, stage / str(prefix).lstrip('/'))
        origins[resources / 'bin/gdk-pixbuf-query-loaders'] = query.resolve()
        prepare_typelibs(prefixes, resources, relocator, origins, temp / 'gir')
        relocator.sources.update(origins)
        for binary, source in list(relocator.sources.items()):
            relocator.visit(binary, source)
        relocator.verify()
        target_version = tuple(int(v) for v in args.minimum_macos.split('.'))
        target_version += (0,) * (3 - len(target_version))
        architectures = set(run('lipo', '-archs', macos / 'dia-bin').split())
        for binary in relocator.done:
            if not architectures.issubset(set(run('lipo', '-archs', binary).split())):
                raise RuntimeError(f'Architecture mismatch: {binary}')
            load_commands = run('otool', '-l', binary)
            for command in re.split(r'Load command \d+', load_commands):
                if not re.search(r'cmd LC_(BUILD_VERSION|VERSION_MIN_MACOSX)\b', command):
                    continue
                minimum = re.search(r'(?:minos|version) (\d+\.\d+(?:\.\d+)?)', command).group(1)
                value = tuple(int(v) for v in minimum.split('.'))
                value += (0,) * (3 - len(value))
                if value > target_version:
                    raise RuntimeError(f'{binary} requires macOS {minimum}, above {args.minimum_macos}')
        # Sign inside-out, including Python extension modules and GTK loaders.
        identity = args.sign_identity or '-'
        for binary in sorted(relocator.done, key=lambda p: len(p.parts), reverse=True):
            flags = ['--options', 'runtime', '--timestamp'] if args.sign_identity else []
            run('codesign', '--force', '--sign', identity, *flags, binary)
        flags = ['--options', 'runtime', '--timestamp'] if args.sign_identity else []
        run('codesign', '--force', '--sign', identity, *flags, bundle)
        run('codesign', '--verify', '--deep', '--strict', bundle)
        shutil.copytree(bundle, app, symlinks=True)
    print(app)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--builddir', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('Dia.app'))
    parser.add_argument('--python', default='python3', help='Python matching python3-embed, with gi and cairo')
    parser.add_argument('--jobs', default='4')
    parser.add_argument('--minimum-macos', required=True, help='Deployment target used for Dia and all dependencies')
    parser.add_argument('--bundle-version', default='1', help='Numeric Apple build version')
    parser.add_argument('--sign-identity', help='Developer ID Application identity; default is local ad-hoc signing')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+(\.\d+){0,2}', args.bundle_version):
        parser.error('--bundle-version must contain one to three numeric components')
    if not re.fullmatch(r'\d+\.\d+(\.\d+)?', args.minimum_macos):
        parser.error('--minimum-macos must be a numeric macOS version')
    try:
        build(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Packaging failed: {error}\n')


if __name__ == '__main__':
    main()
