# -*- coding: utf-8 -*-
"""
Calibre plugin manager — installs/removes/configures Calibre plugins and runs
their action buttons via an out-of-process subprocess driven by calibre-debug.

Async pipeline overview:
    1. run_plugin_action_async() — locates the plugin ZIP, asks the AST parser
       which Python method is bound to the requested button, and starts a
       background thread that launches `calibre-debug cps/plugin_runner.py`.
    2. The runner script (see plugin_runner.py) executes the plugin method
       inside Qt's environment, intercepting any Qt dialogs and forwarding
       them as JSON to <dialog_dir>/dialog_request.json.
    3. get_plugin_action_status() polls the session — returns 'running',
       'dialog' (when the runner is waiting for the user), or 'done'.
    4. respond_to_plugin_dialog() unblocks the runner with the user's reply.
    5. The runner writes its final outcome to <dialog_dir>/result.json,
       which the background thread parses to set the session's terminal state.
"""
import subprocess
import re
import os
import shutil
import threading
import uuid
import time
import json
import tempfile
from flask_babel import gettext as _

from . import logger

log = logger.create()


# ── Configuration constants ──────────────────────────────────────────────────

#: Kill the runner subprocess if it hasn't finished after this many seconds.
SUBPROCESS_TIMEOUT = 300

#: Keep finished sessions (and their dialog_dir) in memory this long, so the
#: frontend has time to poll the final status and download any exported files.
SESSION_RETENTION = 300

#: Path to the runner script executed inside the calibre-debug subprocess.
RUNNER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plugin_runner.py')


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_subprocess_env():
    """Build an env dict pointing calibre tools at the right config directory."""
    env = os.environ.copy()
    env['HOME'] = '/config'
    env['CALIBRE_CONFIG_DIRECTORY'] = '/config/.config/calibre'
    return env


def _normalize_plugin_name(name):
    """Lowercase + spaces → underscores. Used for file/dir paths on disk."""
    return name.lower().replace(' ', '_')


def _normalize_plugin_module(name):
    """Reduce a plugin display name to a valid Python module identifier."""
    return re.sub(r'[^a-z0-9_]', '', name.lower().replace(' ', '_').replace('-', '_'))


# ── Plugin list / install / remove / toggle (synchronous calibre-customize) ─

def get_plugins_list():
    """Parse `calibre-customize -l` output into a list of plugin dicts."""
    try:
        result = subprocess.check_output(
            ['calibre-customize', '-l'],
            stderr=subprocess.STDOUT,
            env=_get_subprocess_env()
        )
        output = result.decode('utf-8')
    except subprocess.CalledProcessError as e:
        log.error("calibre-customize -l failed: %s", e)
        return []

    plugins = []
    lines = output.splitlines()
    if not lines:
        return plugins

    header = ""
    for line in lines:
        if line.startswith("Type") and "Name" in line and "Version" in line:
            header = line
            break
    if not header:
        return plugins

    idx_name, idx_version, idx_disabled = header.find("Name"), header.find("Version"), header.find("Disabled")
    if idx_name == -1 or idx_version == -1 or idx_disabled == -1:
        # Fallback to the column offsets used by current calibre-customize output.
        idx_name, idx_version, idx_disabled = 22, 74, 88

    current_plugin = None
    for line in lines:
        if not line.strip() or line.startswith("Type") or line.startswith("---"):
            continue
        if line[0].isspace():
            if current_plugin:
                current_plugin['description'] += line.strip() + " "
            continue

        type_str = line[0:idx_name].strip()
        name_str = line[idx_name:idx_version].strip()
        version_str = line[idx_version:idx_disabled].strip()
        disabled_col = line[idx_disabled:].strip()
        disabled_str = disabled_col.split()[0] if disabled_col else "False"

        if version_str.startswith('(') and version_str.endswith(')'):
            if current_plugin:
                plugins.append(current_plugin)
            clean_version = version_str.strip('()').replace(', ', '.')
            current_plugin = {
                'name': name_str, 'version': clean_version,
                'enabled': disabled_str == 'False', 'type': type_str, 'description': ''
            }
        elif current_plugin:
            current_plugin['description'] += line.strip() + " "

    if current_plugin:
        plugins.append(current_plugin)

    for p in plugins:
        p['normalized_name'] = _normalize_plugin_name(p['name'])

    return plugins


