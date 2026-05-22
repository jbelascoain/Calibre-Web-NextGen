# -*- coding: utf-8 -*-
import subprocess
import re
import os
import ast
import shutil
import zipfile
import threading
import uuid
import time
import json
import tempfile
from flask_babel import gettext as _

def _get_subprocess_env():
    env = os.environ.copy()
    env['HOME'] = '/config'
    env['CALIBRE_CONFIG_DIRECTORY'] = '/config/.config/calibre'
    return env

def get_plugins_list():
    try:
        result = subprocess.check_output(
            ['calibre-customize', '-l'], 
            stderr=subprocess.STDOUT,
            env=_get_subprocess_env()
        )
        output = result.decode('utf-8')
    except subprocess.CalledProcessError:
        return []

    plugins = []
    lines = output.splitlines()
    if not lines: return plugins

    header = ""
    for line in lines:
        if line.startswith("Type") and "Name" in line and "Version" in line:
            header = line
            break
    if not header: return plugins
        
    idx_name, idx_version, idx_disabled = header.find("Name"), header.find("Version"), header.find("Disabled")
    if idx_name == -1 or idx_version == -1 or idx_disabled == -1:
        idx_name, idx_version, idx_disabled = 22, 74, 88

    current_plugin = None
    for line in lines:
        if not line.strip() or line.startswith("Type") or line.startswith("---"): continue
        if line[0].isspace():
            if current_plugin: current_plugin['description'] += line.strip() + " "
            continue
            
        type_str = line[0:idx_name].strip()
        name_str = line[idx_name:idx_version].strip()
        version_str = line[idx_version:idx_disabled].strip()
        disabled_col = line[idx_disabled:].strip()
        disabled_str = disabled_col.split()[0] if disabled_col else "False"
        
        if version_str.startswith('(') and version_str.endswith(')'):
            if current_plugin: plugins.append(current_plugin)
            clean_version = version_str.strip('()').replace(', ', '.')
            current_plugin = {
                'name': name_str, 'version': clean_version,
                'enabled': disabled_str == 'False', 'type': type_str, 'description': ''
            }
        elif current_plugin:
            current_plugin['description'] += line.strip() + " "

    if current_plugin: plugins.append(current_plugin)
    
    # Normalize plugin names to ensure consistency between list and config paths
    # Some plugins appear as "KFX Input" in the list but are stored as "kfx_input" or similar
    for p in plugins:
        p['normalized_name'] = p['name'].lower().replace(' ', '_')
        
    return plugins

def install_plugin(zip_path):
    try:
        subprocess.check_call(['calibre-customize', '--add', zip_path], env=_get_subprocess_env())
        return True, _("Plugin installed successfully. Please restart the server to apply changes.")
    except Exception as e:
        return False, str(e)

def remove_plugin(plugin_name):
    try:
        subprocess.check_call(['calibre-customize', '--remove-plugin', plugin_name], env=_get_subprocess_env())
        norm_name = plugin_name.lower().replace(' ', '_')
        base_dir = '/config/.config/calibre/plugins'
        json_path = os.path.join(base_dir, f"{norm_name}.json")
        folder_path = os.path.join(base_dir, plugin_name)
        if not os.path.exists(folder_path):
            folder_path = os.path.join(base_dir, norm_name)
            
        if os.path.exists(json_path): os.remove(json_path)
        if os.path.isdir(folder_path): shutil.rmtree(folder_path)
        return True, _("Plugin removed successfully. Please restart the server.")
    except Exception as e:
        return False, str(e)

def toggle_plugin(plugin_name, enable=True):
    action = '--enable-plugin' if enable else '--disable-plugin'
    try:
        subprocess.check_call(['calibre-customize', action, plugin_name], env=_get_subprocess_env())
        return True, _("Plugin status changed.")
    except Exception as e:
        return False, str(e)

