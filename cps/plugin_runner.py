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
import inspect
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


# Schema loaded from <DIALOG_DIR>/schema.json (path passed via CWA_SCHEMA).
# Optional — when absent, the runner still works but custom QDialogs all
# fall back to the generic "not available in web mode" message.
_SCHEMA_PATH = os.environ.get('CWA_SCHEMA')
SCHEMA: dict = {}
if _SCHEMA_PATH and os.path.exists(_SCHEMA_PATH):
    try:
        with open(_SCHEMA_PATH, encoding='utf-8') as _f:
            SCHEMA = json.load(_f)
    except (OSError, ValueError):
        SCHEMA = {}

# class_name -> { 'class_name': str, 'title': str, 'fields': [...] }
DIALOG_SCHEMAS: dict = SCHEMA.get('dialogs', {}) or {}


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


# Constants returned by QDialog.exec_() — matching Qt's own enum values so
# plugin code that does `if d.exec_() == QDialog.Accepted` still works.
_QDIALOG_REJECTED = 0
_QDIALOG_ACCEPTED = 1


def _mark_dialog_result(dialog_instance, code):
    """
    Mirror what Qt's own ``QDialog.accept()`` / ``reject()`` would do to the
    dialog's internal state — namely call ``setResult(code)`` and ``done(code)``.

    Plugin code commonly checks ``d.result()`` after ``d.exec_()`` (rather
    than just trusting the return value of exec_()). Without this mirror,
    our injected exec_() return value never reaches ``d.result()`` and the
    plugin's post-exec branch reads stale Rejected, silently skipping work.

    Best-effort: any missing method is ignored.
    """
    for setter in ('setResult', 'done'):
        fn = getattr(dialog_instance, setter, None)
        if callable(fn):
            try:
                fn(code)
            except Exception:
                continue


def patch_qdialog_exec():
    """
    Replace QDialog.exec_() / exec() so it never blocks the headless runner.

    Two branches:

      1. The dialog's class name appears in ``DIALOG_SCHEMAS`` — the AST
         parser found it and produced a schema. We send a ``subdialog`` spec
         to the web UI (class_name + title + fields), wait for the user's
         form submission, inject the returned values into the dialog
         instance so plugin code reading ``d.field.text()`` etc. sees them,
         and return ``QDialog.Accepted``.

      2. The dialog class is unknown (no schema, or a complex dialog Slice 1
         doesn't yet support). We send a one-shot warning to the user that
         the action requires the desktop Calibre, and return
         ``QDialog.Rejected``. The notification only fires once per session
         so cascading dialog calls don't spam.

    The interception runs on QDialog itself, so it applies to every
    QDialog subclass via Python method resolution. QMessageBox/QInputDialog/
    QFileDialog static methods are already routed via separate static-method
    patches earlier — those don't depend on .exec_().
    """
    notified = {'shown': False}

    def _stub_exec(self, *a, **k):
        cls_name = type(self).__name__
        dialog_schema = DIALOG_SCHEMAS.get(cls_name)

        if dialog_schema:
            if dialog_schema.get('is_manager'):
                code = _run_manager_session(self, dialog_schema)
                _mark_dialog_result(self, code)
                return code
            if _schema_is_simple_form(dialog_schema):
                response = wait_for_dialog_response({
                    'type': 'subdialog',
                    'class_name': cls_name,
                    'title': dialog_schema.get('title') or cls_name,
                    'fields': dialog_schema.get('fields') or [],
                })
                if response.get('cancelled'):
                    _mark_dialog_result(self, _QDIALOG_REJECTED)
                    return _QDIALOG_REJECTED
                _inject_subdialog_values(self, dialog_schema.get('fields') or [], response.get('values') or {})
                _mark_dialog_result(self, _QDIALOG_ACCEPTED)
                return _QDIALOG_ACCEPTED

        # Unknown dialog class — show the generic "not available" notice once
        # per subprocess and report Rejected so the plugin's cancelled branch runs.
        if not notified['shown']:
            wait_for_dialog_response({
                'type': 'error',
                'title': 'Action not available in web mode',
                'msg': 'This plugin button opens a custom dialog window that '
                       'the web interface cannot render. Use the desktop '
                       'Calibre application to manage this feature.',
            })
            notified['shown'] = True
        _mark_dialog_result(self, _QDIALOG_REJECTED)
        return _QDIALOG_REJECTED

    for module_path in ('qt.core', 'PyQt5.QtWidgets', 'PyQt6.QtWidgets'):
        try:
            module = __import__(module_path, fromlist=['QDialog'])
            QDialog = module.QDialog
            QDialog.exec_ = _stub_exec
            QDialog.exec  = _stub_exec
        except Exception:
            continue


