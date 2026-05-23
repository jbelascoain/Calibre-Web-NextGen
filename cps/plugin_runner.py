# -*- coding: utf-8 -*-
"""
Plugin action runner — executed inside the calibre-debug subprocess.

Launched by cps/plugins.py via `calibre-debug <this_file>` with the following
environment variables set:

    CWA_ZIP         absolute path to the plugin ZIP file
    CWA_CLASS       name of the Qt config class to instantiate
    CWA_METHOD      name of the method to call on the config class
    CWA_MODULE      normalized plugin module name (e.g. 'deacsm')
    CWA_DIALOG_DIR  temp directory used for IPC files

Why a standalone .py file (and not a giant string built from a list):
    * Syntax highlighting, line-accurate tracebacks, and linting.
    * The patches below MUST run inside the calibre-debug Python process so
      Qt widgets resolve to our replacements before the plugin's own code
      binds to them at import time.

IPC protocol with the Flask side:
    1. To prompt the user, the runner writes the dialog spec to
       <CWA_DIALOG_DIR>/dialog_request.json and touches dialog_ready.
       It then polls for response_ready and reads dialog_response.json.
    2. On exit, the runner writes the outcome to <CWA_DIALOG_DIR>/result.json:
           { "success": bool, "message": str, "files": [str, ...] }
       The Flask side reads that file to decide success/failure — stdout is
       not parsed, so plugins are free to print anything without confusing us.
"""
import os
import sys
import json
import time
import shutil
import tempfile
import zipfile
import types
import importlib.util


# ── Configuration from environment ───────────────────────────────────────────

ZIP_PATH      = os.environ['CWA_ZIP']
CONFIG_CLASS  = os.environ['CWA_CLASS']
METHOD_NAME   = os.environ['CWA_METHOD']
PLUGIN_MODULE = os.environ['CWA_MODULE']
DIALOG_DIR    = os.environ['CWA_DIALOG_DIR']

EXPORTS_DIR    = os.path.join(DIALOG_DIR, 'exports')
RESULT_FILE    = os.path.join(DIALOG_DIR, 'result.json')
DIALOG_TIMEOUT = 120   # seconds to wait for user dialog response

os.makedirs(EXPORTS_DIR, exist_ok=True)


# ── Result reporting ─────────────────────────────────────────────────────────

def write_result(success, message, files=None):
    """Write the final outcome to result.json. Called exactly once before exit."""
    try:
        with open(RESULT_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'success': bool(success),
                'message': str(message),
                'files': files or [],
            }, f)
    except Exception:
        # If we cannot write the result, the parent will detect missing
        # result.json and report a generic crash. Nothing more we can do.
        pass


# ── IPC: ask the web UI for a response and block until it arrives ────────────

def wait_for_dialog_response(spec):
    """
    Write the dialog spec, signal readiness, and poll until response_ready
    appears. Returns the parsed response dict, or a cancelled-with-timeout
    sentinel if the user never replied within DIALOG_TIMEOUT.
    """
    req_file  = os.path.join(DIALOG_DIR, 'dialog_request.json')
    ready     = os.path.join(DIALOG_DIR, 'dialog_ready')
    resp_file = os.path.join(DIALOG_DIR, 'dialog_response.json')
    resp_rdy  = os.path.join(DIALOG_DIR, 'response_ready')

    with open(req_file, 'w', encoding='utf-8') as f:
        json.dump(spec, f)
    open(ready, 'w').close()

    deadline = time.time() + DIALOG_TIMEOUT
    while time.time() < deadline:
        if os.path.exists(resp_rdy):
            with open(resp_file, encoding='utf-8') as f:
                resp = json.load(f)
            for p in (resp_rdy, resp_file):
                try: os.unlink(p)
                except OSError: pass
            return resp
        time.sleep(0.1)

    return {'cancelled': True, 'timeout': True}


# ── QApplication: required before any Qt widget can be created ───────────────

# Module-level reference to keep the QApplication alive for the whole subprocess.
# Without this, the Python wrapper gets garbage-collected and the underlying Qt
# object dies, so any later QWidget instantiation crashes with
# "Must construct a QApplication before a QWidget".
_qapp = None


def init_qapplication():
    """Bootstrap a headless QApplication so plugin Qt widgets can be instantiated."""
    global _qapp
    for import_path in ('qt.core', 'PyQt5.Qt'):
        try:
            module = __import__(import_path, fromlist=['QApplication'])
            QApplication = module.QApplication
            _qapp = QApplication.instance() or QApplication([])
            return _qapp
        except Exception:
            continue
    return None


# ── Patched calibre.gui2 dialog functions ────────────────────────────────────

