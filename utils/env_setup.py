import os
import sys


def configure_runtime_env(fallback_env_base: str = r"C:\Users\Frank\.conda\envs\UavTool"):
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

    for b in bin_candidates:
        if not os.path.isdir(b):
            continue
        os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(b)
            except Exception:
                pass
