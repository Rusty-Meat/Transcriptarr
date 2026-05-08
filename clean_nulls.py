"""One-shot: strip null bytes from all .py source files in this folder.

Run with your real Windows Python so it writes to the actual file system:
    "C:\\Second Brain\\Second Brain\\.venv\\Scripts\\python.exe" clean_nulls.py
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent

for f in HERE.glob("*.py"):
    if f.name == "clean_nulls.py":
        continue
    data = f.read_bytes()
    cleaned = data.replace(b"\x00", b"")
    if data != cleaned:
        f.write_bytes(cleaned)
        print(f"{f.name}: {len(data):,} -> {len(cleaned):,} bytes "
              f"({len(data)-len(cleaned)} null bytes removed)")
    else:
        print(f"{f.name}: clean")
