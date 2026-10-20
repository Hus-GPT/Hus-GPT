# Single-cell Google Colab launcher

Paste this entire cell into the **existing** Colab notebook and run it. It mounts Drive only if it is not already mounted, fetches the latest public script from this repository on every run, and starts it. It does not create a new notebook.

```python
from pathlib import Path
import subprocess, sys, urllib.request, runpy

DRIVE = Path("/content/drive/MyDrive")
if not DRIVE.exists():
    from google.colab import drive
    drive.mount("/content/drive")

subprocess.check_call([
    sys.executable, "-m", "pip", "install", "-q",
    "requests", "beautifulsoup4", "rarfile"
])

LATEST_SCRIPT = "https://raw.githubusercontent.com/Hus-GPT/Hus-GPT/main/edunet_collector.py"
LOCAL_SCRIPT = Path("/content/edunet_collector_latest.py")
urllib.request.urlretrieve(LATEST_SCRIPT, LOCAL_SCRIPT)

print("Fetched latest collector:", LATEST_SCRIPT)
source = LOCAL_SCRIPT.read_text(encoding="utf-8")
compile(source, str(LOCAL_SCRIPT), "exec")  # fail early if the downloaded script is malformed
runpy.run_path(str(LOCAL_SCRIPT), run_name="__main__")
```

The destination is `/content/drive/MyDrive/Edunet_Educational_Library`. The script reuses existing reports, writes a new run report, and does not intentionally delete existing user files. Archive files are removed only after successful extraction of at least one non-duplicate file. External video URLs are recorded in the report rather than downloaded automatically.

If the runtime has lost its Drive mount, Google may ask for authorization again. Google controls this requirement; the script cannot guarantee that authorization will never be requested in future sessions.
