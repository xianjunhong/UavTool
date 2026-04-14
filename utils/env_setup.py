import os
import sys


def configure_runtime_env(fallback_env_base: str = r"C:\Users\Frank\.conda\envs\UavTool"):
    """Ensure PROJ/GDAL runtime paths are configured before importing geo libs."""
    env_base = os.path.dirname(sys.executable)
    if not os.path.isdir(env_base):
        env_base = fallback_env_base

    env_bin = os.path.join(env_base, r"Library\bin")
    env_data = os.path.join(env_base, r"Library\share\proj")

    if os.path.isdir(env_data):
        os.environ["PROJ_LIB"] = env_data
        os.environ["PROJ_DATA"] = env_data

    if os.path.isdir(env_bin):
        os.environ["PATH"] = env_bin + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(env_bin)
