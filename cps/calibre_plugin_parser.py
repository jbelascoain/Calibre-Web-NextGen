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
    # Collection widgets used by manager-style sub-dialogs. The frontend
    # renders these as a list/table view that the user can select from;
    # the runner introspects their current items at runtime.
    "QListWidget":    "list",
    "QTableWidget":   "table",
    # Mutually-exclusive choice and numeric range widgets, plus single-line
    # textual range pickers. The runner injects values by patching the
    # standard getter on each widget instance.
    "QRadioButton":   "radio",
    "QSlider":        "slider",
    "QDial":          "slider",
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
    type: str           # text | textarea | checkbox | select | number | password
                        # | button | list | table | radio | slider
    label: str = ""
    options: list = field(default_factory=list)   # populated for 'select' type
    default: Optional[str] = None
    help_text: str = ""
    min: Optional[int] = None   # for 'number'/'slider' types
    max: Optional[int] = None   # for 'number'/'slider' types
    connected_method: Optional[str] = None  # Python method linked via clicked.connect()
    section: str = ""   # tab label from QTabWidget.addTab(); empty when ungrouped
    # For custom-widget fields whose value lives one hop deeper on the
    # instance (e.g. ``self.user_key`` is a custom widget whose actual
    # QLineEdit is at ``self.user_key.inner_edit``), inner_attr is the
    # attribute name on the outer instance. Runtime read/inject paths
    # navigate ``instance.<name>.<inner_attr>`` instead of
    # ``instance.<name>`` directly. None when the value is on the widget
    # itself (the common case).
    inner_attr: Optional[str] = None


@dataclass
class DialogSchema:
    """
    Schema for a Qt sub-dialog opened from inside a primary config widget's
    button handler. Captures the same kind of field information as the main
    schema, scoped to a single QDialog subclass.

    Two flavors are recognized:

      * Simple form (no buttons) — Slice 1. The runner renders the fields
        as a one-shot form, collects values, injects them into the dialog
        instance, returns Accepted.

      * Manager (has buttons + usually a list/table) — Slice 2. The runner
        keeps the dialog instance alive and enters an interactive loop:
        each click on an action button calls the corresponding method on
        the dialog, the next snapshot of state is re-emitted to the web,
        and the loop runs until the user closes via OK/Cancel.

    ``button_connections`` mirrors the primary class's ``button_connections``
    but scoped to the dialog body — used by the runner to resolve which
    method to call when an action button is clicked in manager mode.

    ``is_manager`` is set when the dialog declares at least one button
    field. The runtime branch is selected from this flag.
    """
    class_name: str
    title: str = ""
    fields: list = field(default_factory=list)
    button_connections: dict = field(default_factory=dict)  # button name → method name
    is_manager: bool = False


@dataclass
class PluginSchema:
    """
    Represents the full parsed schema of a Calibre plugin, including
    its metadata and the list of detected configuration fields.

    Sub-dialog information lives in two complementary fields:
      * ``dialogs`` — schema for each QDialog subclass found in the plugin
        that fits the simple-form pattern, keyed by class name.
      * ``button_dialog_map`` — maps primary-class method names to the
        QDialog subclass they instantiate and call ``.exec_()`` on. The
        runner uses this to know when to render a sub-dialog instead of
        falling back to "not available".
    """
    plugin_name: str
    plugin_version: str = ""
    plugin_type: str = ""       # e.g. FileTypePlugin, InterfaceActionBase
    config_class: str = ""      # name of the Qt config class found
    fields: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    confidence: str = "high"    # high | medium | low — how reliable the extraction was
    dialogs: dict = field(default_factory=dict)            # class_name → DialogSchema
    button_dialog_map: dict = field(default_factory=dict)  # method_name → dialog class_name


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


# ── Qt signal-connect detection ──────────────────────────────────────────────

#: Signal names whose connection we treat as "this widget triggers a method
#: when activated". These are the click-equivalent signals for buttons and
#: button-like widgets. Other signals (toggled, stateChanged, valueChanged)
#: deliver per-event arguments rather than discrete user-action callbacks and
#: aren't the action surface the web UI invokes.
_ACTION_SIGNALS = frozenset({"clicked", "triggered", "released", "pressed"})