def _route_warning(parent, title, msg, *a, **k):
    wait_for_dialog_response({'type': 'warning', 'title': str(title), 'msg': str(msg)})
    return True


def _route_error(parent, title, msg, *a, **k):
    wait_for_dialog_response({'type': 'error', 'title': str(title), 'msg': str(msg)})
    return True


def _route_info(parent, title, msg, *a, **k):
    wait_for_dialog_response({'type': 'info', 'title': str(title), 'msg': str(msg)})
    return True


def _route_question(parent, title, msg, *a, yes_text='Yes', no_text='No', **k):
    resp = wait_for_dialog_response({
        'type': 'question',
        'title': str(title), 'msg': str(msg),
        'yes': str(yes_text), 'no': str(no_text),
    })
    return resp.get('value', False)


def _route_save_file(parent, title, *a, initial_path=None, suggested_filename=None, name='', **k):
    fn = suggested_filename or name or (os.path.basename(initial_path) if initial_path else 'export.bin')
    save_path = os.path.join(EXPORTS_DIR, fn)
    resp = wait_for_dialog_response({'type': 'save_file', 'title': str(title), 'filename': fn})
    return None if resp.get('cancelled') else save_path


def _route_open_url(url, *a, **k):
    wait_for_dialog_response({'type': 'open_url', 'url': str(url)})


def patch_calibre_gui2():
    """Replace calibre.gui2 dialog functions with our IPC routers."""
    try:
        import calibre.gui2 as g2
        g2.warning_dialog   = _route_warning
        g2.error_dialog     = _route_error
        g2.info_dialog      = _route_info
        g2.question_dialog  = _route_question
        g2.open_url         = _route_open_url
        g2.choose_save_file = _route_save_file
        g2.choose_files     = lambda p, t, *a, **k: []
        try:
            import calibre.gui2.choose_files as cf
            cf.choose_save_file = _route_save_file
            cf.choose_files     = lambda p, t, *a, **k: []
        except Exception:
            pass
    except Exception:
        pass


# ── Patched Qt dialog classes (QInputDialog / QFileDialog / QMessageBox) ─────

class CWAInputDialog:
    """Drop-in replacement for QInputDialog static methods."""

    @staticmethod
    def getText(parent, title, label, *a, text='', **k):
        resp = wait_for_dialog_response({
            'type': 'text_input', 'title': str(title),
            'label': str(label), 'default': str(text),
        })
        return ('', False) if resp.get('cancelled') else (resp.get('value', ''), True)

    @staticmethod
    def getItem(parent, title, label, items, current=0, *a, **k):
        items_list = list(items)
        resp = wait_for_dialog_response({
            'type': 'select', 'title': str(title), 'label': str(label),
            'items': items_list, 'current': int(current),
        })
        if resp.get('cancelled'):
            return ('', False)
        return (resp.get('value', items_list[0] if items_list else ''), True)


class CWAFileDialog:
    """Drop-in replacement for QFileDialog static methods."""

    @staticmethod
    def getSaveFileName(parent, title='Save', directory='', filter='', *a, **k):
        fn = os.path.basename(directory) or 'export.bin'
        save_path = os.path.join(EXPORTS_DIR, fn)
        resp = wait_for_dialog_response({'type': 'save_file', 'title': str(title), 'filename': fn})
        return ('', filter) if resp.get('cancelled') else (save_path, filter)

    @staticmethod
    def getOpenFileName(*a, **k):
        return ('', '')

    @staticmethod
    def getExistingDirectory(*a, **k):
        return ''


class CWAMessageBox:
    """
    Drop-in replacement for QMessageBox covering the four common static dialogs.
    The integer button values are the ones Qt itself uses, so plugins that
    compare the return value still work.
    """
    Yes    = 16384
    No     = 65536
    Ok     = 1024
    Cancel = 4194304
    StandardButton = type('SB', (), {'Yes': 16384, 'No': 65536, 'Ok': 1024, 'Cancel': 4194304})()

    @staticmethod
    def question(parent, title, msg, *a, **k):
        ok = wait_for_dialog_response({'type': 'question', 'title': str(title), 'msg': str(msg)}).get('value')
        return CWAMessageBox.Yes if ok else CWAMessageBox.No

    @staticmethod
    def warning(parent, title, msg, *a, **k):
        wait_for_dialog_response({'type': 'warning', 'title': str(title), 'msg': str(msg)})
        return CWAMessageBox.Ok

    @staticmethod
    def information(parent, title, msg, *a, **k):
        wait_for_dialog_response({'type': 'info', 'title': str(title), 'msg': str(msg)})
        return CWAMessageBox.Ok

    @staticmethod
    def critical(parent, title, msg, *a, **k):
        wait_for_dialog_response({'type': 'error', 'title': str(title), 'msg': str(msg)})
        return CWAMessageBox.Ok

    @classmethod
    def exec_(cls):
        return cls.Ok

    def __call__(self, *a, **k):
        return self