def prettify_key(key):
    return key.replace('_', ' ').replace('-', ' ').title()

def get_plugin_settings_path(plugin_name):
    # Normalize name for path lookup
    norm_name = plugin_name.lower().replace(' ', '_')
    
    base_config_dir = '/config/.config/calibre/plugins'
    return os.path.join(base_config_dir, f"{norm_name}.json")

def get_plugin_defaults(plugin_name):
    values, labels, help_text, groups = {}, {}, {}, {}
    norm_name = plugin_name.lower().replace(' ', '_')
    base_dir = '/config/.config/calibre/plugins'
    
    # Try to find the plugin directory to analyze its source code
    # Calibre plugins are often extracted into folders with the same name as the plugin
    plugin_folder = os.path.join(base_dir, plugin_name)
    if not os.path.exists(plugin_folder):
        plugin_folder = os.path.join(base_dir, norm_name)
    
    def analyze_source(content):
        # 1. VALORES (defaults)
        # Patrón A: defaults['key'] = value o prefs['key'] = value
        matches = re.findall(r"(?:defaults|prefs)\[['\"](.+?)['\"]\]\s*=\s*([^#\n\r]+)", content)
        for key, val_str in matches:
            try: values[key] = ast.literal_eval(val_str.strip())
            except: continue
        
        # Patrón B: self.option_name = value (común en clases de configuración)
        class_matches = re.findall(r"self\.([a-zA-Z0-9_]+)\s*=\s*([^#\n\r]+)", content)
        for key, val_str in class_matches:
            if key not in values:
                try: values[key] = ast.literal_eval(val_str.strip())
                except: continue
        
        # Patrón C: Definiciones de diccionarios de configuración globales
        global_prefs = re.findall(r"([a-zA-Z0-9_]+_PREFS)\s*=\s*(\{.*?\})", content, re.DOTALL)
        for pref_name, pref_val in global_prefs:
            try:
                data = ast.literal_eval(pref_val)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k not in values: values[k] = v
            except: continue

        # Patrón D: JSONConfig defaults (Específico de plugins como KFX Output)
        json_defaults = re.findall(r"plugin_config\.defaults\[([a-zA-Z0-9_]+)\]\s*=\s*([^#\n\r]+)", content)
        for key, val_str in json_defaults:
            try:
                try:
                    values[key] = ast.literal_eval(val_str.strip())
                except:
                    var_match = re.search(rf'{key}\s*=\s*[\'"](.+?)[\'"]', content)
                    if var_match:
                        values[key] = var_match.group(1)
                    else:
                        values[key] = False
            except: continue

        # 2. DETECCIÓN DE ETIQUETAS Y GRUPOS (Estrategia de Proximidad y Widget)
        current_group = "General Settings"
        lines = content.split('\n')
        
        for line in lines:
            # Detectar grupos de configuración (QGroupBox)
            group_match = re.search(r'QGroupBox\s*\(\s*_\([\'"](.+?)[\'"]\)\s*\)', line)
            if not group_match:
                group_match = re.search(r'QGroupBox\s*\(\s*[\'"](.+?)[\'"]\s*\)', line)
            if group_match:
                current_group = group_match.group(1)
            
            # Vincular variables con etiquetas legibles
            for key in values.keys():
                # Patrón: self.key = QCheckBox("Label") o self.key = QLineEdit("Label")
                widget_match = re.search(rf'self\.{re.escape(key)}\s*=\s*(?:Q\w+)\s*\(\s*_\([\'"](.+?)[\'"]\)', line)
                if not widget_match:
                    widget_match = re.search(rf'self\.{re.escape(key)}\s*=\s*(?:Q\w+)\s*\(\s*[\'"](.+?)[\'"]\)', line)
                
                if widget_match:
                    labels[key] = widget_match.group(1).replace('&', '')
                    groups[key] = current_group
                    continue

                # Patrón: .setText(_("Label")) o .setLabel(_("Label"))
                text_match = re.search(rf'{re.escape(key)}\.(?:setText|setLabel)\s*\(\s*_\([\'"](.+?)[\'"]\)', line)
                if not text_match:
                    text_match = re.search(rf'{re.escape(key)}\.(?:setText|setLabel)\s*\(\s*[\'"](.+?)[\'"]\)', line)
                
                if text_match:
                    labels[key] = text_match.group(1).replace('&', '')
                    groups[key] = current_group

        # Búsqueda por Proximidad (Si la etiqueta no está en la misma línea)
        for key in values.keys():
            if key not in labels:
                for i, line in enumerate(lines):
                    if key in line:
                        block = "\n".join(lines[i:i+5])
                        trans_match = re.search(r'_\([\'"](.+?)[\'"]\)', block)
                        if trans_match:
                            label_text = trans_match.group(1)
                            if label_text.lower() != key.lower():
                                labels[key] = label_text.replace('&', '')
                                groups[key] = current_group
                                break

        # Fallback final: Si la etiqueta sigue vacía, usar prettify_key
        for key in values.keys():
            if key not in labels:
                labels[key] = prettify_key(key)
            if key not in groups:
                groups[key] = "General Settings"

        # 3. TOOLTIPS (Ayuda)
        for match in re.finditer(r'\.setToolTip\s*\(\s*_\([\'"](.+?)[\'"]\)\s*\)', content):
            tip = match.group(1)
            scope = content[max(0, match.start()-100):match.start()]
            var = re.search(r'\.([a-zA-Z0-9_]+)', scope)
            if var and var.group(1) in values:
                help_text[var.group(1)] = tip

    # Search for configuration files in the plugin folder
    if os.path.isdir(plugin_folder):
        for root, dirs, files in os.walk(plugin_folder):
            for file in files:
                if (file in ['prefs.py', 'config.py', '__init__.py'] or 'prefs' in file or 'config' in file) and file.endswith('.py'):
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                            analyze_source(f.read())
                    except Exception:
                        continue
    else:
        # If the folder doesn't exist, the plugin might be just a ZIP file in the plugins directory
        # We try both the original name and the normalized name for the ZIP file
        for zip_name in [f"{plugin_name}.zip", f"{norm_name}.zip"]:
            zip_path = os.path.join(base_dir, zip_name)
            if os.path.exists(zip_path):
                try:
                    with zipfile.ZipFile(zip_path, 'r') as z:
                        for name in z.namelist():
                            if (any(x in name for x in ['prefs.py', 'config.py', '__init__.py']) or 'prefs' in name or 'config' in name) and name.endswith('.py'):
                                with z.open(name) as f:
                                    analyze_source(f.read().decode('utf-8', errors='ignore'))
                except Exception:
                    pass

    return {"values": values, "labels": labels, "help": help_text, "groups": groups}


