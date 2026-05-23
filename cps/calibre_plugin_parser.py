"""
Calibre Plugin AST Parser
=========================
Parses the source code of a Calibre plugin (ZIP) without executing it,
detects Qt widgets in configuration classes, and generates a JSON schema
that can be used to render a web form.

Part of the Calibre-Web-NextGen plugin manager contribution.
"""

import ast
import os
import sys
import json
import zipfile
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Qt widget → web field type mapping ───────────────────────────────────────

# Maps Qt widget class names to their equivalent HTML input types.
# Only widgets that represent user-configurable values are included here.
QT_TO_FIELD = {
    "QLineEdit":      "text",
    "QTextEdit":      "textarea",
    "QPlainTextEdit": "textarea",
    "QCheckBox":      "checkbox",
    "QComboBox":      "select",
    "QSpinBox":       "number",
    "QDoubleSpinBox": "number",
    "QPasswordField": "password",   # not a real Qt class, but used by some plugins as a naming convention
    # Action buttons are not config fields per se, but we register them
    # so the UI can render them as callable actions.
    "QPushButton":    "button",
    "QToolButton":    "button",
}

# Widgets that carry no user-configurable value and should be ignored
# during field extraction (layout containers, decorative elements, etc.).
SKIP_WIDGETS = {
    "QLabel", "QGroupBox", "QFrame", "QWidget",
    "QHBoxLayout", "QVBoxLayout", "QGridLayout",
    "QTabWidget", "QScrollArea", "QSplitter",
}


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class FieldSchema:
    """
    Represents a single configurable field extracted from a Qt config widget.
    This schema is consumed by the frontend to render the appropriate input element.
    """
    name: str
    type: str           # text | textarea | checkbox | select | number | button | password
    label: str = ""
    options: list = field(default_factory=list)   # populated for 'select' type
    default: Optional[str] = None
    help_text: str = ""
    min: Optional[int] = None   # for 'number' type
    max: Optional[int] = None   # for 'number' type
    connected_method: Optional[str] = None  # Python method linked via clicked.connect()


@dataclass
class PluginSchema:
    """
    Represents the full parsed schema of a Calibre plugin, including
    its metadata and the list of detected configuration fields.
    """
    plugin_name: str
    plugin_version: str = ""
    plugin_type: str = ""       # e.g. FileTypePlugin, InterfaceActionBase
    config_class: str = ""      # name of the Qt config class found
    fields: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    confidence: str = "high"    # high | medium | low — how reliable the extraction was


# ── AST helper functions ─────────────────────────────────────────────────────

def get_name(node) -> str:
    """
    Extracts the identifier name from an AST Name or Attribute node.
    Returns an empty string if the node type is not recognized.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def get_string_value(node) -> Optional[str]:
    """
    Extracts the string value from an AST node.
    Handles plain string constants and _("...") / __("...") translation wrappers.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("_", "__")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def get_call_name(node) -> str:
    """
    Returns the name of the function or class being called in an AST Call node.
    Works for both simple calls (e.g. QLineEdit()) and attribute calls (e.g. self.x()).
    """
    if isinstance(node, ast.Call):
        return get_name(node.func)
    return ""


# ── Plugin metadata extractor ─────────────────────────────────────────────────

def extract_plugin_metadata(source: str) -> dict:
    """
    Extracts plugin name, version, and base type from __init__.py source.

    Looks for class-level assignments such as:
        name = 'DeACSM'
        version = (0, 0, 16)
    and the class base (e.g. FileTypePlugin, InterfaceActionBase).

    Returns a dict with keys: 'name', 'version', 'type'.
    """
    meta = {"name": "", "version": "", "type": ""}

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return meta

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Detect the plugin type from the first recognized base class
        for base in node.bases:
            base_name = get_name(base)
            if base_name:
                meta["type"] = base_name

        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            for target in item.targets:
                attr = get_name(target)

                if attr == "name":
                    val = get_string_value(item.value)
                    if val:
                        meta["name"] = val

                elif attr == "version":
                    # Two common forms:
                    #   version = (0, 0, 16) → "0.0.16"
                    #   version = "1.0.0"    → "1.0.0"
                    if isinstance(item.value, ast.Tuple):
                        parts = [
                            str(elt.value)
                            for elt in item.value.elts
                            if isinstance(elt, ast.Constant)
                        ]
                        meta["version"] = ".".join(parts)
                    elif isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                        meta["version"] = item.value.value

    return meta


# ── Config class field extractor ─────────────────────────────────────────────

