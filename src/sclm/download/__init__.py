"""Download layer: yt-dlp integration, playlist preview, job queue/workers. No Qt imports.

Public API (see docs/ARCHITECTURE.md):
  sclm.download.preview.fetch_preview
  sclm.download.manager.DownloadManager

Internals: ``ytdlp_opts`` (option builder, logger adapter, cookies copy), ``mapping``
(info dict -> TrackInfo/PlaylistInfo, artwork choice, Go+ preview detection), ``errors``
(``classify_ytdlp_error``).
"""