# ── Sub-dialog support helpers ──────────────────────────────────────────────

def _schema_is_simple_form(dialog_schema):
    """
    Return True iff the dialog's schema fits the Slice 1 "simple form"
    pattern: only input widgets (text, checkbox, select, number, textarea,
    password), no button fields.

    Dialogs with button fields are list/key managers handled by
    ``_run_manager_session`` (Slice 2) — they require an interactive loop
    rather than a one-shot form roundtrip.
    """
    fields = dialog_schema.get('fields') or []
    for f in fields:
        if f.get('type') == 'button':
            return False
    return True


# ── Manager-style sub-dialog session (Slice 2) ──────────────────────────────

# Cap on how many user actions a single manager session may process before we
# bail out. Stops a runaway loop from blocking the subprocess timeout.
_MANAGER_MAX_ACTIONS = 200


def _resolve_field_widget(instance, field_def):
    """
    Locate the actual value-bearing widget on the instance for a field.

    For plain fields (the common case) the widget lives directly at
    ``instance.<name>``. For custom-widget fields (Slice 7), the parser
    set ``inner_attr`` so the runner walks one hop deeper:
    ``instance.<name>.<inner_attr>``. Returns ``None`` when either step
    fails — callers treat that as "field absent on this instance".
    """
    name = field_def.get('name')
    if not name:
        return None
    outer = getattr(instance, name, None)
    if outer is None:
        return None
    inner = field_def.get('inner_attr')
    if not inner:
        return outer
    return getattr(outer, inner, None)


def _read_list_items(widget):
    """
    Best-effort snapshot of a QListWidget's items as a list of strings.

    Tries the standard Qt API (count() + item(i).text()). Returns an empty
    list if the widget isn't a list-like collection or any step fails.
    """
    try:
        n = widget.count()
    except (AttributeError, TypeError):
        return []
    items = []
    try:
        for i in range(int(n)):
            it = widget.item(i)
            if it is None:
                continue
            try:
                items.append(str(it.text()))
            except (AttributeError, TypeError):
                items.append('')
    except (AttributeError, TypeError):
        return items
    return items


def _read_table_items(widget):
    """
    Best-effort snapshot of a QTableWidget as a structured payload:
        { 'headers': [str ...], 'rows': [[str ...], ...] }

    Reads the horizontal header section to label each column, then walks
    each (row, col) cell, falling back to '' for empty/missing cells.
    Returns a payload with empty lists when the widget isn't table-shaped
    or any step fails — same defensive style as _read_list_items.
    """
    try:
        nrows = int(widget.rowCount())
        ncols = int(widget.columnCount())
    except (AttributeError, TypeError, ValueError):
        return {'headers': [], 'rows': []}

    headers = []
    try:
        hdr = widget.horizontalHeader()
        model = widget.model()
    except (AttributeError, TypeError):
        hdr, model = None, None
    if model is not None:
        for c in range(ncols):
            try:
                txt = model.headerData(c, 1)   # 1 == Qt.Horizontal
                headers.append(str(txt) if txt is not None else '')
            except Exception:
                headers.append('')

    rows = []
    for r in range(nrows):
        row = []
        for c in range(ncols):
            try:
                cell = widget.item(r, c)
                row.append(str(cell.text()) if cell is not None else '')
            except (AttributeError, TypeError):
                row.append('')
        rows.append(row)
    return {'headers': headers, 'rows': rows}