class ConfigClassVisitor(ast.NodeVisitor):
    """
    AST visitor that walks a Python source file and extracts configuration
    fields from classes that inherit from known Qt base classes
    (QWidget, QDialog, ConfigWidgetBase, etc.).

    The visitor tracks:
    - self.xxx = QSomeWidget(...) assignments → register as fields
    - self.xxx.addItems([...])               → populate select options
    - self.xxx.setMinimum/Maximum(n)         → set number bounds
    - self.xxx.setPlaceholderText(...)       → set help text
    - self.xxx.setToolTip(...)               → set help text (fallback)
    - QLabel("text") before a widget         → use as the field label
    """

    # Qt base classes that signal a configuration dialog/widget
    CONFIG_BASE_CLASSES = {
        "QWidget", "QDialog", "QDialogButtonBox",
        "ConfigWidgetBase", "ConfigWidget",
    }

    def __init__(self):
        self.fields: list[FieldSchema] = []
        self.warnings: list[str] = []
        self.config_classes_found: list[str] = []
        self._last_label: str = ""   # text of the most recently seen QLabel
        self._assignments: dict = {} # tracks variable_name → widget_type
        self.button_connections: dict[str, str] = {}  # button_field_name → method_name

    def visit_ClassDef(self, node: ast.ClassDef):
        """
        Only recurse into classes that inherit from a known Qt config base.
        This avoids false positives from helper or data classes in the same file.
        """
        bases = {get_name(b) for b in node.bases}
        if bases & self.CONFIG_BASE_CLASSES:
            self.config_classes_found.append(node.name)
            self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        """
        Detects assignments of the form:
            self.field_name = QSomeWidget(...)
        Registers the field and tries to capture the preceding QLabel as its label.
        """
        if not isinstance(node.value, ast.Call):
            self.generic_visit(node)
            return

        widget_type = get_call_name(node.value)

        # Skip layout and decoration widgets
        if widget_type in SKIP_WIDGETS:
            self.generic_visit(node)
            return

        for target in node.targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                if target.value.id == "self":
                    attr_name = target.attr

                    if widget_type in QT_TO_FIELD:
                        detected_field = self._build_field(attr_name, widget_type, node.value)
                        if detected_field:
                            # Assign the most recently seen QLabel text as this field's label
                            if self._last_label and not detected_field.label:
                                detected_field.label = self._last_label
                                self._last_label = ""
                            self.fields.append(detected_field)

                    self._assignments[attr_name] = widget_type

        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr):
        """
        Detects standalone QLabel(...) expressions (not assigned to self)
        and stores their text for use as the label of the next widget.

        Example:
            QLabel("Adobe ID Email:")   # will become the label for the next field
            self.adobe_email = QLineEdit()
        """
        if isinstance(node.value, ast.Call):
            call = node.value
            name = get_call_name(call)
            if name == "QLabel" and call.args:
                label_text = get_string_value(call.args[0])
                if label_text:
                    # Strip trailing colons and spaces that are common in Qt UIs
                    self._last_label = label_text.rstrip(": ")

            # Detect: self.button_name.clicked.connect(self.method_name)
            #
            # We only match this exact, fully-qualified pattern. Anything else
            # — lambdas (`connect(lambda: self.foo())`), dict-indexed methods
            # (`connect(self.handlers[key])`), or other signals like `triggered`
            # / `released` — is silently skipped. Such buttons will surface in
            # the schema as `connected_method == None`, and the runtime path
            # in cps/plugins.py will raise NotImplementedError when they're
            # clicked. Expand the match here if a target plugin needs it.
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "connect"
                and isinstance(call.func.value, ast.Attribute)
                and call.func.value.attr == "clicked"
                and isinstance(call.func.value.value, ast.Attribute)
                and isinstance(call.func.value.value.value, ast.Name)
                and call.func.value.value.value.id == "self"
                and call.args
                and isinstance(call.args[0], ast.Attribute)
                and isinstance(call.args[0].value, ast.Name)
                and call.args[0].value.id == "self"
            ):
                btn = call.func.value.value.attr  # e.g. "button_link_account"
                mth = call.args[0].attr           # e.g. "link_account"
                self.button_connections[btn] = mth

        self.generic_visit(node)

    def _build_field(self, name: str, widget_type: str, call_node: ast.Call) -> Optional[FieldSchema]:
        """
        Constructs a FieldSchema from a detected Qt widget assignment.

        Handles simple defaults (e.g. QLineEdit("default")) at construction time.
        More complex properties like options, min/max, and tooltips are resolved
        later in visit_Call when method calls on self.field_name are detected.
        """
        field_type = QT_TO_FIELD.get(widget_type)
        if not field_type:
            return None

        detected_field = FieldSchema(
            name=name,
            type=field_type,
            # Generate a human-readable label from the variable name as a fallback.
            # This will be overwritten if a QLabel is found in the surrounding code.
            label=self._label_from_name(name),
        )

        # Extract inline default value from QLineEdit("some default")
        if widget_type == "QLineEdit" and call_node.args:
            default = get_string_value(call_node.args[0])
            if default:
                detected_field.default = default

        # Note: QCheckBox("label text") passes the label as the first argument.
        # We capture that here and use it as the field label.
        if widget_type == "QCheckBox" and call_node.args:
            checkbox_label = get_string_value(call_node.args[0])
            if checkbox_label:
                detected_field.label = checkbox_label

        # Note: QPushButton("Button Text") passes the button label as first argument.
        if widget_type in ("QPushButton", "QToolButton") and call_node.args:
            btn_label = get_string_value(call_node.args[0])
            if btn_label:
                detected_field.label = btn_label

        return detected_field

    @staticmethod
    def _label_from_name(name: str) -> str:
        """
        Converts a snake_case variable name to a Title Case human-readable label.
        Used as a fallback when no QLabel is found near the widget.

        Example: 'adobe_account_email' → 'Adobe Account Email'
        """
        return name.lstrip("_").replace("_", " ").replace("-", " ").title()

    def visit_Call(self, node: ast.Call):
        """
        Detects method calls on previously registered self.field_name widgets:

            self.some_combo.addItems([...])     → populate select options
            self.some_combo.addItem("...")      → append single option
            self.some_spin.setMinimum(0)        → set min bound
            self.some_spin.setMaximum(100)      → set max bound
            self.some_input.setPlaceholderText  → set help text
            self.some_input.setToolTip(...)     → set help text (if not already set)
            self.some_label.setText("...")      → update the pending label text
        """
        if not isinstance(node.func, ast.Attribute):
            self.generic_visit(node)
            return

        method = node.func.attr
        obj = node.func.value

        # We only care about calls in the form: self.something.method(...)
        if not (
            isinstance(obj, ast.Attribute) and
            isinstance(obj.value, ast.Name) and
            obj.value.id == "self"
        ):
            self.generic_visit(node)
            return

        field_name = obj.attr
        target_field = next((f for f in self.fields if f.name == field_name), None)

        if not target_field:
            self.generic_visit(node)
            return

        # Populate options for select fields
        if method == "addItems" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.List):
                options = [get_string_value(elt) for elt in arg.elts]
                target_field.options = [o for o in options if o is not None]

        elif method == "addItem" and node.args:
            val = get_string_value(node.args[0])
            if val:
                target_field.options.append(val)

        # Set numeric bounds
        elif method == "setMinimum" and node.args:
            if isinstance(node.args[0], ast.Constant):
                target_field.min = node.args[0].value

        elif method == "setMaximum" and node.args:
            if isinstance(node.args[0], ast.Constant):
                target_field.max = node.args[0].value

        # Set help text from placeholder or tooltip
        elif method == "setPlaceholderText" and node.args:
            val = get_string_value(node.args[0])
            if val:
                target_field.help_text = val

        elif method == "setToolTip" and node.args:
            val = get_string_value(node.args[0])
            # Only use tooltip if no placeholder text was already set
            if val and not target_field.help_text:
                target_field.help_text = val

        # setText on a registered field updates its own label directly
        elif method == "setText" and node.args:
            val = get_string_value(node.args[0])
            if val:
                target_field.label = val.rstrip(": ")

        self.generic_visit(node)