def patch_qt_dialogs():
    """Override Qt input, file, and message-box dialogs across qt.core and PyQt5/6."""
    for module_path in ('qt.core', 'PyQt5.QtWidgets', 'PyQt6.QtWidgets'):
        try:
            module = __import__(module_path, fromlist=['QInputDialog'])
            module.QInputDialog = CWAInputDialog
            module.QFileDialog  = CWAFileDialog
            module.QMessageBox  = CWAMessageBox
        except Exception:
            continue


# ── Plugin loading: extract ZIP, build fake calibre_plugins.<MODULE> package ─

def load_plugin_modules(tmpdir):
    """
    Extract the plugin ZIP into tmpdir and pre-load the three known entry-point
    files (__init__.py, prefs.py, config.py) into sys.modules under a fake
    calibre_plugins.<MODULE> package, so the plugin's own relative imports
    resolve correctly.

    Returns the package name used.
    """
    with zipfile.ZipFile(ZIP_PATH) as z:
        z.extractall(tmpdir)

    sys.path.insert(0, tmpdir)

    # Some plugins ship internal libraries as nested ZIPs (e.g. DeACSM).
    # Extract those and add them to sys.path too.
    for fname in sorted(os.listdir(tmpdir)):
        if not fname.endswith('.zip'):
            continue
        stem = fname[:-4]
        nested_dir = os.path.join(tmpdir, '_lib_' + stem)
        os.makedirs(nested_dir, exist_ok=True)
        with zipfile.ZipFile(os.path.join(tmpdir, fname)) as nz:
            nz.extractall(nested_dir)
        inner = os.path.join(nested_dir, stem)
        sys.path.insert(0, inner if os.path.isdir(inner) else nested_dir)

    pkg_name = 'calibre_plugins.' + PLUGIN_MODULE

    if 'calibre_plugins' not in sys.modules:
        sys.modules['calibre_plugins'] = types.ModuleType('calibre_plugins')

    if pkg_name not in sys.modules:
        pm = types.ModuleType(pkg_name)
        pm.__path__ = [tmpdir]
        pm.__package__ = pkg_name
        sys.modules[pkg_name] = pm

    # Only load known entry-point files. Loading every *.py would trigger
    # blocking top-level code in some plugins (e.g. DeACSM's
    # exportPluginAuthToWindowsADE.py blocks forever waiting for Windows APIs).
    for entry in ('__init__.py', 'prefs.py', 'config.py'):
        fpath = os.path.join(tmpdir, entry)
        if not os.path.exists(fpath):
            continue
        modname = pkg_name + '.' + entry[:-3]
        if modname in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(modname, fpath)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg_name
        sys.modules[modname] = mod
        try:
            spec.loader.exec_module(mod)
        except (Exception, SystemExit):
            sys.modules.pop(modname, None)

    return pkg_name


def execute_method(pkg_name):
    """
    Find CONFIG_CLASS in the loaded modules, instantiate it, and call METHOD_NAME.
    Returns (success, message, files).
    """
    for entry in ('config.py', 'prefs.py', '__init__.py'):
        modname = pkg_name + '.' + entry[:-3]
        mod = sys.modules.get(modname)
        if not mod:
            continue
        cls = getattr(mod, CONFIG_CLASS, None)
        if not cls:
            continue

        try:
            # Some plugin config classes expect the plugin's path as 1st arg
            try:
                instance = cls(ZIP_PATH)
            except TypeError:
                instance = cls()

            fn = getattr(instance, METHOD_NAME, None)
            if not fn:
                return False, 'Method %s not found on %s' % (METHOD_NAME, CONFIG_CLASS), []

            result = fn()
            files = os.listdir(EXPORTS_DIR)
            return True, str(result) if result is not None else 'Done', files

        except Exception as e:
            return False, str(e), []

    return False, 'Config class %s not found in any entry-point module' % CONFIG_CLASS, []


# ── Main entry point ─────────────────────────────────────────────────────────

def main():
    init_qapplication()
    # IMPORTANT: patch BEFORE loading the plugin — plugin top-level imports
    # bind to dialog classes at load time, so late patching has no effect.
    patch_calibre_gui2()
    patch_qt_dialogs()

    tmpdir = tempfile.mkdtemp(prefix='cwa_action_')
    try:
        pkg_name = load_plugin_modules(tmpdir)
        success, message, files = execute_method(pkg_name)
    except (Exception, SystemExit) as e:
        success, message, files = False, str(e), []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    write_result(success, message, files)


if __name__ == '__main__':
    main()