def _read_text_value(widget):
    """Best-effort current text from a QLineEdit-like widget."""
    for getter in ('text', 'toPlainText'):
        fn = getattr(widget, getter, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                continue
    return ''


def _snapshot_manager_state(dialog_instance, dialog_schema):
    """
    Read the live values of the dialog's data-bearing fields so the next
    snapshot can refresh the web UI.

    Buttons carry no state — they're announced via the schema and
    dispatched separately. Lists, tables, texts, checkboxes, selects,
    numbers, radios, and sliders are all snapshotted here.
    """
    state = {
        'lists': {}, 'tables': {}, 'texts': {},
        'flags': {}, 'numbers': {}, 'selections': {},
    }
    for f in dialog_schema.get('fields') or []:
        name = f.get('name')
        ftype = f.get('type')
        if not name:
            continue
        widget = _resolve_field_widget(dialog_instance, f)
        if widget is None:
            continue
        try:
            if ftype == 'list':
                state['lists'][name] = _read_list_items(widget)
            elif ftype == 'table':
                state['tables'][name] = _read_table_items(widget)
            elif ftype in ('text', 'password', 'textarea'):
                state['texts'][name] = _read_text_value(widget)
            elif ftype in ('checkbox', 'radio'):
                fn = getattr(widget, 'isChecked', None)
                if callable(fn):
                    state['flags'][name] = bool(fn())
            elif ftype in ('number', 'slider'):
                fn = getattr(widget, 'value', None)
                if callable(fn):
                    state['numbers'][name] = int(fn())
            elif ftype == 'select':
                fn = getattr(widget, 'currentText', None)
                if callable(fn):
                    state['selections'][name] = str(fn())
        except Exception:
            # Skip fields whose live read raises — best-effort only.
            continue
    return state


def _select_list_item(dialog_instance, dialog_schema, field_name, index):
    """
    Push a selection onto the named collection widget so the next method
    call sees it. Manager actions (delete/rename/export) read the
    selection via ``currentRow()``/``currentItem()`` (QListWidget) or
    ``currentRow()``/``selectRow()`` (QTableWidget); without this the
    plugin treats the click as "nothing selected" and silently does
    nothing.

    Tries the cross-type setters in order (setCurrentRow first, then
    selectRow as a fallback for QTableWidget when only one row at a time
    is meaningful).
    """
    if not field_name or index is None:
        return
    widget = getattr(dialog_instance, field_name, None)
    if widget is None:
        return
    for setter_name in ('setCurrentRow', 'selectRow', 'setCurrentIndex'):
        setter = getattr(widget, setter_name, None)
        if callable(setter):
            try:
                setter(int(index))
                return
            except Exception:
                continue


def _run_manager_session(dialog_instance, dialog_schema):
    """
    Interactive loop for manager-style sub-dialogs.

    Each pass:
      1. Snapshot the dialog's data-bearing widgets (list contents, texts).
      2. Send the snapshot + the action-button list to the web UI as a
         ``manager_dialog`` spec.
      3. Wait for the user's action:
           * ``close`` — leave the loop, return Accepted/Rejected as asked.
           * ``select`` — push the selection onto the named list widget,
             no method call yet; the next snapshot will reflect any
             selection-dependent state.
           * ``button`` — invoke the dialog method bound to that button
             (via the parsed ``connected_method``); any inner ``.exec_()``
             call inside that method (e.g. an Add dialog) routes through
             the existing simple-form path transparently.

    Method invocation failures inside the manager are caught so a single
    misbehaving button click doesn't terminate the whole session — the
    user can keep interacting with the remaining buttons.
    """
    cls_name = type(dialog_instance).__name__
    fields = dialog_schema.get('fields') or []

    # Pre-compute the action-button summary — buttons that have a resolved
    # connected_method. The frontend only renders these as clickable
    # actions; buttons with no known handler are inert.
    actions = []
    for f in fields:
        if f.get('type') != 'button':
            continue
        method = f.get('connected_method')
        if not method:
            continue
        actions.append({
            'name': f.get('name'),
            'label': f.get('label') or f.get('name'),
            'method': method,
        })

    # The first list/table field is treated as the "primary" collection —
    # that's the widget the frontend renders as the main selectable view,
    # and where ``select`` actions push their index. Plugins with multiple
    # collections in one manager are an edge case we'll address only if a
    # real plugin surfaces the pattern.
    primary_list_name = None
    primary_kind = None  # 'list' | 'table'
    for f in fields:
        if f.get('type') in ('list', 'table'):
            primary_list_name = f.get('name')
            primary_kind = f.get('type')
            break

    for _ in range(_MANAGER_MAX_ACTIONS):
        state = _snapshot_manager_state(dialog_instance, dialog_schema)
        response = wait_for_dialog_response({
            'type': 'manager_dialog',
            'class_name': cls_name,
            'title': dialog_schema.get('title') or cls_name,
            'actions': actions,
            'primary_list': primary_list_name,
            'primary_kind': primary_kind,
            'state': state,
        })

        action = response.get('action')
        if action == 'close':
            return _QDIALOG_ACCEPTED if response.get('accepted') else _QDIALOG_REJECTED

        if action == 'select':
            _select_list_item(
                dialog_instance, dialog_schema,
                response.get('field') or primary_list_name,
                response.get('index'),
            )
            continue

        if action == 'button':
            method_name = response.get('method')
            if not method_name:
                continue
            method = getattr(dialog_instance, method_name, None)
            if not callable(method):
                continue
            # Push any selection sent with the click before invoking so the
            # plugin method sees the current selection.
            if 'index' in response:
                _select_list_item(
                    dialog_instance, dialog_schema,
                    response.get('field') or primary_list_name,
                    response.get('index'),
                )
            try:
                method()
            except Exception:
                # Per-action failures don't end the session — re-snapshot
                # and let the user try something else.
                continue
            continue

        # Unknown action — refuse to spin forever. Treat as cancel.
        return _QDIALOG_REJECTED

    # Action cap reached. Best to terminate cleanly rather than block.
    return _QDIALOG_REJECTED


def _inject_subdialog_values(dialog_instance, fields, values):
    """
    After the user submits the web sub-dialog form, push the entered values
    into the dialog's Qt widget attributes so the plugin's read-back code
    (e.g. ``self.line_edit.text()``, ``self.checkbox.isChecked()``,
    ``self.combo.currentText()``) returns what the user typed.

    Strategy: monkey-patch the relevant getter methods on each widget
    instance to return the submitted value. We patch the bound methods
    directly, so subsequent calls inside the plugin code see the injected
    value transparently.

    Caveat: a few plugins read the value via attribute access rather than
    a getter (``d.field`` instead of ``d.field.text()``). For those we
    additionally try to call the matching setter (``setText``/``setChecked``/
    ``setCurrentText``) on the widget — if the widget is real, the Qt
    object actually stores the value and any future read works.
    """
    for f in fields:
        attr_name = f.get('name')
        if not attr_name:
            continue
        widget = _resolve_field_widget(dialog_instance, f)
        if widget is None:
            continue

        ftype = f.get('type')
        value = values.get(attr_name)

        try:
            if ftype == 'text' or ftype == 'password':
                _patch_getter(widget, 'text', str(value) if value is not None else '')
                _safe_call(widget, 'setText', str(value) if value is not None else '')
            elif ftype == 'textarea':
                _patch_getter(widget, 'toPlainText', str(value) if value is not None else '')
                _patch_getter(widget, 'text', str(value) if value is not None else '')
                _safe_call(widget, 'setPlainText', str(value) if value is not None else '')
            elif ftype == 'checkbox':
                bval = bool(value)
                _patch_getter(widget, 'isChecked', bval)
                _safe_call(widget, 'setChecked', bval)
            elif ftype == 'select':
                str_val = str(value) if value is not None else ''
                items = f.get('options') or []
                try:
                    idx = items.index(value)
                except ValueError:
                    idx = 0
                _patch_getter(widget, 'currentText', str_val)
                _patch_getter(widget, 'currentIndex', idx)
                _safe_call(widget, 'setCurrentText', str_val)
                _safe_call(widget, 'setCurrentIndex', idx)
            elif ftype == 'number' or ftype == 'slider':
                try:
                    num = int(value)
                except (TypeError, ValueError):
                    num = 0
                _patch_getter(widget, 'value', num)
                _safe_call(widget, 'setValue', num)
            elif ftype == 'radio':
                # QRadioButton is read via isChecked(). Mutual-exclusion
                # is normally handled by Qt at the button-group level; we
                # only force the single radio's getter — plugins that
                # consult a QButtonGroup may still pick up stale state,
                # which is a known limit until group introspection lands.
                bval = bool(value)
                _patch_getter(widget, 'isChecked', bval)
                _safe_call(widget, 'setChecked', bval)
        except Exception:
            # Per-field failures should not abort the whole injection — keep
            # going so other fields still get patched.
            continue


def _patch_getter(widget, method_name, return_value):
    """
    Replace ``widget.method_name`` so it returns ``return_value`` regardless
    of arguments. Used for Qt getters like ``text()``, ``isChecked()``,
    ``currentText()`` so the plugin's read-back code returns the injected
    value even when the widget is unrendered (offscreen mode).
    """
    try:
        setattr(widget, method_name, lambda *a, v=return_value, **k: v)
    except (AttributeError, TypeError):
        # Some C++-backed Qt widgets reject method replacement at the Python
        # level. setValue/setText below is the fallback for those.
        pass


def _safe_call(widget, method_name, *args):
    """Invoke ``widget.method_name(*args)`` ignoring missing methods or errors."""
    fn = getattr(widget, method_name, None)
    if not callable(fn):
        return
    try:
        fn(*args)
    except Exception:
        pass


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


class _PluginContextStub:
    """
    Stand-in for plugin-context constructor args we can't synthesize for
    real — typically Calibre runtime objects (``gui``, ``ui``, ``action``,
    ``plugin``, ``db``, etc.) that a desktop Calibre would inject at
    runtime but a headless web flow has no real equivalent for.

    Semantics:
      * Truthy — so plugin guards of the form ``if self.gui:`` proceed
        rather than silently skipping optional features.
      * Chainable — every attribute access returns the same stub, which
        is itself callable and returns a stub. Plugin code that walks
        ``self.gui.current_db.library_path`` doesn't AttributeError on
        any step.
      * String-ready — a few well-known attributes resolve to real
        strings instead of the stub, because plugin code routinely passes
        them to ``os.path.join`` or similar. The two we honor today are
        ``library_path`` (the Calibre library mount) and ``library_dir``.

    Plugins that genuinely depend on a real Calibre runtime to function
    will fail at the moment they need a real value (e.g. real database
    rows). That's the right failure surface — the alternative is to ship
    a half-functional facade that pretends to work.
    """

    _STRING_ATTRS = {
        'library_path': '/calibre-library',
        'library_dir':  '/calibre-library',
    }

    def __getattr__(self, name):
        # Class-level dunders / private names should fall through to the
        # normal AttributeError so Python internals (pickling, copy,
        # introspection) keep working.
        if name.startswith('_'):
            raise AttributeError(name)
        if name in self._STRING_ATTRS:
            return self._STRING_ATTRS[name]
        return self

    def __call__(self, *a, **k):
        return self

    def __bool__(self):
        return True

    def __iter__(self):
        return iter(())

    def __getitem__(self, key):
        return self

    def __contains__(self, item):
        return False

    def __len__(self):
        return 0


def _plugin_data_dir():
    """
    Best-effort guess at the plugin's private data directory under Calibre's
    config tree. Plugins like DeDRM store key files in
    <config>/plugins/<PluginName>/libraryfiles.
    """
    config_dir = os.environ.get('CALIBRE_CONFIG_DIRECTORY') or os.path.expanduser('~/.config/calibre')
    plugins_root = os.path.join(config_dir, 'plugins')
    if not os.path.isdir(plugins_root):
        return None
    # Match the plugin's directory case-insensitively against our module name
    target = PLUGIN_MODULE.lower()
    for name in os.listdir(plugins_root):
        if name.lower() == target:
            return os.path.join(plugins_root, name)
    return None


# Plugin constructor arg names we recognize by intent. The values are
# resolver tags, not the values themselves — actual values come from
# ``_resolve_arg_value`` below, which can do lazy work (e.g. resolve the
# plugin's data directory) only when an arg of that intent appears.
#
# Intent categories:
#   'path'       — the plugin's ZIP path on disk
#   'data_dir'   — the plugin's per-user data directory under Calibre's config
#   'parent'     — a Qt parent widget; None is the right default for QWidget
#   'context'    — a Calibre runtime object (gui/ui/action/db/...) we
#                  substitute with _PluginContextStub
_ARG_NAME_INTENTS = {
    # Path arguments
    'plugin_path':       'path',
    'path':              'path',
    'pluginpath':        'path',
    # Data directory arguments
    'alfdir':            'data_dir',
    'plugin_dir':        'data_dir',
    'plugindir':         'data_dir',
    'maindir':           'data_dir',
    'main_dir':          'data_dir',
    'resource':          'data_dir',
    'resource_dir':      'data_dir',
    'resources_dir':     'data_dir',
    # Qt parent
    'parent':            'parent',
    # Calibre runtime context objects
    'gui':               'context',
    'ui':                'context',
    'plugin':            'context',
    'action':            'context',
    'plugin_action':     'context',
    'pluginaction':      'context',
    'interface_action':  'context',
    'iaction':           'context',
    'db':                'context',
    'database':          'context',
    'model':             'context',
    'view':              'context',
}


def _resolve_arg_value(intent, _state):
    """Materialize the actual value for a recognized argument intent."""
    if intent == 'path':
        return ZIP_PATH
    if intent == 'data_dir':
        if _state['data_dir'] is None:
            _state['data_dir'] = _plugin_data_dir()
        d = _state['data_dir']
        if d:
            libfiles = os.path.join(d, 'libraryfiles')
            return libfiles if os.path.isdir(libfiles) else d
        return None
    if intent == 'parent':
        return None    # Qt widgets accept None as parent
    if intent == 'context':
        return _PluginContextStub()
    return None


def _resolve_init_args(cls):
    """
    Inspect the config class constructor and build kwargs for any required
    parameter, mapping by parameter name.

    Two-pass strategy:
      1. Match each required arg against a known intent (``_ARG_NAME_INTENTS``)
         and materialize the right value (a path, a data dir, a Qt parent,
         or a stub for Calibre context objects).
      2. Anything we don't recognize falls through to a context stub —
         strictly safer than passing ``None``, because most plugin code
         that holds onto an unrecognized arg eventually reads attributes
         off it, and the stub absorbs those reads.

    Plugins with truly unique arg shapes can still misbehave; the
    instantiation fallback chain in ``_instantiate_config`` then tries
    other strategies (one positional path, no args).
    """
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return None

    state = {'data_dir': None}
    kwargs = {}
    for pname, param in sig.parameters.items():
        if pname == 'self' or param.default is not inspect.Parameter.empty:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue

        lname = pname.lower()
        intent = _ARG_NAME_INTENTS.get(lname)
        if intent is None:
            # Substring fallbacks for compound names (e.g. ``my_plugin_path``).
            for known, candidate_intent in _ARG_NAME_INTENTS.items():
                if known in lname:
                    intent = candidate_intent
                    break
        if intent is None:
            # Truly unknown — default to a context stub so attribute access
            # downstream doesn't AttributeError on the constructor body.
            intent = 'context'

        kwargs[pname] = _resolve_arg_value(intent, state)

    return kwargs


def _instantiate_config(cls):
    """
    Try multiple strategies to build a ConfigWidget instance.
    Returns the instance or raises TypeError if no strategy worked.
    """
    # 1. Introspect signature and fill in known names
    kwargs = _resolve_init_args(cls)
    if kwargs:
        try:
            return cls(**kwargs)
        except TypeError:
            pass

    # 2. Single positional with the ZIP path (older plugin convention)
    try:
        return cls(ZIP_PATH)
    except TypeError:
        pass

    # 3. No args
    return cls()


class _MissingWidgetStub:
    """
    Stand-in returned for Qt widget attributes the plugin's ``__init__``
    didn't actually create.

    Why we need this: many Calibre plugins gate widget creation on state.
    DeACSM's ConfigWidget, for example, only creates the activation buttons
    inside an ``if not activated:`` branch. In the desktop UI you'd never see
    the "Create anonymous authorization" button when already activated, so
    its handler never runs. Our web UI shows all buttons unconditionally, so
    a user can click an action that the plugin's own code assumes is
    impossible — and the handler crashes on ``self.button_X.setEnabled(...)``
    in its trailing "refresh the UI" block.

    This stub silently absorbs attribute access, method calls, and truthiness
    checks. The actual side-effects of the action (writing files, hitting
    Adobe's servers, etc.) still happen — only the trailing UI refresh is
    no-op'd, which is exactly the desired behavior in headless mode.
    """
    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __bool__(self):
        return False

    def __iter__(self):
        return iter(())


def _install_missing_widget_fallback(cls):
    """
    Install a class-level ``__getattr__`` that returns a silent stub when an
    attribute is missing. Python only consults ``__getattr__`` after the
    normal lookup chain fails, so legitimate attributes are unaffected.

    We patch the class (not the instance) because ``__getattr__`` must live on
    the type to participate in attribute resolution.
    """
    if getattr(cls, '_cwa_missing_widget_patched', False):
        return

    def __getattr__(self, name):
        # Skip dunder/private names so we don't break Python internals
        # (pickling, copy, debugger introspection, etc.)
        if name.startswith('_'):
            raise AttributeError(name)
        return _MissingWidgetStub()

    cls.__getattr__ = __getattr__
    cls._cwa_missing_widget_patched = True


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
            _install_missing_widget_fallback(cls)
            instance = _instantiate_config(cls)

            fn = getattr(instance, METHOD_NAME, None)
            if not fn:
                return False, 'Method %s not found on %s' % (METHOD_NAME, CONFIG_CLASS), []

            result = fn()

            # Calibre desktop's lifecycle calls Plugin.save_settings(widget)
            # after the user clicks OK on the config dialog — which most
            # plugins implement as ``widget.save_settings()``, persisting
            # whatever in-memory pref dict they've been mutating during
            # the session. We have no equivalent close-event in the web
            # flow: the action subprocess just exits and any unwritten
            # state evaporates. Invoking the widget's own save_settings()
            # here mirrors the desktop behavior so changes the method made
            # to the plugin's pref store (e.g. adding a Kindle serial in
            # ManageKeysDialog) actually reach disk.
            saver = getattr(instance, 'save_settings', None)
            if callable(saver):
                try:
                    saver()
                except Exception:
                    # save_settings failures shouldn't override the
                    # action's outcome — log only via the stderr the
                    # parent thread already captures.
                    pass

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
    patch_qdialog_exec()

    if os.environ.get('CWA_INTROSPECT'):
        introspect_and_exit()
        return

    tmpdir = tempfile.mkdtemp(prefix='cwa_action_')
    try:
        pkg_name = load_plugin_modules(tmpdir)
        success, message, files = execute_method(pkg_name)
    except (Exception, SystemExit) as e:
        success, message, files = False, str(e), []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    write_result(success, message, files)


# ── Introspection mode (Slice 4) ────────────────────────────────────────────
#
# When invoked with CWA_INTROSPECT=1, the runner instantiates the primary
# config widget and writes a snapshot of:
#   * present_fields — names from SCHEMA['fields'] whose attribute actually
#     exists on the live instance (via vars()), so the frontend can filter
#     out widgets the plugin's __init__ chose not to create this time.
#   * values         — current values read live via the appropriate Qt
#     getter for each present field. Used to pre-fill the web form with
#     the same defaults the desktop UI would have shown.
#
# Why ``vars(instance)`` and not ``hasattr``: the _MissingWidgetStub class-
# level __getattr__ that other paths install would make hasattr return True
# for missing attrs. ``vars()`` reads the real instance __dict__, bypassing
# any fallback machinery.

INTROSPECTION_FILE = os.path.join(DIALOG_DIR, 'introspection.json')


def _read_field_value(instance, field_def):
    """
    Best-effort live read of a field's current value from a freshly-init'd
    config widget instance. Returns ``None`` when the widget is missing or
    the getter raises — the caller treats ``None`` as "no live value;
    leave the schema's default in place".
    """
    name = field_def.get('name')
    ftype = field_def.get('type')
    if not name:
        return None
    # For plain fields, read straight off the instance __dict__ (bypasses
    # _MissingWidgetStub). For custom-widget fields (Slice 7), the outer
    # attribute lives in __dict__ but its inner_attr lives one hop deeper
    # — we don't have a stub installed at that level, so a normal attr
    # walk is safe and correct.
    outer = vars(instance).get(name)
    if outer is None:
        return None
    inner_attr = field_def.get('inner_attr')
    widget = getattr(outer, inner_attr, None) if inner_attr else outer
    if widget is None:
        return None
    try:
        if ftype in ('text', 'password'):
            fn = getattr(widget, 'text', None)
            if callable(fn):
                return str(fn())
        elif ftype == 'textarea':
            for getter in ('toPlainText', 'text'):
                fn = getattr(widget, getter, None)
                if callable(fn):
                    return str(fn())
        elif ftype == 'checkbox' or ftype == 'radio':
            fn = getattr(widget, 'isChecked', None)
            if callable(fn):
                return bool(fn())
        elif ftype == 'number' or ftype == 'slider':
            fn = getattr(widget, 'value', None)
            if callable(fn):
                return int(fn())
        elif ftype == 'select':
            fn = getattr(widget, 'currentText', None)
            if callable(fn):
                return str(fn())
        elif ftype == 'list':
            return _read_list_items(widget)
        elif ftype == 'table':
            return _read_table_items(widget)
    except Exception:
        return None
    return None


def introspect_and_exit():
    """
    Runner entry for ``CWA_INTROSPECT=1`` mode. Instantiates the primary
    config class, snapshots which schema fields are actually present on the
    instance (plus their live values), writes the result to
    ``<DIALOG_DIR>/introspection.json``, and returns.

    On any failure (config class not found, init exception) writes a
    payload with an ``error`` key and empty present_fields — the Flask
    side then leaves the static schema unfiltered as a fallback.
    """
    payload = {
        'present_fields': [],
        'values': {},
        'error': None,
    }
    tmpdir = tempfile.mkdtemp(prefix='cwa_introspect_')
    try:
        pkg_name = load_plugin_modules(tmpdir)
        for entry in ('config.py', 'prefs.py', '__init__.py'):
            modname = pkg_name + '.' + entry[:-3]
            mod = sys.modules.get(modname)
            if not mod:
                continue
            cls = getattr(mod, CONFIG_CLASS, None)
            if not cls:
                continue
            try:
                instance = _instantiate_config(cls)
            except Exception as e:
                payload['error'] = 'init failed: ' + str(e)
                break
            schema_fields = SCHEMA.get('fields') or []
            inst_attrs = set(vars(instance).keys())
            for f in schema_fields:
                name = f.get('name')
                if not name or name not in inst_attrs:
                    continue
                payload['present_fields'].append(name)
                live = _read_field_value(instance, f)
                if live is not None:
                    payload['values'][name] = live
            break
        else:
            payload['error'] = 'config class %s not found' % CONFIG_CLASS
    except (Exception, SystemExit) as e:
        payload['error'] = str(e)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        with open(INTROSPECTION_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
    except OSError:
        pass


if __name__ == '__main__':
    main()