# ── ZIP inspection ────────────────────────────────────────────────────────────

def detect_config_files(zip_path: str) -> dict:
    """
    Opens a Calibre plugin ZIP and returns a dict of {filename: source_code}
    for all Python files that are likely to contain configuration UI code.

    Always includes __init__.py (for metadata).
    Also includes any .py file whose name contains common config-related keywords.
    """
    candidates = {}
    config_hints = {"config", "setting", "prefs", "preference", "options"}

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".py"):
                continue
            stem = Path(name).stem.lower()
            if stem == "__init__" or any(hint in stem for hint in config_hints):
                try:
                    source = zf.read(name).decode("utf-8", errors="replace")
                    candidates[name] = source
                except Exception:
                    pass  # skip unreadable files silently

    return candidates


# ── Result cache (keyed by zip_path + mtime) ──────────────────────────────────

# Parsing a plugin ZIP can touch dozens of Python files and run a non-trivial
# amount of AST work. The admin UI re-requests the schema every time the user
# opens a plugin's configuration modal, so we cache by mtime to avoid redoing
# the work when the ZIP hasn't changed.
_parse_cache: dict = {}
_parse_cache_lock = threading.Lock()


def parse_plugin_zip(zip_path: str) -> PluginSchema:
    """
    Cached entry point. Returns the parsed PluginSchema for the given ZIP.
    Cache is keyed by (zip_path, mtime) so editing/replacing the ZIP forces
    a re-parse on the next call.
    """
    try:
        mtime = os.path.getmtime(zip_path)
    except OSError:
        mtime = 0.0

    with _parse_cache_lock:
        cached = _parse_cache.get(zip_path)
        if cached and cached[0] == mtime:
            return cached[1]

    schema = _do_parse_plugin_zip(zip_path)

    with _parse_cache_lock:
        _parse_cache[zip_path] = (mtime, schema)

    return schema


