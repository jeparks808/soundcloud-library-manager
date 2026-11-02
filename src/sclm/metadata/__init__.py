"""Metadata layer: ID3 tagging, artwork, naming, organization, duplicates, scan, Beets.
No Qt imports.

Public API (see docs/ARCHITECTURE.md):
  sclm.metadata.pipeline.finalize_track      (called by download.manager)
  sclm.metadata.duplicates.find_duplicate    (called by download.manager)
  sclm.metadata.duplicates.audio_sha256
  sclm.metadata.scanner.scan_library         (called by the GUI via run_in_background)
  sclm.metadata.naming.render_filename       (GUI uses it for the settings preview)
  sclm.metadata.naming.sanitize_component
  sclm.metadata.genre.normalize_genre
  sclm.metadata.editor.read_tags / save_tags (GUI metadata editor, via run_in_background)
"""
