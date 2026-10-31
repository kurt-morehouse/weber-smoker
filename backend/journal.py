"""Cook journal: structured notes and photos for each cook, on the Mac only.

Stored in data/journal/<csv stem>/:
    journal.json   {"proteins": [...], "setup": "", "outcome": "", "rating": null,
                    "notes": "", "photos": [{"file", "taken_at", "caption"}]}
    photos/        JPEGs

Uploaded photos are re-saved as JPEG with all metadata removed (so no
location data), rotated upright first. HEIC photos (iPhone) are converted if
the optional pillow-heif package is installed; otherwise they're refused with
a message saying so. A photo's taken_at comes from its EXIF date if it has
one, so it can be shown as a dot on the charts at that moment.
"""
import io
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

FIELDS = {"proteins": list, "setup": str, "outcome": str, "rating": (int, type(None)),
          "notes": str}
MAX_PHOTO_BYTES = 30 * 1024 * 1024
STEM_RE = re.compile(r"^[A-Za-z0-9._-]+$")

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC = True
except Exception:
    HEIC = False


def empty():
    return {"proteins": [], "setup": "", "outcome": "", "rating": None, "notes": "",
            "photos": []}


def config():
    return {"heic": HEIC, "fields": list(FIELDS), "max_photo_mb": MAX_PHOTO_BYTES // 1024 // 1024}


class Journal:
    def __init__(self, journal_dir):
        self.root = Path(journal_dir)

    def _dir(self, stem):
        if not STEM_RE.match(stem or "") or stem.startswith("."):
            raise ValueError("bad cook name")
        return self.root / stem

    def get(self, stem):
        p = self._dir(stem) / "journal.json"
        if not p.exists():
            return empty()
        data = json.loads(p.read_text())
        return {**empty(), **data}

    def save(self, stem, data):
        current = self.get(stem)
        for key, typ in FIELDS.items():
            if key in data:
                if not isinstance(data[key], typ):
                    raise ValueError(f"{key} has the wrong type")
                current[key] = data[key]
        if isinstance(current.get("rating"), int) and not 0 <= current["rating"] <= 5:
            raise ValueError("rating must be 0-5")
        current["proteins"] = [str(x)[:80] for x in current["proteins"]][:20]
        self._write(stem, current)
        return current

    def _write(self, stem, data):
        d = self._dir(stem)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "journal.json.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(d / "journal.json")

    def add_photo(self, stem, filename, raw, caption=""):
        from PIL import Image, ImageOps
        if len(raw) > MAX_PHOTO_BYTES:
            raise ValueError("photo too large")
        if filename.lower().endswith((".heic", ".heif")) and not HEIC:
            raise ValueError("HEIC photos need the pillow-heif package: "
                             "pip install pillow-heif, or export the photo as JPEG")
        try:
            img = Image.open(io.BytesIO(raw))
            taken = _exif_time(img)
            img = ImageOps.exif_transpose(img).convert("RGB")
        except Exception as e:
            raise ValueError(f"not an image I can read: {e}")
        img.thumbnail((2400, 2400))
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.jpg"
        d = self._dir(stem) / "photos"
        d.mkdir(parents=True, exist_ok=True)
        img.save(d / name, "JPEG", quality=85)       # no exif= : metadata dropped
        data = self.get(stem)
        data["photos"].append({"file": name, "taken_at": taken, "caption": caption[:200]})
        self._write(stem, data)
        return data

    def photo_path(self, stem, name):
        if not re.match(r"^[A-Za-z0-9_.-]+\.jpg$", name):
            raise ValueError("bad photo name")
        p = self._dir(stem) / "photos" / name
        if not p.is_file():
            raise ValueError("no such photo")
        return p

    def delete_photo(self, stem, name):
        p = self.photo_path(stem, name)
        p.unlink()
        data = self.get(stem)
        data["photos"] = [x for x in data["photos"] if x["file"] != name]
        self._write(stem, data)
        return data


def _exif_time(img):
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)     # DateTimeOriginal, DateTime
        if raw:
            return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").astimezone().isoformat(timespec="seconds")
    except Exception:
        pass
    return None