def _do_parse_plugin_zip(zip_path: str) -> PluginSchema:
    """
    Parses a Calibre plugin ZIP file and returns a PluginSchema describing
    all detected configuration fields.

    The schema can be serialized to JSON and consumed by the frontend
    to render a dynamic configuration form.
    """
    files = detect_config_files(zip_path)

    if not files:
        return PluginSchema(
            plugin_name=Path(zip_path).stem,
            warnings=["No Python files found in the ZIP archive."],
            confidence="low",
        )

    # Extract plugin metadata from __init__.py
    meta = {"name": Path(zip_path).stem, "version": "", "type": ""}
    if "__init__.py" in files:
        meta = extract_plugin_metadata(files["__init__.py"])

    schema = PluginSchema(
        plugin_name=meta["name"] or Path(zip_path).stem,
        plugin_version=meta["version"],
        plugin_type=meta["type"],
    )

    all_fields = []
    all_warnings = []

    for filename, source in files.items():
        if filename == "__init__.py":
            # Already used __init__.py for metadata; skip field extraction here
            # unless no dedicated config file exists (handled as fallback below)
            continue

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            all_warnings.append(f"SyntaxError in {filename}: {e}")
            continue

        visitor = ConfigClassVisitor()
        visitor.visit(tree)

        for f in visitor.fields:
            if f.type == "button" and f.name in visitor.button_connections:
                f.connected_method = visitor.button_connections[f.name]

        if visitor.config_classes_found:
            schema.config_class = ", ".join(visitor.config_classes_found)
            all_fields.extend(visitor.fields)

        elif visitor.fields:
            # Fields found but no recognized Qt base class — lower confidence
            all_fields.extend(visitor.fields)
            all_warnings.append(
                f"{filename}: fields detected but no recognized Qt base class found."
            )

    # Fallback: if no config file found fields, try __init__.py as well
    if not all_fields and "__init__.py" in files:
        try:
            tree = ast.parse(files["__init__.py"])
            visitor = ConfigClassVisitor()
            visitor.visit(tree)
            for f in visitor.fields:
                if f.type == "button" and f.name in visitor.button_connections:
                    f.connected_method = visitor.button_connections[f.name]
            if visitor.fields:
                all_fields.extend(visitor.fields)
                all_warnings.append(
                    "Config fields found inside __init__.py (no dedicated config file detected)."
                )
        except SyntaxError:
            pass

    # Deduplicate fields by name, preserving order
    seen = set()
    for f in all_fields:
        if f.name not in seen:
            schema.fields.append(f)
            seen.add(f.name)

    schema.warnings = all_warnings

    # Assess extraction confidence
    if not schema.fields:
        schema.confidence = "low"
        schema.warnings.append(
            "No configuration fields detected. "
            "The plugin may build its UI dynamically at runtime, "
            "or it may not have a configuration dialog."
        )
    elif len(schema.fields) < 2:
        schema.confidence = "medium"
    else:
        schema.confidence = "high"

    return schema


def main():
    if len(sys.argv) > 1:
        schema = parse_plugin_zip(sys.argv[1])
        print(json.dumps(asdict(schema), indent=2, ensure_ascii=False))
    else:
        print("Usage: python calibre_plugin_parser.py plugin.zip")


if __name__ == "__main__":
    main()
