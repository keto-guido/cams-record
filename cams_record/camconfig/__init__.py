"""cams_config: the camera object (identity, streams, stream purposes, access, detection, recording
and notification settings) stored in its own MariaDB database. Design: docs/camera-model.md.

cams-record owns the schema and is the only writer except cams-detect's detection settings.
Nothing in the running services reads it yet; ``python -m cams_record.camconfig`` imports today's
files into it and exports the App Hub registry from it.
"""