def get_plugin_zip_path(plugin_name):
    norm = plugin_name.lower().replace(' ', '_')
    search = [
        ('/config/.config/calibre/plugins', plugin_name),
        ('/config/.config/calibre/plugins', norm),
        ('/config/plugins', plugin_name),
        ('/config/plugins', norm),
    ]
    for base, name in search:
        path = os.path.join(base, f"{name}.zip")
        if os.path.exists(path):
            return path
    raise FileNotFoundError(f"ZIP not found for plugin '{plugin_name}'")


_active_sessions = {}
_sessions_lock = threading.Lock()


def _build_subprocess_script(zip_path, config_class, method_name, plugin_module, dialog_dir):
    # calibre-debug -c runs exec(code, globals, locals) with SEPARATE dicts.
    # Top-level `def` goes into locals; function __globals__ is globals.
    # So functions cannot see each other via LOAD_GLOBAL.
    # Fix: wrap everything in _run() so all names are accessible via closures.
    lines = [
        "def _run():",
        "    import sys, os, zipfile, tempfile, importlib.util, types, json, time, shutil",
        f"    _zip={repr(zip_path)}",
        f"    _cls_name={repr(config_class)}",
        f"    _method={repr(method_name)}",
        f"    _plugin_module={repr(plugin_module)}",
        f"    _dialog_dir={repr(dialog_dir)}",
        "    _exports_dir=os.path.join(_dialog_dir,'exports')",
        "    os.makedirs(_exports_dir,exist_ok=True)",
        # IPC helper: write dialog spec and wait for web user response
        "    def _cwa_wait(spec):",
        "        req=os.path.join(_dialog_dir,'dialog_request.json')",
        "        rdy=os.path.join(_dialog_dir,'dialog_ready')",
        "        rsp=os.path.join(_dialog_dir,'dialog_response.json')",
        "        rrdy=os.path.join(_dialog_dir,'response_ready')",
        "        with open(req,'w') as f: json.dump(spec,f)",
        "        open(rdy,'w').close()",
        "        dl=time.time()+120",
        "        while time.time()<dl:",
        "            if os.path.exists(rrdy):",
        "                with open(rsp) as f: r=json.load(f)",
        "                try: os.unlink(rrdy)",
        "                except: pass",
        "                try: os.unlink(rsp)",
        "                except: pass",
        "                return r",
        "            time.sleep(0.1)",
        "        return {'cancelled':True,'timeout':True}",
        # QApplication (required before creating any Qt widget)
        "    try:",
        "        from qt.core import QApplication",
        "        _app=QApplication.instance() or QApplication([])",
        "    except Exception:",
        "        try:",
        "            from PyQt5.Qt import QApplication",
        "            _app=QApplication.instance() or QApplication([])",
        "        except: pass",
        # Patch calibre.gui2 BEFORE loading plugin (plugin's top-level imports bind at load time)
        "    try:",
        "        import calibre.gui2 as _g2",
        "        def _cwa_warn(p,t,m,*a,**k): _cwa_wait({'type':'warning','title':str(t),'msg':str(m)}); return True",
        "        def _cwa_err(p,t,m,*a,**k): _cwa_wait({'type':'error','title':str(t),'msg':str(m)}); return True",
        "        def _cwa_info(p,t,m,*a,**k): _cwa_wait({'type':'info','title':str(t),'msg':str(m)}); return True",
        "        def _cwa_q(p,t,m,*a,yes_text='Yes',no_text='No',**k):",
        "            return _cwa_wait({'type':'question','title':str(t),'msg':str(m),'yes':str(yes_text),'no':str(no_text)}).get('value',False)",
        "        def _cwa_save(p,t,*a,initial_path=None,suggested_filename=None,name='',**k):",
        "            fn=suggested_filename or name or (os.path.basename(initial_path) if initial_path else 'export.bin')",
        "            sp=os.path.join(_exports_dir,fn)",
        "            r=_cwa_wait({'type':'save_file','title':str(t),'filename':fn})",
        "            return None if r.get('cancelled') else sp",
        "        def _cwa_open_url(url,*a,**k): _cwa_wait({'type':'open_url','url':str(url)})",
        "        _g2.warning_dialog=_cwa_warn; _g2.error_dialog=_cwa_err; _g2.info_dialog=_cwa_info",
        "        _g2.question_dialog=_cwa_q; _g2.open_url=_cwa_open_url",
        "        _g2.choose_save_file=_cwa_save; _g2.choose_files=lambda p,t,*a,**k: []",
        "        try:",
        "            import calibre.gui2.choose_files as _cf",
        "            _cf.choose_save_file=_cwa_save; _cf.choose_files=lambda p,t,*a,**k: []",
        "        except: pass",
        "    except: pass",
        # Patch QInputDialog and QFileDialog with pure-Python replacements
        "    try:",
        "        import qt.core as _qtc",
        "        _w=_cwa_wait",
        "        _ed=_exports_dir",
        "        class _CWAI:",
        "            @staticmethod",
        "            def getText(p,t,l,*a,text='',**k):",
        "                r=_w({'type':'text_input','title':str(t),'label':str(l),'default':str(text)})",
        "                return ('',False) if r.get('cancelled') else (r.get('value',''),True)",
        "            @staticmethod",
        "            def getItem(p,t,l,items,current=0,*a,**k):",
        "                il=list(items)",
        "                r=_w({'type':'select','title':str(t),'label':str(l),'items':il,'current':int(current)})",
        "                return ('',False) if r.get('cancelled') else (r.get('value',il[0] if il else ''),True)",
        "        class _CWAFD:",
        "            @staticmethod",
        "            def getSaveFileName(p,t='Save',directory='',filter='',*a,**k):",
        "                fn=os.path.basename(directory) or 'export.bin'",
        "                sp=os.path.join(_ed,fn)",
        "                r=_w({'type':'save_file','title':str(t),'filename':fn})",
        "                return ('',filter) if r.get('cancelled') else (sp,filter)",
        "            @staticmethod",
        "            def getOpenFileName(*a,**k): return ('','')",
        "            @staticmethod",
        "            def getExistingDirectory(*a,**k): return ''",
        "        _qtc.QInputDialog=_CWAI; _qtc.QFileDialog=_CWAFD",
        "    except: pass",
        # Same patches for plugins that import directly from PyQt5/PyQt6
        "    try:",
        "        import PyQt5.QtWidgets as _pqw",
        "        _pqw.QInputDialog=_CWAI; _pqw.QFileDialog=_CWAFD",
        "    except: pass",
        "    try:",
        "        import PyQt6.QtWidgets as _pqw6",
        "        _pqw6.QInputDialog=_CWAI; _pqw6.QFileDialog=_CWAFD",
        "    except: pass",
        # Patch QMessageBox (used by plugins that call it directly instead of calibre wrappers)
        "    try:",
        "        import qt.core as _qtc2",
        "        _w2=_cwa_wait",
        "        class _CWAMB:",
        "            Yes=16384; No=65536; Ok=1024; Cancel=4194304; StandardButton=type('SB',(),{'Yes':16384,'No':65536,'Ok':1024,'Cancel':4194304})()",
        "            @staticmethod",
        "            def question(p,t,m,*a,**k):",
        "                return _CWAMB.Yes if _w2({'type':'question','title':str(t),'msg':str(m)}).get('value') else _CWAMB.No",
        "            @staticmethod",
        "            def warning(p,t,m,*a,**k):",
        "                _w2({'type':'warning','title':str(t),'msg':str(m)}); return _CWAMB.Ok",
        "            @staticmethod",
        "            def information(p,t,m,*a,**k):",
        "                _w2({'type':'info','title':str(t),'msg':str(m)}); return _CWAMB.Ok",
        "            @staticmethod",
        "            def critical(p,t,m,*a,**k):",
        "                _w2({'type':'error','title':str(t),'msg':str(m)}); return _CWAMB.Ok",
        "            @classmethod",
        "            def exec_(cls): return cls.Ok",
        "            def __call__(self,*a,**k): return self",
        "        _qtc2.QMessageBox=_CWAMB",
        "    except: pass",
        "    try:",
        "        import PyQt5.QtWidgets as _pqw2",
        "        _pqw2.QMessageBox=_CWAMB",
        "    except: pass",
        # Load plugin and execute method
        "    _pkg='calibre_plugins.'+_plugin_module",
        "    tmpdir=tempfile.mkdtemp(prefix='cwa_action_')",
        "    try:",
        "        with zipfile.ZipFile(_zip) as z: z.extractall(tmpdir)",
        "        sys.path.insert(0,tmpdir)",
        "        for _zf in sorted(os.listdir(tmpdir)):",
        "            if not _zf.endswith('.zip'): continue",
        "            _stem=_zf[:-4]; _nd=os.path.join(tmpdir,'_lib_'+_stem)",
        "            os.makedirs(_nd,exist_ok=True)",
        "            with zipfile.ZipFile(os.path.join(tmpdir,_zf)) as _nz: _nz.extractall(_nd)",
        "            _inner=os.path.join(_nd,_stem)",
        "            sys.path.insert(0,_inner if os.path.isdir(_inner) else _nd)",
        "        if 'calibre_plugins' not in sys.modules: sys.modules['calibre_plugins']=types.ModuleType('calibre_plugins')",
        "        if _pkg not in sys.modules:",
        "            _pm=types.ModuleType(_pkg); _pm.__path__=[tmpdir]; _pm.__package__=_pkg",
        "            sys.modules[_pkg]=_pm",
        "        for _pyf in ['__init__.py','prefs.py','config.py']:",
        "            _fpath=os.path.join(tmpdir,_pyf)",
        "            if not os.path.exists(_fpath): continue",
        "            _modn=_pkg+'.'+_pyf[:-3]",
        "            if _modn in sys.modules: continue",
        "            _sp=importlib.util.spec_from_file_location(_modn,_fpath)",
        "            _m=importlib.util.module_from_spec(_sp); _m.__package__=_pkg",
        "            sys.modules[_modn]=_m",
        "            try: _sp.loader.exec_module(_m)",
        "            except (Exception,SystemExit): sys.modules.pop(_modn,None)",
        "        loaded=False",
        "        for fname in ['config.py','prefs.py','__init__.py']:",
        "            _modn=_pkg+'.'+fname[:-3]",
        "            mod=sys.modules.get(_modn)",
        "            if not mod: continue",
        "            cls=getattr(mod,_cls_name,None)",
        "            if not cls: continue",
        "            try:",
        "                try: obj=cls(_zip)",
        "                except TypeError: obj=cls()",
        "                fn=getattr(obj,_method,None)",
        "                if fn:",
        "                    result=fn()",
        "                    exports=os.listdir(_exports_dir)",
        "                    if exports: print('CWA_SUCCESS:'+str(result if result is not None else 'Done')+'|EXPORTS:'+','.join(exports))",
        "                    else: print('CWA_SUCCESS:'+str(result if result is not None else 'Done'))",
        "                else: print('CWA_ERROR:Method '+_method+' not found on '+_cls_name)",
        "            except Exception as e: print('CWA_ERROR:'+str(e))",
        "            loaded=True; break",
        "        if not loaded: print('CWA_ERROR:Config class not found')",
        "    except (Exception,SystemExit) as e: print('CWA_ERROR:'+str(e))",
        "    finally:",
        "        shutil.rmtree(tmpdir,ignore_errors=True)",
        # Top-level call — found via LOAD_NAME (locals lookup) from exec scope
        "_run()",
    ]
    return "\n".join(lines)