def install_plugin(zip_path):
    try:
        subprocess.check_call(['calibre-customize', '--add', zip_path], env=_get_subprocess_env())
        return True, _("Plugin installed successfully. Please restart the server to apply changes.")
    except Exception as e:
        log.error("Plugin install failed for %s: %s", zip_path, e)
        return False, str(e)


def remove_plugin(plugin_name):
    try:
        subprocess.check_call(['calibre-customize', '--remove-plugin', plugin_name], env=_get_subprocess_env())
        norm_name = _normalize_plugin_name(plugin_name)
        base_dir = '/config/.config/calibre/plugins'
        json_path = os.path.join(base_dir, "%s.json" % norm_name)
        folder_path = os.path.join(base_dir, plugin_name)
        if not os.path.exists(folder_path):
            folder_path = os.path.join(base_dir, norm_name)

        if os.path.exists(json_path):
            os.remove(json_path)
        if os.path.isdir(folder_path):
            shutil.rmtree(folder_path)
        return True, _("Plugin removed successfully. Please restart the server.")
    except Exception as e:
        log.error("Plugin remove failed for %s: %s", plugin_name, e)
        return False, str(e)


def toggle_plugin(plugin_name, enable=True):
    action = '--enable-plugin' if enable else '--disable-plugin'
    try:
        subprocess.check_call(['calibre-customize', action, plugin_name], env=_get_subprocess_env())
        return True, _("Plugin status changed.")
    except Exception as e:
        log.error("Plugin toggle (%s) failed for %s: %s", action, plugin_name, e)
        return False, str(e)


def get_plugin_settings_path(plugin_name):
    """Return the JSON config path for a plugin (the file may not exist yet)."""
    norm_name = _normalize_plugin_name(plugin_name)
    return os.path.join('/config/.config/calibre/plugins', "%s.json" % norm_name)


#: Timeout for the brief introspection subprocess. Init should be fast —
#: anything past ~20s on a real plugin is unhealthy and we'd rather degrade
#: to the static schema than block the modal-open path indefinitely.
INTROSPECT_TIMEOUT = 30


