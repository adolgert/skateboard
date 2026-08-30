"""What the oracle image runs: the app built from the container's environment.

Kept apart from app.py so that importing the app module -- which a test
does on a machine holding none of the deployment's directories -- builds
nothing, while this module, named only in the image's command line,
builds the one app the container serves.
"""
from .app import from_environment

app = from_environment()
