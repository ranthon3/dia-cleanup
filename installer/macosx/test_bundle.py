#!/usr/bin/env python3
"""Portable regression checks; native integration runs in macOS CI."""
import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bundle', HERE / 'bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class PackagingTests(unittest.TestCase):
    def test_otool_universal_dependencies_and_spaces(self):
        output = '''example (architecture arm64):
\t/opt/Some Prefix/lib/libfoo.dylib (compatibility version 1.0.0, current version 1.0.0)
example (architecture x86_64):
\t/opt/Some Prefix/lib/libfoo.dylib (compatibility version 1.0.0, current version 1.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)
'''
        self.assertEqual(bundle.dependencies(output), [
            '/opt/Some Prefix/lib/libfoo.dylib', '/usr/lib/libSystem.B.dylib'])

    def test_resolve_staged_library_and_inherited_rpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'Dia.app'
            app.mkdir()
            stage = root / 'stage'
            (stage / 'lib').mkdir(parents=True)
            library = stage / 'lib/libdia.dylib'
            library.touch()
            relocator = bundle.Relocator(app, Path('/dia-prefix'), stage)
            relocator.sources[app / 'Contents/MacOS/dia-bin'] = stage / 'bin/dia'
            with patch.object(bundle, 'run', return_value=''):
                resolved, _ = relocator.resolve('@rpath/libdia.dylib', stage / 'bin/dia', ['/dia-prefix/lib'])
                self.assertEqual(resolved, library)
                with self.assertRaisesRegex(RuntimeError, 'Cannot resolve'):
                    relocator.resolve('@rpath/missing.dylib', stage / 'bin/dia', [])

    def test_verify_rejects_build_machine_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / 'Dia.app'
            (app / 'Contents/MacOS').mkdir(parents=True)
            binary = app / 'Contents/MacOS/dia-bin'
            binary.write_bytes(b'\xcf\xfa\xed\xfe')
            relocator = bundle.Relocator(app, Path('/prefix'), Path('/stage'))
            def output(*args):
                if args[1] == '-L':
                    return 'binary:\n\t/opt/homebrew/lib/libfoo.dylib (compatibility version 1.0.0, current version 1.0.0)'
                return ''
            with patch.object(bundle, 'run', side_effect=output):
                with self.assertRaisesRegex(RuntimeError, 'Unrelocated dependency'):
                    relocator.verify()

    def test_verify_rejects_external_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / 'Dia.app'
            app.mkdir()
            (app / 'outside').symlink_to('/tmp')
            relocator = bundle.Relocator(app, Path('/prefix'), Path('/stage'))
            with self.assertRaisesRegex(RuntimeError, 'External symlink'):
                relocator.verify()

    def test_launcher_preserves_arguments_status_and_cleans_cache(self):
        with tempfile.TemporaryDirectory(prefix='dia app with spaces ') as temporary:
            root = Path(temporary)
            macos = root / 'Dia.app/Contents/MacOS'
            resources = root / 'Dia.app/Contents/Resources'
            macos.mkdir(parents=True)
            (resources / 'bin').mkdir(parents=True)
            (resources / 'lib/gdk-pixbuf-2.0/2.10.0/loaders').mkdir(parents=True)
            (resources / 'lib/gdk-pixbuf-2.0/2.10.0/loaders/test.so').touch()
            (resources / 'python-version').write_text('3.13\n')
            query = resources / 'bin/gdk-pixbuf-query-loaders'
            query.write_text('#!/bin/sh\nprintf "loader cache\\n"\n')
            query.chmod(0o755)
            binary = macos / 'dia-bin'
            binary.write_text('''#!/bin/sh
printf '%s\\n' "$@" > "$TEST_OUTPUT"
printf '%s\\n' "$GDK_PIXBUF_MODULE_FILE" "$PYTHONHOME" "$DIA_BASE_PATH" >> "$TEST_OUTPUT"
test -s "$GDK_PIXBUF_MODULE_FILE" || exit 99
if [ -n "${GTK_IM_MODULE_FILE:-}" ]; then test -s "$GTK_IM_MODULE_FILE" || exit 98; fi
exit 17
''')
            binary.chmod(0o755)
            launcher = resources / 'launch.sh'
            launcher.write_bytes((HERE / 'dia').read_bytes())
            launcher.chmod(0o755)
            output = root / 'result'
            environment = dict(os.environ, TEST_OUTPUT=str(output), TMPDIR=str(root))
            result = subprocess.run([str(launcher), 'diagram with spaces.dia', '--export=a.svg'], env=environment)
            self.assertEqual(result.returncode, 17)
            lines = output.read_text().splitlines()
            self.assertEqual(lines[:2], ['diagram with spaces.dia', '--export=a.svg'])
            self.assertFalse(Path(lines[2]).exists())
            self.assertEqual(lines[3:], [str(resources / 'python'), str(resources)])
            modules = resources / 'lib/gtk-3.0/3.0.0/immodules'
            modules.mkdir(parents=True)
            (modules / 'test.so').touch()
            im_query = resources / 'bin/gtk-query-immodules-3.0'
            im_query.write_bytes(query.read_bytes())
            im_query.chmod(0o755)
            result = subprocess.run([str(launcher), 'diagram with spaces.dia'], env=environment)
            self.assertEqual(result.returncode, 17)
            self.assertFalse(Path(output.read_text().splitlines()[1]).exists())

    def test_copy_tree_preserves_origins_across_merged_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origins = {}
            for name in ('one', 'two'):
                source = root / name
                source.mkdir()
                (source / (name + '.dylib')).write_bytes(b'\xcf\xfa\xed\xfe')
                bundle.copy_tree(source, root / 'dest', origins)
            self.assertEqual(origins[root / 'dest/one.dylib'], root / 'one/one.dylib')
            self.assertEqual(origins[root / 'dest/two.dylib'], root / 'two/two.dylib')

    def test_font_configuration_is_relocatable_and_uses_user_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary) / 'Resources'
            config = resources / 'etc/fonts/fonts.conf'
            config.parent.mkdir(parents=True)
            config.write_text('<fontconfig><dir>/opt/brew/share/fonts</dir>'
                              '<dir>/System/Library/Fonts</dir>'
                              '<cachedir>/opt/brew/var/cache/fontconfig</cachedir></fontconfig>')
            bundle.relocate_font_config(resources, {Path('/opt/brew')})
            root = bundle.ET.parse(config).getroot()
            self.assertEqual(root[0].text, '../../share/fonts')
            self.assertEqual(root[0].get('prefix'), 'relative')
            self.assertEqual(root[1].text, '/System/Library/Fonts')
            self.assertEqual(root[2].text, 'fontconfig')
            self.assertEqual(root[2].get('prefix'), 'xdg')

    def test_typelib_dependency_closure_uses_bundled_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / 'prefix'
            gir = prefix / 'share/gir-1.0'
            gir.mkdir(parents=True)
            (prefix / 'lib').mkdir()
            for name, includes in [('Gtk-3.0', '<include name="GLib" version="2.0"/>'),
                                   ('GLib-2.0', ''), ('PangoCairo-1.0', '')]:
                namespace, version = name.split('-')
                library = 'lib' + namespace + '.dylib'
                (prefix / 'lib' / library).touch()
                (gir / (name + '.gir')).write_text(
                    '<repository xmlns="http://www.gtk.org/introspection/core/1.0" version="1.2">'
                    + includes + f'<namespace name="{namespace}" version="{version}" '
                    + f'shared-library="{library}"/></repository>')
            app = root / 'Dia.app'
            resources = app / 'Contents/Resources'
            resources.mkdir(parents=True)
            relocator = bundle.Relocator(app, Path('/prefix'), Path('/stage'))
            origins = {}
            commands = []
            def run(*args):
                commands.append(args)
                return str(gir) if args[0] == 'pkg-config' else ''
            with patch.object(bundle, 'run', side_effect=run):
                bundle.prepare_typelibs({prefix}, resources, relocator, origins, root / 'generated')
            ns = '{http://www.gtk.org/introspection/core/1.0}'
            for path in (root / 'generated').glob('*.gir'):
                namespace = bundle.ET.parse(path).getroot().find(ns + 'namespace')
                self.assertTrue(namespace.get('shared-library').startswith('@executable_path/../Frameworks/'))
            self.assertEqual(len(origins), 3)
            self.assertEqual(len([c for c in commands if c[0] == 'g-ir-compiler']), 3)

    def test_plist_uses_standard_bundle_entry_point(self):
        with (HERE / 'Info.plist.in').open('rb') as stream:
            info = plistlib.load(stream)
        self.assertEqual(info['CFBundleExecutable'], 'dia')
        self.assertEqual(info['CFBundleIdentifier'], 'org.gnome.Dia')
        self.assertNotIn('LSMinimumSystemVersion', info)  # Must be explicitly supplied.


if __name__ == '__main__':
    unittest.main()
