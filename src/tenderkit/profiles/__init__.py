"""Built-in profiles; no dependency on pharma or a user-specific filesystem path."""
from importlib.resources import files

from tenderkit.profile import load_profile


def builtin_profiles():
    return {path.name[:-5]: load_profile(path) for path in files(__name__).iterdir() if path.name.endswith('.toml')}


def resolve_profile(name_or_path):
    profiles = builtin_profiles()
    return profiles[name_or_path] if name_or_path in profiles else load_profile(name_or_path)