def _run_session_thread(session_id, zip_path, config_class, method_name, plugin_name):
    plugin_module = re.sub(r'[^a-z0-9_]', '', plugin_name.lower().replace(' ', '_').replace('-', '_'))
    dialog_dir = tempfile.mkdtemp(prefix='cwa_sess_')

    with _sessions_lock:
        _active_sessions[session_id]['dialog_dir'] = dialog_dir
        _active_sessions[session_id]['status'] = 'running'

    script = _build_subprocess_script(zip_path, config_class, method_name, plugin_module, dialog_dir)
    env = _get_subprocess_env()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['PYTHONUNBUFFERED'] = '1'

    try:
        proc = subprocess.Popen(
            ['calibre-debug', '-c', script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env
        )
        stdout, stderr = proc.communicate(timeout=300)
        output = (stdout or '') + (stderr or '')

        success = False
        message = _("Action completed without result")
        files = []

        for line in output.splitlines():
            if line.startswith('CWA_SUCCESS:'):
                msg = line[len('CWA_SUCCESS:'):]
                if '|EXPORTS:' in msg:
                    parts = msg.split('|EXPORTS:', 1)
                    msg, files = parts[0], [f.strip() for f in parts[1].split(',') if f.strip()]
                success, message = True, msg
                break
            if line.startswith('CWA_ERROR:'):
                message = line[len('CWA_ERROR:'):]
                break

        with _sessions_lock:
            _active_sessions[session_id].update({'status': 'done', 'success': success, 'message': message, 'files': files})

    except subprocess.TimeoutExpired:
        try: proc.kill()
        except: pass
        with _sessions_lock:
            _active_sessions[session_id].update({'status': 'done', 'success': False, 'message': _("Action timed out")})
    except Exception as e:
        with _sessions_lock:
            _active_sessions[session_id].update({'status': 'done', 'success': False, 'message': str(e)})
    finally:
        def _cleanup():
            time.sleep(300)
            with _sessions_lock:
                sess = _active_sessions.pop(session_id, None)
            if sess:
                dd = sess.get('dialog_dir')
                if dd and os.path.exists(dd):
                    shutil.rmtree(dd, ignore_errors=True)
        threading.Thread(target=_cleanup, daemon=True).start()


def run_plugin_action_async(plugin_name, action_name):
    from .calibre_plugin_parser import parse_plugin_zip

    zip_path = get_plugin_zip_path(plugin_name)
    schema = parse_plugin_zip(zip_path)

    method_name = None
    for f in schema.fields:
        if f.name == action_name and f.type == 'button':
            method_name = getattr(f, 'connected_method', None)
            break

    if not method_name:
        raise NotImplementedError(
            f"No executable handler found for action '{action_name}' on plugin '{plugin_name}'"
        )

    config_class = schema.config_class.split(',')[0].strip() if schema.config_class else 'ConfigWidget'
    session_id = uuid.uuid4().hex[:12]

    with _sessions_lock:
        _active_sessions[session_id] = {
            'status': 'starting',
            'plugin_name': plugin_name,
            'action_name': action_name,
            'dialog_dir': None,
            'created_at': time.time(),
        }

    threading.Thread(
        target=_run_session_thread,
        args=(session_id, zip_path, config_class, method_name, plugin_name),
        daemon=True
    ).start()

    return session_id


def get_plugin_action_status(session_id):
    with _sessions_lock:
        session = _active_sessions.get(session_id)

    if not session:
        return {'status': 'not_found'}

    status = session.get('status', 'running')
    dialog_dir = session.get('dialog_dir')

    if status == 'done':
        result = {'status': 'done', 'success': session.get('success', False), 'message': session.get('message', '')}
        if session.get('files'):
            result['files'] = session['files']
        return result

    if dialog_dir:
        ready_file = os.path.join(dialog_dir, 'dialog_ready')
        req_file = os.path.join(dialog_dir, 'dialog_request.json')
        if os.path.exists(ready_file) and os.path.exists(req_file):
            try:
                with open(req_file) as f:
                    return {'status': 'dialog', 'dialog': json.load(f)}
            except Exception:
                pass

    return {'status': 'running'}


def respond_to_plugin_dialog(session_id, response_data):
    with _sessions_lock:
        session = _active_sessions.get(session_id)

    if not session:
        raise ValueError(f"Session {session_id} not found")

    dialog_dir = session.get('dialog_dir')
    if not dialog_dir:
        raise ValueError("No dialog pending for this session")

    with open(os.path.join(dialog_dir, 'dialog_response.json'), 'w') as f:
        json.dump(response_data, f)

    try: os.unlink(os.path.join(dialog_dir, 'dialog_ready'))
    except: pass

    open(os.path.join(dialog_dir, 'response_ready'), 'w').close()


def get_plugin_action_file(session_id, filename):
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