def _parse_signal_connect(call):
    """
    Recognize Qt signal connect calls of the form::

        self.<widget>.<signal>.connect(<handler>)

    Returns ``(widget_name, signal_name, handler_arg)`` when the call
    matches, ``None`` otherwise. ``handler_arg`` is the raw AST node
    handed to ``.connect(...)`` — caller passes it to
    ``_extract_handler_method`` to resolve the underlying method name.

    Only ``self.<widget>.<signal>.connect(<one_arg>)`` matches. Connections
    spread across saved local references (e.g. ``btn = self.x; btn.clicked
    .connect(...)``) aren't tracked — that's a value-flow analysis we
    don't currently do.
    """
    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "connect"
        and isinstance(call.func.value, ast.Attribute)
        and isinstance(call.func.value.value, ast.Attribute)
        and isinstance(call.func.value.value.value, ast.Name)
        and call.func.value.value.value.id == "self"
        and call.args
    ):
        return None
    signal_name = call.func.value.attr
    if signal_name not in _ACTION_SIGNALS:
        return None
    widget_name = call.func.value.value.attr
    return widget_name, signal_name, call.args[0]


def _extract_handler_method(node):
    """
    Resolve the method name a Qt signal is being connected to. Returns the
    bare method name (string) or ``None`` when the handler isn't expressible
    as a single method on ``self``.

    Supported handler shapes:

      * ``self.method``                              — direct attribute
      * ``lambda *args, **kw: self.method(...)``     — single-call lambda
      * ``functools.partial(self.method, ...)``      — partial application
      * ``partial(self.method, ...)``                — same, unqualified

    Anything else (closures over local state, dict-indexed handlers,
    bound free functions, multi-statement lambdas) yields ``None``.
    """
    # 1. Direct attribute: self.method
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr

    # 2. Lambda whose body is a Call to self.method(...)
    if isinstance(node, ast.Lambda) and isinstance(node.body, ast.Call):
        inner = node.body.func
        if (
            isinstance(inner, ast.Attribute)
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
        ):
            return inner.attr

    # 3. functools.partial(self.method, ...) — partial may be referenced
    #    either as a bare name ``partial`` or as ``functools.partial``.
    if isinstance(node, ast.Call):
        func = node.func
        is_partial = False
        if isinstance(func, ast.Name) and func.id == "partial":
            is_partial = True
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "partial"
            and isinstance(func.value, ast.Name)
            and func.value.id == "functools"
        ):
            is_partial = True
        if is_partial and node.args:
            first = node.args[0]
            if (
                isinstance(first, ast.Attribute)
                and isinstance(first.value, ast.Name)
                and first.value.id == "self"
            ):
                return first.attr

    return None


# ── Plugin metadata extractor ─────────────────────────────────────────────────

