import os
import json
import datetime
import platform
import getpass
import socket

from ayon_core.settings.lib import get_local_settings
from .execute import get_ayon_launcher_args
from .local_settings import get_local_site_id


def get_ayon_launcher_version():
    version_filepath = os.path.join(os.environ["AYON_ROOT"], "version.py")
    if not os.path.exists(version_filepath):
        return None
    content = {}
    with open(version_filepath, "r") as stream:
        exec(stream.read(), content)
    return content["__version__"]


def is_running_from_build():
    """Determine if current process is running from build or code.

    Returns:
        bool: True if running from build.
    """

    executable_path = os.environ["AYON_EXECUTABLE"]
    executable_filename = os.path.basename(executable_path)
    if "python" in executable_filename.lower():
        return False
    return True


def is_staging_enabled():
    return os.getenv("AYON_USE_STAGING") == "1"


def is_dev_mode_enabled():
    """Dev mode is enabled in AYON.

    Returns:
        bool: True if dev mode is enabled.
    """

    return os.getenv("AYON_USE_DEV") == "1"


def get_ayon_info():
    executable_args = get_ayon_launcher_args()
    if is_running_from_build():
        version_type = "build"
    else:
        version_type = "code"
    return {
        "ayon_launcher_version": get_ayon_launcher_version(),
        "version_type": version_type,
        "executable": executable_args[-1],
        "ayon_root": os.environ["AYON_ROOT"],
        "server_url": os.environ["AYON_SERVER_URL"]
    }


def get_workstation_info():
    """Basic information about workstation."""
    host_name = socket.gethostname()
    try:
        host_ip = socket.gethostbyname(host_name)
    except socket.gaierror:
        host_ip = "127.0.0.1"

    return {
        "hostname": host_name,
        "host_ip": host_ip,
        "username": getpass.getuser(),
        "system_name": platform.system(),
        "local_id": get_local_site_id()
    }


def get_all_current_info():
    """All information about current process in one dictionary."""

    return {
        "workstation": get_workstation_info(),
        "env": os.environ.copy(),
        "local_settings": get_local_settings(),
        "ayon": get_ayon_info(),
    }


def extract_ayon_info_to_file(dirpath, filename=None):
    """Extract all current info to a file.

    It is possible to define only directory path. Filename is concatenated with
    pype version, workstation site id and timestamp.

    Args:
        dirpath (str): Path to directory where file will be stored.
        filename (Optional[str]): Filename. If not defined, it is generated.

    Returns:
        filepath (str): Full path to file where data were extracted.
    """
    if not filename:
        filename = "{}_{}.json".format(
            get_local_site_id(),
            datetime.datetime.now().strftime("%y%m%d%H%M%S")
        )
    filepath = os.path.join(dirpath, filename)
    data = get_all_current_info()
    if not os.path.exists(dirpath):
        os.makedirs(dirpath)

    with open(filepath, "w") as file_stream:
        json.dump(data, file_stream, indent=4)
    return filepath
