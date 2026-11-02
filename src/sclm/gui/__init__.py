"""PySide6 presentation layer. No business logic; talks to core/download/metadata only
through ``sclm.context.AppContext``, ``gui.bridge`` signals and ``run_in_background``.

Modules (Stage 2): main_window, bridge, theme, preview_panel, queue_view, history_view,
library_view, metadata_editor, settings_dialog. See docs/ARCHITECTURE.md section 9.
"""