def extract_plugin_metadata(source: str) -> dict:
    """
    Extracts plugin name, version, and base type from __init__.py source.

    Looks for class-level assignments such as:
        name = 'MyPlugin'
        version = (1, 0, 0)
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

    # Qt base classes that signal a sub-dialog (something a primary widget
    # might open via .exec_() and read values back from). Kept narrow on
    # purpose — we want to confidently identify sub-dialogs, not over-match
    # generic widgets.
    DIALOG_BASE_CLASSES = {"QDialog"}

    def __init__(self, dialog_class_names=None, primary_class_name=None, custom_widgets=None):
        self.fields: list[FieldSchema] = []
        self.warnings: list[str] = []
        self.config_classes_found: list[str] = []
        self._last_label: str = ""   # text of the most recently seen QLabel
        self._assignments: dict = {} # tracks variable_name → widget_type
        self.button_connections: dict[str, str] = {}  # button_field_name → method_name

        # Sub-dialog support (Slice 1):
        #   * dialog_class_names — names of QDialog subclasses in the file
        #     (passed in by _do_parse_plugin_zip after a quick pre-pass), used
        #     to recognize dialog instantiation in method bodies.
        #   * primary_class_name — the class _do_parse_plugin_zip selected as
        #     the primary config widget. Driven by a structural heuristic
        #     (prefer non-QDialog config classes) rather than file order, so
        #     plugins that define sub-dialogs before the main ConfigWidget
        #     still resolve correctly.
        #   * dialogs — per-class DialogSchema collected while visiting each
        #     QDialog subclass body.
        #   * button_dialog_map — method_name → dialog_class_name, populated
        #     by visit_FunctionDef when it finds `var = SomeDialog(...);
        #     var.exec_()` inside a primary-class method.
        self._dialog_class_names: set[str] = set(dialog_class_names or [])
        self._primary_class_name: Optional[str] = primary_class_name
        # Catalog of plugin-defined custom QWidget subclasses (Slice 7):
        # ``class_name → {'inner_attr', 'inner_type', 'inner_options'}``.
        # When a config-class assignment uses one of these as its widget
        # type, the visitor synthesizes a field of the inner type with an
        # inner_attr pointer so the runner can navigate the value chain.
        self._custom_widgets: dict = custom_widgets or {}
        self.dialogs: dict[str, DialogSchema] = {}
        self.button_dialog_map: dict[str, str] = {}

        # Active visitor context — drives whether visit_Assign appends to
        # the primary field list or to the current sub-dialog's field list.
        self._context: Optional[str] = None  # 'primary' | 'dialog' | None
        self._current_dialog: Optional[DialogSchema] = None

        # Buffer of fields registered since the last QTabWidget.addTab call.
        # Each time a new addTab(_, "Label") is seen, every field in this
        # buffer gets `section = "Label"` and the buffer is cleared. This
        # gives a best-effort grouping for plugins that build their UI in
        # source order:
        #     self.field_a = QLineEdit(...)        # buffered
        #     self.field_b = QCheckBox(...)        # buffered
        #     self.tabs.addTab(tab1, "General")    # → both → section "General"
        # Plugins that interleave field assignments and addTab calls in
        # less linear ways won't get clean grouping, but the schema still
        # collects every field — section is purely a presentation hint.
        self._pending_section_fields: list = []

    def visit_ClassDef(self, node: ast.ClassDef):
        """
        Recurse into classes that inherit from a known Qt config base.

        Two distinct kinds of classes are interesting:

          * The PRIMARY config class — the one Calibre instantiates as the
            plugin's config widget. Selected by _do_parse_plugin_zip via the
            ``primary_class_name`` argument; defaults to the first config
            class found when no explicit selection is made.

          * SUB-DIALOG classes — QDialog subclasses (other than the primary)
            that the primary's button handlers may open. Their fields
            populate ``self.dialogs[class_name].fields``.

        Other config-base subclasses (e.g. a helper QWidget used internally)
        are named in ``config_classes_found`` but not parsed for fields.
        """
        bases = {get_name(b) for b in node.bases}
        if not (bases & self.CONFIG_BASE_CLASSES):
            return  # not a config-class candidate, skip entirely

        if self._primary_class_name is not None:
            is_primary = (node.name == self._primary_class_name)
        else:
            # Fallback when no explicit selection was passed in: first config
            # class wins. Keeps backwards compatibility with callers that
            # construct the visitor directly without the pre-pass.
            is_primary = (len(self.config_classes_found) == 0)
        is_dialog = (not is_primary) and bool(bases & self.DIALOG_BASE_CLASSES)
        self.config_classes_found.append(node.name)

        if is_primary:
            prev_ctx, prev_dlg = self._context, self._current_dialog
            self._context = 'primary'
            self._current_dialog = None
            self.generic_visit(node)
            self._context, self._current_dialog = prev_ctx, prev_dlg

        elif is_dialog:
            prev_ctx, prev_dlg, prev_label = self._context, self._current_dialog, self._last_label
            self._context = 'dialog'
            self._current_dialog = DialogSchema(class_name=node.name)
            self._last_label = ""  # don't bleed labels from primary into dialog
            self.generic_visit(node)
            self.dialogs[node.name] = self._current_dialog
            self._context, self._current_dialog, self._last_label = prev_ctx, prev_dlg, prev_label
        # else: helper widget — name recorded, body not parsed

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
                    detected_field = None

                    if widget_type in QT_TO_FIELD:
                        detected_field = self._build_field(attr_name, widget_type, node.value)
                    elif widget_type in self._custom_widgets:
                        # Custom widget subclass (Slice 7) — synthesize a
                        # field whose type comes from the catalogued inner
                        # widget, with inner_attr set so the runner navigates
                        # through the outer instance to reach the value.
                        spec = self._custom_widgets[widget_type]
                        detected_field = FieldSchema(
                            name=attr_name,
                            type=spec['inner_type'],
                            options=list(spec.get('inner_options') or []),
                            inner_attr=spec['inner_attr'],
                        )

                    if detected_field:
                        # Assign the most recently seen QLabel text as this field's label
                        if self._last_label and not detected_field.label:
                            detected_field.label = self._last_label
                            self._last_label = ""
                        # Route the field into the active context's collection
                        if self._context == 'primary':
                            self.fields.append(detected_field)
                        elif self._context == 'dialog' and self._current_dialog is not None:
                            self._current_dialog.fields.append(detected_field)
                        # Buffer for QTabWidget.addTab grouping — flushed
                        # when a matching addTab() call is later detected.
                        self._pending_section_fields.append(detected_field)

                    self._assignments[attr_name] = widget_type

        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """
        While walking a primary-class method body, look for the pattern:

            var = SomeDialog(...)        # SomeDialog is a known QDialog subclass
            ...
            var.exec_()                  # or var.exec()

        If found, record ``method_name → SomeDialog`` in button_dialog_map.
        This is the linkage the runner uses at action-execution time to
        decide whether the QDialog.exec_() interception should render a
        web sub-dialog (mapping exists) or fall back to the generic
        "not available in web mode" message (mapping absent).

        The scan is local to the method's AST subtree, ignoring nested
        function definitions to avoid following lambdas/closures into
        unrelated scopes.
        """
        if self._context != 'primary':
            # Methods of sub-dialogs / helpers are not scanned at Slice 1.
            self.generic_visit(node)
            return

        # Map of locally-assigned names → dialog class name within this method.
        local_dialog_vars: dict[str, str] = {}

        for child in ast.walk(node):
            # Don't follow nested function bodies — they have their own scope.
            if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue

            # var = SomeDialog(...)
            if isinstance(child, ast.Assign) and isinstance(child.value, ast.Call):
                callee_name = get_call_name(child.value)
                if callee_name in self._dialog_class_names:
                    for tgt in child.targets:
                        if isinstance(tgt, ast.Name):
                            local_dialog_vars[tgt.id] = callee_name

            # var.exec_() / var.exec()
            elif isinstance(child, ast.Call):
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ('exec_', 'exec')
                    and isinstance(func.value, ast.Name)
                    and func.value.id in local_dialog_vars
                ):
                    self.button_dialog_map[node.name] = local_dialog_vars[func.value.id]
                    break

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

            # Signal connection detection. _parse_signal_connect recognizes
            #     self.<widget>.<signal>.connect(<handler>)
            # for the click-equivalent signals (clicked / triggered /
            # released / pressed). The handler is then resolved by
            # _extract_handler_method, which supports direct
            # ``self.method``, single-call ``lambda``, and ``partial``
            # wrappers. Patterns the helper doesn't recognize yield
            # ``None`` and the connection is silently skipped — the field
            # will still appear in the schema, just with
            # ``connected_method == None``, so the runtime falls back to
            # a NotImplementedError on invocation.
            parsed = _parse_signal_connect(call)
            if parsed is not None:
                widget_name, _signal, handler_node = parsed
                method = _extract_handler_method(handler_node)
                if method is not None:
                    # Route the connection to the active context's button
                    # map. Primary-class connections feed
                    # FieldSchema.connected_method downstream; sub-dialog
                    # connections power the manager-mode action dispatch
                    # at runtime (Slice 2).
                    if self._context == 'dialog' and self._current_dialog is not None:
                        self._current_dialog.button_connections[widget_name] = method
                    else:
                        self.button_connections[widget_name] = method

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

        # QTabWidget grouping — detect `self.tabs.addTab(<widget>, "Label")`.
        # We don't require the parent QTabWidget to be a registered field
        # (it's in SKIP_WIDGETS), so we resolve this before the field-target
        # lookup. The label flushes the pending-fields buffer onto section,
        # giving every field added since the previous addTab a presentation
        # group.
        if method == "addTab" and len(node.args) >= 2:
            label = get_string_value(node.args[1])
            if label and self._pending_section_fields:
                for pf in self._pending_section_fields:
                    if not pf.section:
                        pf.section = label
                self._pending_section_fields = []
            # Continue walking — nested calls inside the addTab args could
            # carry field metadata for downstream visitors.
            self.generic_visit(node)
            return

        field_name = obj.attr

        # Resolve the target field from whichever collection matches the
        # active context, so widget configuration calls (.addItems, setMinimum
        # etc.) made inside a sub-dialog's body update the sub-dialog's
        # fields rather than silently no-op'ing.
        if self._context == 'dialog' and self._current_dialog is not None:
            candidates = self._current_dialog.fields
        else:
            candidates = self.fields
        target_field = next((f for f in candidates if f.name == field_name), None)

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

    def _collect_dialog_class_names(tree: ast.AST) -> set:
        """Pre-pass: find all QDialog subclass names declared in this tree."""
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {get_name(b) for b in node.bases}
                if bases & ConfigClassVisitor.DIALOG_BASE_CLASSES:
                    names.add(node.name)
        return names

    def _catalog_custom_widgets(tree: ast.AST, dialog_classes: set) -> dict:
        """
        Pre-pass: find every ``class XYZ(QWidget)`` declared in this tree
        that isn't a config-class candidate (won't be the primary) and isn't
        a QDialog subclass (those are handled by the sub-dialog path). For
        each, sniff the ``__init__`` body for a single primary inner Qt
        widget — a ``self.foo = QSomething(...)`` assignment whose widget
        type is in QT_TO_FIELD and isn't a button.

        Returns a dict::

            { custom_class_name: { 'inner_attr': str,
                                   'inner_type': str,
                                   'inner_options': [str, ...] } }

        Custom widgets with zero recognized inner fields, or with two or
        more recognized inner fields, are NOT cataloged — too ambiguous to
        proxy cleanly through the web. Plugin code can still use them; they
        just won't surface in the schema.
        """
        catalog: dict = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {get_name(b) for b in node.bases}
            # Only QWidget subclasses that aren't QDialog subclasses are
            # candidates. ConfigWidget/ConfigWidgetBase base names belong
            # to the primary class path and are excluded too.
            if "QWidget" not in bases:
                continue
            if bases & ConfigClassVisitor.DIALOG_BASE_CLASSES:
                continue
            if {"ConfigWidget", "ConfigWidgetBase"} & bases:
                continue
            if node.name in dialog_classes:
                continue

            # Walk the class body looking for self.X = QSomething(...).
            inner_fields = []
            for body_node in ast.walk(node):
                if not isinstance(body_node, ast.Assign):
                    continue
                if not isinstance(body_node.value, ast.Call):
                    continue
                wtype = get_call_name(body_node.value)
                if wtype not in QT_TO_FIELD:
                    continue
                fld_type = QT_TO_FIELD[wtype]
                if fld_type == 'button':
                    continue   # buttons aren't value-bearing; ignore
                for target in body_node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        # Capture options for select-like inner widgets from
                        # construction args, if any inline list is present.
                        opts: list = []
                        if fld_type == 'select':
                            for arg in body_node.value.args:
                                if isinstance(arg, ast.List):
                                    opts = [
                                        get_string_value(elt)
                                        for elt in arg.elts
                                    ]
                                    opts = [o for o in opts if o is not None]
                                    break
                        inner_fields.append((target.attr, fld_type, opts))
                        break

            if len(inner_fields) == 1:
                name, ftype, opts = inner_fields[0]
                catalog[node.name] = {
                    'inner_attr': name,
                    'inner_type': ftype,
                    'inner_options': opts,
                }
        return catalog

    def _select_primary_class(tree: ast.AST) -> Optional[str]:
        """
        Pre-pass: pick the class most likely to be the plugin's primary
        config widget. Strategy:

          1. Any config-base subclass that is NOT a QDialog subclass wins —
             plugins overwhelmingly use QWidget (or ConfigWidget/
             ConfigWidgetBase) for the main config and QDialog only for
             sub-dialogs.
          2. If every config-base subclass in the file is a QDialog
             subclass (rare — happens with old plugins whose root config IS
             a modal dialog), fall back to the first one in source order.

        Returns None when no config-base subclass is found at all.
        """
        non_dialog_candidates: list[str] = []
        all_candidates: list[str] = []
        dialog_bases = ConfigClassVisitor.DIALOG_BASE_CLASSES
        config_bases = ConfigClassVisitor.CONFIG_BASE_CLASSES

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {get_name(b) for b in node.bases}
            if not (bases & config_bases):
                continue
            all_candidates.append(node.name)
            if not (bases & dialog_bases):
                non_dialog_candidates.append(node.name)

        if non_dialog_candidates:
            return non_dialog_candidates[0]
        if all_candidates:
            return all_candidates[0]
        return None

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

        dialog_classes = _collect_dialog_class_names(tree)
        primary = _select_primary_class(tree)
        custom_widgets = _catalog_custom_widgets(tree, dialog_classes)
        visitor = ConfigClassVisitor(
            dialog_class_names=dialog_classes,
            primary_class_name=primary,
            custom_widgets=custom_widgets,
        )
        visitor.visit(tree)

        for f in visitor.fields:
            if f.type == "button" and f.name in visitor.button_connections:
                f.connected_method = visitor.button_connections[f.name]

        # Merge sub-dialog data into the plugin schema. Last writer wins on
        # the rare case the same dialog class name appears in two files.
        schema.dialogs.update(visitor.dialogs)
        schema.button_dialog_map.update(visitor.button_dialog_map)

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
            dialog_classes = _collect_dialog_class_names(tree)
            primary = _select_primary_class(tree)
            custom_widgets = _catalog_custom_widgets(tree, dialog_classes)
            visitor = ConfigClassVisitor(
                dialog_class_names=dialog_classes,
                primary_class_name=primary,
                custom_widgets=custom_widgets,
            )
            visitor.visit(tree)
            for f in visitor.fields:
                if f.type == "button" and f.name in visitor.button_connections:
                    f.connected_method = visitor.button_connections[f.name]
            schema.dialogs.update(visitor.dialogs)
            schema.button_dialog_map.update(visitor.button_dialog_map)
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

    # Post-process sub-dialog field collections:
    #   * Deduplicate by name (the visitor can pick up the same attribute
    #     through multiple AST paths — e.g. constructor branches that all
    #     assign self.x).
    #   * Resolve connected_method for each button using the dialog's own
    #     button_connections map (populated by visit_Expr when in dialog
    #     context).
    #   * Set is_manager whenever the dialog contains at least one button
    #     field — this is the signal the runner uses to choose between the
    #     Slice 1 simple-form path and the Slice 2 interactive-loop path.
    for dlg in schema.dialogs.values():
        unique_fields = []
        seen_names = set()
        has_button = False
        for f in dlg.fields:
            if f.name in seen_names:
                continue
            seen_names.add(f.name)
            if f.type == "button":
                has_button = True
                if f.name in dlg.button_connections:
                    f.connected_method = dlg.button_connections[f.name]
            unique_fields.append(f)
        dlg.fields = unique_fields
        dlg.is_manager = has_button

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
