import os
import sys


_dll_directory_handles = {}


def _path_key(path):
    return os.path.normcase(os.path.abspath(path.strip('"')))


def configure_runtime_env(fallback_env_base: str = ""):
    """Ensure PROJ/GDAL runtime paths are configured before importing geo libs."""
    candidates = []

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(meipass)

    exe_base = os.path.dirname(sys.executable)
    if exe_base:
        candidates.append(exe_base)

    if fallback_env_base:
        candidates.append(fallback_env_base)

    proj_candidates = []
    gdal_candidates = []
    bin_candidates = []

    for base in candidates:
        proj_candidates.extend(
            [
                os.path.join(base, "proj"),
                os.path.join(base, r"Library\share\proj"),
            ]
        )
        gdal_candidates.extend(
            [
                os.path.join(base, "gdal-data"),
                os.path.join(base, r"Library\share\gdal"),
            ]
        )
        bin_candidates.extend(
            [
                base,
                os.path.join(base, r"Library\bin"),
            ]
        )

    proj_dir = next((p for p in proj_candidates if os.path.isdir(p)), "")
    if proj_dir:
        os.environ["PROJ_LIB"] = proj_dir
        os.environ["PROJ_DATA"] = proj_dir

    gdal_dir = next((p for p in gdal_candidates if os.path.isdir(p)), "")
    if gdal_dir:
        os.environ["GDAL_DATA"] = gdal_dir

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    existing_paths = {_path_key(p) for p in path_entries if p}
    for b in bin_candidates:
        if not os.path.isdir(b):
            continue
        key = _path_key(b)
        if key not in existing_paths:
            os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
            existing_paths.add(key)
        if hasattr(os, "add_dll_directory") and key not in _dll_directory_handles:
            try:
                _dll_directory_handles[key] = os.add_dll_directory(b)
            except Exception:
                pass