def introspect_plugin_live_state(plugin_name, schema_dict):
    """
    Run the plugin's ``ConfigWidget.__init__`` in an isolated subprocess and
    snapshot which attributes really exist plus their current values.

    Returns a dict::

        { 'present_fields': [str, ...], 'values': { name: value, ... } }

    On any failure (subprocess crash, missing config class, write error)
    returns ``{'present_fields': None}`` to signal "no usable result;
    callers should keep the static schema as-is". This is a soft optimization
    layer — the modal must still render even when introspection fails.

    The subprocess is the same ``cps/plugin_runner.py`` script invoked by
    the action pipeline, just gated by ``CWA_INTROSPECT=1`` to take a
    different code path (instantiate, snapshot, exit — no Qt event loop,
    no IPC dialogs).
    """
    try:
        zip_path = get_plugin_zip_path(plugin_name)
    except Exception as e:
        log.warning("Introspection: ZIP not found for '%s': %s", plugin_name, e)
        return {'present_fields': None}

    config_class = (
        (schema_dict.get('config_class') or 'ConfigWidget').split(',')[0].strip()
        or 'ConfigWidget'
    )
    plugin_module = _normalize_plugin_module(plugin_name)
    dialog_dir = tempfile.mkdtemp(prefix='cwa_introspect_')
    schema_path = os.path.join(dialog_dir, 'schema.json')
    introspection_path = os.path.join(dialog_dir, 'introspection.json')

    try:
        with open(schema_path, 'w', encoding='utf-8') as f:
            json.dump(schema_dict, f)

        env = _get_subprocess_env()
        env['QT_QPA_PLATFORM'] = 'offscreen'
        env['PYTHONUNBUFFERED'] = '1'
        env['CWA_ZIP']         = zip_path
        env['CWA_CLASS']       = config_class
        # METHOD is unused by the introspection path but plugin_runner.py
        # reads it at module load before we get to dispatch; supply a stub.
        env['CWA_METHOD']      = ''
        env['CWA_MODULE']      = plugin_module
        env['CWA_DIALOG_DIR']  = dialog_dir
        env['CWA_SCHEMA']      = schema_path
        env['CWA_INTROSPECT']  = '1'

        try:
            proc = subprocess.run(
                ['calibre-debug', RUNNER_SCRIPT],
                env=env, capture_output=True, text=True,
                timeout=INTROSPECT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log.warning("Introspection timed out for '%s' after %ds", plugin_name, INTROSPECT_TIMEOUT)
            return {'present_fields': None}
        except FileNotFoundError as e:
            log.warning("Introspection: calibre-debug not available: %s", e)
            return {'present_fields': None}

        if not os.path.exists(introspection_path):
            log.warning(
                "Introspection: no result file from '%s'; stderr tail: %s",
                plugin_name, (proc.stderr or '')[-300:].strip() or '(empty)',
            )
            return {'present_fields': None}

        try:
            with open(introspection_path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("Introspection: could not read result for '%s': %s", plugin_name, e)
            return {'present_fields': None}

        if data.get('error'):
            log.info("Introspection for '%s' reported: %s", plugin_name, data['error'])
            # Even with an error the payload may carry partial present_fields;
            # accept it if non-empty, otherwise fall through to the static schema.
            if not data.get('present_fields'):
                return {'present_fields': None}

        return {
            'present_fields': list(data.get('present_fields') or []),
            'values': dict(data.get('values') or {}),
        }
    finally:
        shutil.rmtree(dialog_dir, ignore_errors=True)


def get_plugin_zip_path(plugin_name):
    """
    Resolve the on-disk ZIP path for a plugin. Searches the user's calibre
    config directory first, falling back to the bundled plugins shipped with
    the repository. Raises FileNotFoundError if nothing matches.
    """
    norm = _normalize_plugin_name(plugin_name)
    search = [
        ('/config/.config/calibre/plugins', plugin_name),
        ('/config/.config/calibre/plugins', norm),
        ('/config/plugins', plugin_name),
        ('/config/plugins', norm),
    ]
    for base, name in search:
        path = os.path.join(base, "%s.zip" % name)
        if os.path.exists(path):
            return path
    raise FileNotFoundError("ZIP not found for plugin '%s'" % plugin_name)


# ── Async plugin action pipeline ────────────────────────────────────────────

_active_sessions = {}
_sessions_lock = threading.Lock()


def _cleanup_orphan_session_dirs():
    """
    Remove leftover cwa_sess_* and cwa_action_* directories from previous runs.
    Called once on module import — best effort; failures are logged but
    non-fatal because the worst case is a few extra MB in /tmp.
    """
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if name.startswith('cwa_sess_') or name.startswith('cwa_action_'):
                path = os.path.join(tmp, name)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
    except OSError as e:
        log.warning("Could not clean orphan plugin session dirs in %s: %s", tmp, e)


_cleanup_orphan_session_dirs()


def _delayed_session_cleanup(session_id):
    """After SESSION_RETENTION seconds, drop the session and wipe its dialog_dir."""
    time.sleep(SESSION_RETENTION)
    with _sessions_lock:
        session = _active_sessions.pop(session_id, None)
    if session:
        dd = session.get('dialog_dir')
        if dd and os.path.exists(dd):
            shutil.rmtree(dd, ignore_errors=True)


def _read_result_file(dialog_dir):
    """Parse <dialog_dir>/result.json. Returns (success, message, files) or None."""
    result_path = os.path.join(dialog_dir, 'result.json')
    if not os.path.exists(result_path):
        return None
    try:
        with open(result_path, encoding='utf-8') as f:
            data = json.load(f)
        return (
            bool(data.get('success')),
            str(data.get('message', '')),
            list(data.get('files') or []),
        )
    except Exception as e:
        log.error("Failed to read result.json at %s: %s", result_path, e)
        return None


def _run_session_thread(session_id, zip_path, config_class, method_name, plugin_name, schema_dict=None):
    """
    Background thread that launches the runner subprocess, waits for it,
    parses result.json, and updates the session state.

    ``schema_dict`` is an optional ``asdict(PluginSchema)`` dump. When
    provided, it is serialized to ``<dialog_dir>/schema.json`` and the path
    is exposed to the runner via ``CWA_SCHEMA``. The runner reads it to
    learn which sub-dialogs it can render via web IPC vs. fall back on the
    "not available in web mode" handler.
    """
    plugin_module = _normalize_plugin_module(plugin_name)
    dialog_dir = tempfile.mkdtemp(prefix='cwa_sess_')

    env = _get_subprocess_env()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['PYTHONUNBUFFERED'] = '1'
    env['CWA_ZIP']        = zip_path
    env['CWA_CLASS']      = config_class
    env['CWA_METHOD']     = method_name
    env['CWA_MODULE']     = plugin_module
    env['CWA_DIALOG_DIR'] = dialog_dir

    if schema_dict is not None:
        schema_path = os.path.join(dialog_dir, 'schema.json')
        try:
            with open(schema_path, 'w', encoding='utf-8') as f:
                json.dump(schema_dict, f)
            env['CWA_SCHEMA'] = schema_path
        except OSError as e:
            log.warning("Could not write schema.json for session %s: %s", session_id, e)

    try:
        proc = subprocess.Popen(
            ['calibre-debug', RUNNER_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env
        )
    except Exception as e:
        log.error("Failed to start plugin runner for %s: %s", plugin_name, e)
        with _sessions_lock:
            _active_sessions[session_id].update({
                'status': 'done', 'success': False, 'message': str(e),
                'dialog_dir': dialog_dir,
            })
        threading.Thread(target=_delayed_session_cleanup, args=(session_id,), daemon=True).start()
        return

    with _sessions_lock:
        _active_sessions[session_id].update({
            'dialog_dir': dialog_dir,
            'proc': proc,
            'status': 'running',
        })

    try:
        _, stderr = proc.communicate(timeout=SUBPROCESS_TIMEOUT)

        parsed = _read_result_file(dialog_dir)
        if parsed is not None:
            success, message, files = parsed
        else:
            # The runner exited without writing result.json — interpret as a crash.
            success = False
            files = []
            stderr_tail = (stderr or '')[-500:].strip()
            log.warning("Plugin runner for session %s exited without result.json. stderr tail: %s",
                        session_id, stderr_tail or '(empty)')
            if stderr_tail:
                message = _("Action crashed: %(err)s", err=stderr_tail)
            else:
                message = _("Action exited without producing a result")

        with _sessions_lock:
            _active_sessions[session_id].update({
                'status': 'done', 'success': success,
                'message': message, 'files': files,
            })

    except subprocess.TimeoutExpired:
        log.warning("Plugin action session %s timed out after %ds", session_id, SUBPROCESS_TIMEOUT)
        try:
            proc.kill()
        except Exception:
            pass
        with _sessions_lock:
            _active_sessions[session_id].update({
                'status': 'done', 'success': False,
                'message': _("Action timed out after %(t)d seconds", t=SUBPROCESS_TIMEOUT),
            })
    except Exception as e:
        log.exception("Plugin action session %s failed unexpectedly", session_id)
        with _sessions_lock:
            _active_sessions[session_id].update({
                'status': 'done', 'success': False, 'message': str(e),
            })
    finally:
        threading.Thread(target=_delayed_session_cleanup, args=(session_id,), daemon=True).start()


def run_plugin_action_async(plugin_name, action_name):
    """
    Look up the Python method bound to the requested button via the AST parser
    and launch the runner in a background thread. Returns a session_id the
    caller can poll via get_plugin_action_status().

    Raises NotImplementedError if the AST parser could not find a method linked
    to the button (e.g. the plugin uses a lambda or signal we don't recognize).
    """
    from .calibre_plugin_parser import parse_plugin_zip
    from dataclasses import asdict

    zip_path = get_plugin_zip_path(plugin_name)
    schema = parse_plugin_zip(zip_path)

    method_name = None
    for f in schema.fields:
        if f.name == action_name and f.type == 'button':
            method_name = getattr(f, 'connected_method', None)
            break

    if not method_name:
        raise NotImplementedError(
            "No executable handler found for action '%s' on plugin '%s'" % (action_name, plugin_name)
        )

    config_class = schema.config_class.split(',')[0].strip() if schema.config_class else 'ConfigWidget'
    session_id = uuid.uuid4().hex

    with _sessions_lock:
        _active_sessions[session_id] = {
            'status': 'starting',
            'plugin_name': plugin_name,
            'action_name': action_name,
            'dialog_dir': None,
            'proc': None,
            'created_at': time.time(),
        }

    schema_dict = asdict(schema)
    threading.Thread(
        target=_run_session_thread,
        args=(session_id, zip_path, config_class, method_name, plugin_name, schema_dict),
        daemon=True
    ).start()

    log.info("Plugin action started: plugin='%s' action='%s' method='%s' session=%s",
             plugin_name, action_name, method_name, session_id)
    return session_id


def get_plugin_action_status(session_id):
    """Return a status dict for the given session: running / dialog / done / not_found."""
    with _sessions_lock:
        session = _active_sessions.get(session_id)

    if not session:
        return {'status': 'not_found'}

    status = session.get('status', 'running')
    dialog_dir = session.get('dialog_dir')

    if status == 'done':
        result = {
            'status': 'done',
            'success': session.get('success', False),
            'message': session.get('message', ''),
        }
        if session.get('files'):
            result['files'] = session['files']
        return result

    if dialog_dir:
        ready_file = os.path.join(dialog_dir, 'dialog_ready')
        req_file = os.path.join(dialog_dir, 'dialog_request.json')
        if os.path.exists(ready_file) and os.path.exists(req_file):
            try:
                with open(req_file, encoding='utf-8') as f:
                    return {'status': 'dialog', 'dialog': json.load(f)}
            except Exception as e:
                log.warning("Could not read dialog request for session %s: %s", session_id, e)

    return {'status': 'running'}


def respond_to_plugin_dialog(session_id, response_data):
    """Write the user's reply for a pending dialog and unblock the runner."""
    with _sessions_lock:
        session = _active_sessions.get(session_id)

    if not session:
        raise ValueError("Session %s not found" % session_id)

    dialog_dir = session.get('dialog_dir')
    if not dialog_dir:
        raise ValueError("No dialog pending for this session")

    ready_file = os.path.join(dialog_dir, 'dialog_ready')
    if not os.path.exists(ready_file):
        raise ValueError("No dialog awaiting response for this session")

    with open(os.path.join(dialog_dir, 'dialog_response.json'), 'w', encoding='utf-8') as f:
        json.dump(response_data, f)

    try:
        os.unlink(ready_file)
    except OSError:
        pass

    open(os.path.join(dialog_dir, 'response_ready'), 'w').close()


def cancel_plugin_action(session_id):
    """
    Kill the subprocess and clean up the session immediately.
    Returns True if a session was cancelled, False if it didn't exist.
    """
    with _sessions_lock:
        session = _active_sessions.get(session_id)
    if not session:
        return False

    proc = session.get('proc')
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except Exception as e:
            log.warning("Could not kill plugin subprocess for session %s: %s", session_id, e)

    with _sessions_lock:
        session = _active_sessions.pop(session_id, None)

    if session:
        dd = session.get('dialog_dir')
        if dd and os.path.exists(dd):
            shutil.rmtree(dd, ignore_errors=True)

    log.info("Plugin action session %s cancelled", session_id)
    return True


def get_plugin_action_file(session_id, filename):
    """
    Resolve an exported file path for a session, with traversal protection.
    Returns the absolute path if the file is inside the session's exports dir,
    otherwise None.
    """
    with _sessions_lock:
        session = _active_sessions.get(session_id)
    if not session:
        return None
    dialog_dir = session.get('dialog_dir')
    if not dialog_dir:
        return None
    exports_dir = os.path.join(dialog_dir, 'exports')
    path = os.path.realpath(os.path.join(exports_dir, filename))
    real_exports = os.path.realpath(exports_dir)
    if os.path.isfile(path) and path.startswith(real_exports + os.sep):
        return path
    return None
