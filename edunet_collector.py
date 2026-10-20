#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Continue an existing EduNET Drive project; discover and fetch course-specific public resources."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                           "requests", "beautifulsoup4", "rarfile"])
    import requests
    from bs4 import BeautifulSoup

try:
    import rarfile
except Exception:
    rarfile = None

ROOT_DEFAULT = Path("/content/drive/MyDrive/Edunet_Educational_Library")
BASE = "https://www.edunet.bh/"
KNOWN_ARCHIVE = (
    "https://www.edunet.bh/e_content/level_3/stage_10/subject_ID_26/"
    "Part_3/others/AGA103QA/AGA103QA.zip"
)
COURSES = {
    "AGA103": {"ar": "أجا 103", "terms": ["aga103", "أجا 103", "اجا 103"]},
    "ENG301": {"ar": "إنج 301", "terms": ["eng301", "english", "إنج 301", "انج 301"]},
    "BIO316": {"ar": "حيا 316", "terms": ["bio316", "biology", "حيا 316", "حيا316"]},
    "DIN301": {"ar": "دين 301", "terms": ["din301", "islamic", "دين 301", "دين301"]},
    "MATH364": {"ar": "ريض 364", "terms": ["math364", "mathematics", "math", "ريض 364", "ريض364"]},
    "ARB301": {"ar": "عرب 301", "terms": ["arb301", "arabic", "عرب 301", "عرب301"]},
    "PHY311": {"ar": "فيز 311", "terms": ["phy311", "physics", "فيز 311", "فيز311"]},
    "CHEM315": {"ar": "كيم 315", "terms": ["chem315", "chemistry", "كيم 315", "كيم315"]},
    "WATAN301": {"ar": "وطن 301", "terms": ["watan301", "citizenship", "وطن 301", "وطن301"]},
}
RESOURCE_EXTS = {
    ".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".odt", ".ods", ".odp", ".txt", ".rtf",
    ".mp4", ".m4v", ".mov", ".avi", ".mp3", ".m4a", ".wav", ".webm"
}
ARCHIVE_EXTS = {".zip", ".rar"}
MAX_PAGES = int(os.getenv("EDUNET_MAX_PAGES", "180"))
MAX_DOWNLOADS = int(os.getenv("EDUNET_MAX_DOWNLOADS", "500"))
TIMEOUT = 35
UA = "Mozilla/5.0 (compatible; EduNETCourseResourceCollector/1.0)"

def log(msg):
    print(msg, flush=True)

def safe_name(value, fallback="resource"):
    value = unquote(str(value or "")).strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value[:150] or fallback)

def course_hits(text):
    s = str(text or "").lower()
    hits = set()
    for code, meta in COURSES.items():
        if code.lower() in s:
            hits.add(code)
            continue
        # Terms such as "math" or "english" are useful only with the course number nearby,
        # or when present in a candidate's descriptive text; Arabic course labels require code/number.
        for term in meta["terms"]:
            t = term.lower()
            if t in ("math", "english", "biology", "islamic", "arabic", "physics", "chemistry", "citizenship", "mathematics"):
                continue
            if t in s:
                hits.add(code)
                break
    return hits

def text_course_hits(text):
    s = str(text or "").lower()
    hits = set()
    for code, meta in COURSES.items():
        if code.lower() in s:
            hits.add(code)
            continue
        for term in meta["terms"]:
            if term.lower() in s:
                hits.add(code)
                break
    return hits

def load_urls_from_json(path):
    found = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return found
    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str):
            for match in re.findall(r'https?://[^\s"\'<>]+', obj):
                u = match.rstrip(".,);]")
                if urlparse(u).scheme in ("http", "https"):
                    found.add(u)
    walk(data)
    return found

def discover_existing(root):
    report_dir = root / "reports"
    urls = set()
    if report_dir.exists():
        for p in report_dir.rglob("*.json"):
            urls.update(load_urls_from_json(p))
    # Explicit known resource already validated in the prior project work.
    urls.add(KNOWN_ARCHIVE)
    return urls

def local_course_from_path(url, anchor_text=""):
    """High-confidence match: course code or explicit numbered Arabic course label."""
    combined = (url + " " + anchor_text).lower()
    strong = set()
    for code, meta in COURSES.items():
        if code.lower() in combined or any(
            t.lower() in combined for t in meta["terms"] if any(ch.isdigit() for ch in t)
        ):
            strong.add(code)
    return strong

def classify_resource(url, anchor="", title="", page_url="", context=""):
    """Classify cautiously using resource identity, nearby link text, and unique page context."""
    primary = " ".join([url or "", anchor or "", title or ""]).lower()
    strong = local_course_from_path(primary, "")
    if strong:
        return strong
    # Do not treat a broad subject folder (e.g. /English/ or /Islamic/) as proof
    # that a file belongs to one numbered course. Only explicit numbered labels qualify.
    rules = {
        "ENG301": ("eng_301", "eng-301", "english_301", "english-301", "إنج 301", "انج 301"),
        "DIN301": ("islamic_301", "islamic-301", "دين 301"),
        "MATH364": ("math_364", "math-364", "maths_364", "ريض 364"),
        "ARB301": ("arab_301", "arab-301", "arabic_301", "arabic-301", "عرب 301"),
        "WATAN301": ("watan_301", "watan-301", "citizenship_301", "وطن 301"),
        "BIO316": ("biology_316", "biology-316", "bio_316", "bio-316", "حيا 316"),
        "PHY311": ("physics_311", "physics-311", "phy_311", "phy-311", "فيز 311"),
        "CHEM315": ("chemistry_315", "chemistry-315", "chem_315", "chem-315", "كيم 315"),
        "AGA103": ("أجا 103", "اجا 103"),
    }
    hits = set()
    for code, terms in rules.items():
        if any(term in primary for term in terms):
            hits.add(code)
    if hits:
        return hits
    # Nearby row/card text often carries the subject label while the file URL is opaque.
    context_hits = text_course_hits(context)
    if len(context_hits) == 1:
        return context_hits
    # Inherit a course only when the source page URL/title itself identifies exactly one course.
    page_hits = local_course_from_path(page_url, "")
    if len(page_hits) == 1:
        return page_hits
    return set()

def is_edunet_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == "edunet.bh" or host.endswith(".edunet.bh")
    except Exception:
        return False

def normalize_url(url):
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url

def looks_like_resource(url):
    path = urlparse(url).path.lower()
    return Path(path).suffix.lower() in RESOURCE_EXTS

def extension_from_response(url, response):
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in RESOURCE_EXTS:
        return path_ext
    ctype = (response.headers.get("Content-Type") or "").split(";")[0].lower()
    return {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/vnd.rar": ".rar",
        "application/x-rar-compressed": ".rar",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
    }.get(ctype, "")

def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def inventory_existing(root):
    by_hash = {}
    count = 0
    for p in root.rglob("*"):
        if not p.is_file() or "reports" in p.parts or ".tmp" in p.name:
            continue
        try:
            digest = file_sha256(p)
            by_hash.setdefault(digest, p)
            count += 1
        except Exception:
            pass
    return by_hash, count

def safe_extract_zip(archive_path, dest):
    extracted = []
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive_path) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            target = (dest / member.filename).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError("Archive path traversal blocked")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(target)
    return extracted

def safe_extract_rar(archive_path, dest):
    if rarfile is None:
        raise RuntimeError("rarfile package unavailable")
    rf = rarfile.RarFile(archive_path)
    extracted = []
    dest_resolved = dest.resolve()
    for info in rf.infolist():
        if info.isdir():
            continue
        target = (dest / info.filename).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise ValueError("Archive path traversal blocked")
        target.parent.mkdir(parents=True, exist_ok=True)
        with rf.open(info) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)
        extracted.append(target)
    return extracted

def safe_extract(archive_path, dest):
    ext = archive_path.suffix.lower()
    if ext == ".zip":
        return safe_extract_zip(archive_path, dest)
    if ext == ".rar":
        return safe_extract_rar(archive_path, dest)
    if ext == ".7z":
        raise RuntimeError("7z archive detected; extractor not installed")
    raise RuntimeError("Unsupported archive format")

def valid_download(path, ext):
    try:
        if not path.exists() or path.stat().st_size < 100:
            return False
        with path.open("rb") as f:
            head = f.read(16)
        if ext == ".pdf":
            return b"%PDF-" in head
        if ext == ".zip":
            return head.startswith(b"PK")
        if ext == ".rar":
            return head.startswith(b"Rar!")
        if ext in {".mp4", ".m4v", ".mov"}:
            return len(head) >= 8
        return True
    except Exception:
        return False


AJAX_HOME = "https://www.edunet.bh/Econtent/Eduecontent_P"
AJAX_ENDPOINTS = {
    "levels": "https://www.edunet.bh/Econtent/get_A_Levels_P",
    "subjects": "https://www.edunet.bh/Econtent/get_Subjects_P2",
    "courses": "https://www.edunet.bh/Econtent/get_Courses_P2",
}

def _ajax_html(response):
    """Return HTML from an AJAX response, including JSON-wrapped HTML fragments."""
    body = response.text or ""
    if "html" in (response.headers.get("Content-Type") or "").lower() or "<" in body:
        try:
            data = response.json()
            if isinstance(data, dict):
                for value in data.values():
                    if isinstance(value, str) and "<" in value:
                        return value
        except Exception:
            pass
        return body
    try:
        data = response.json()
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, str) and "<" in value:
                    return value
    except Exception:
        pass
    return body

def _tag_attrs(tag):
    pieces = [tag.get_text(" ", strip=True)]
    for key, value in tag.attrs.items():
        if isinstance(value, (list, tuple)):
            value = " ".join(str(x) for x in value)
        pieces.extend([str(key), str(value)])
    return " ".join(pieces)

def _ids_from_html(html, key):
    """Extract IDs from data attributes, ordinary attributes, and inline handlers."""
    soup = BeautifulSoup(html or "", "html.parser")
    pattern = re.compile(r"""(?i)(?:data[-_]?|\b)""" + re.escape(key) + r"""\s*['"]?\s*[:=]\s*['"]?(\d+)""")
    found = set()
    for tag in soup.find_all(True):
        blob = _tag_attrs(tag)
        found.update(int(m.group(1)) for m in pattern.finditer(blob))
    # Some legacy pages pass IDs in JavaScript calls rather than data attributes.
    found.update(int(m.group(1)) for m in pattern.finditer(html or ""))
    return found

def _level_pairs(html):
    """Extract grade/part pairs from rendered level controls without guessing IDs."""
    soup = BeautifulSoup(html or "", "html.parser")
    pairs = set()
    for tag in soup.find_all(True):
        blob = _tag_attrs(tag)
        grade_matches = re.findall(r"""(?i)(?:gradeid|grade_id|data-grade)\s*['"]?\s*[:=]\s*['"]?(\d+)""", blob)
        part_matches = re.findall(r"""(?i)(?:partid|part_id|data-part)\s*['"]?\s*[:=]\s*['"]?(\d+)""", blob)
        if grade_matches and part_matches:
            for grade in grade_matches:
                for part in part_matches:
                    pairs.add((int(grade), int(part)))
    for match in re.finditer(r"""(?is)(?:gradeid|grade_id)\s*['"]?\s*[:=]\s*['"]?(\d+).*?(?:partid|part_id)\s*['"]?\s*[:=]\s*['"]?(\d+)""", html or ""):
        pairs.add((int(match.group(1)), int(match.group(2))))
    # Some level controls encode the two IDs positionally in an inline JavaScript call.
    for match in re.finditer(r"""(?i)get_Subjects_P2\s*\(\s*(\d+)\s*,\s*(\d+)""", html or ""):
        pairs.add((int(match.group(1)), int(match.group(2))))
    return pairs

def _ajax_links(html, source_url, course_context=None):
    """Collect direct resources and navigational links from an AJAX HTML fragment."""
    resources = {}
    videos = []
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(["a", "iframe", "source", "embed"]):
        href = tag.get("href") or tag.get("src")
        if not href:
            continue
        target = urljoin(source_url, str(href).strip())
        if urlparse(target).scheme not in ("http", "https"):
            continue
        anchor = tag.get_text(" ", strip=True) or tag.get("title") or tag.get("aria-label") or ""
        context = " ".join([anchor, tag.get("title") or "", tag.parent.get_text(" ", strip=True)[:400] if tag.parent else ""])
        hits = classify_resource(target, anchor, tag.get("title") or "", source_url, context)
        if not hits and course_context and len(course_context) == 1:
            # Only inherit a context that is already a specific numbered course.
            hits = set(course_context)
        ext = Path(urlparse(target).path).suffix.lower()
        if ext in RESOURCE_EXTS and is_edunet_url(target):
            resources.setdefault(target, {"courses": set(), "anchor": anchor, "source_page": source_url, "extension": ext})
            resources[target]["courses"].update(hits)
        elif any(host in (urlparse(target).hostname or "").lower() for host in ("youtube.com", "youtu.be", "vimeo.com")):
            if hits:
                videos.append({"url": target, "courses": sorted(hits), "title": anchor, "source_page": source_url})
    return resources, videos

def discover_ajax_resources(session):
    """Walk EduNET's observed AJAX sequence: levels -> subjects -> courses."""
    candidates = {}
    videos = []
    inspected = [AJAX_HOME]
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": AJAX_HOME,
        "Origin": "https://www.edunet.bh",
        "Accept": "text/html, */*; q=0.01",
    }

    def post_fragment(endpoint, payload=None):
        response = session.post(endpoint, data=payload or {}, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        return _ajax_html(response)

    try:
        # Load the landing page first to establish session cookies and any anti-forgery state.
        landing = session.get(AJAX_HOME, headers={"User-Agent": UA, "Accept-Language": "ar,en-US;q=0.8,en;q=0.6"}, timeout=TIMEOUT)
        inspected.append(landing.url)
        if landing.status_code == 200:
            headers["Referer"] = landing.url
        levels_html = post_fragment(AJAX_ENDPOINTS["levels"])
        inspected.append(AJAX_ENDPOINTS["levels"])
        level_pairs = _level_pairs(levels_html)
        # The reported working example is gradeid=10, partid=3. Use it only as a fallback
        # when the levels fragment does not expose IDs in a machine-readable form.
        if not level_pairs:
            level_pairs = {(10, 3)}
            log("[AJAX] Level IDs not parseable from response; trying reported fallback gradeid=10, partid=3.")
        log(f"[AJAX] Level/part combinations discovered: {len(level_pairs)}")

        subject_jobs = {}
        for grade_id, part_id in sorted(level_pairs):
            try:
                subjects_html = post_fragment(AJAX_ENDPOINTS["subjects"], {"gradeid": grade_id, "partid": part_id})
                inspected.append(f"{AJAX_ENDPOINTS['subjects']} [gradeid={grade_id},partid={part_id}]")
                links, found_videos = _ajax_links(subjects_html, AJAX_ENDPOINTS["subjects"])
                videos.extend(found_videos)
                for url, meta in links.items():
                    candidates.setdefault(url, meta)
                subject_ids = _ids_from_html(subjects_html, "subjectid")
                log(f"[AJAX] gradeid={grade_id}, partid={part_id}: subject IDs={len(subject_ids)}")
                for subject_id in sorted(subject_ids):
                    subject_jobs.setdefault(subject_id, (grade_id, part_id, subjects_html))
            except Exception as exc:
                log(f"[AJAX-WARN] subjects gradeid={grade_id},partid={part_id}: {type(exc).__name__}: {exc}")

        log(f"[AJAX] Unique subject IDs queued: {len(subject_jobs)}")
        for subject_id, (grade_id, part_id, subject_html) in sorted(subject_jobs.items()):
            try:
                courses_html = post_fragment(AJAX_ENDPOINTS["courses"], {"subjectid": subject_id})
                source = f"{AJAX_ENDPOINTS['courses']} [subjectid={subject_id}]"
                inspected.append(source)
                # Use only explicit course-code context from the returned course fragment.
                fragment_text = BeautifulSoup(courses_html, "html.parser").get_text(" ", strip=True)
                context_hits = text_course_hits(fragment_text)
                context = context_hits if len(context_hits) == 1 else None
                links, found_videos = _ajax_links(courses_html, source, context)
                videos.extend(found_videos)
                for url, meta in links.items():
                    candidates.setdefault(url, {"courses": set(), **meta})
                    candidates[url]["courses"].update(meta["courses"])
                # Some fragments expose resource URLs as plain attributes or JSON strings.
                for raw_url in re.findall(r"""https?://[^\s'"<>]+""", courses_html or ""):
                    raw_url = raw_url.rstrip(".,);]")
                    if looks_like_resource(raw_url) and is_edunet_url(raw_url):
                        hits = local_course_from_path(raw_url, "") or (set(context) if context else set())
                        candidates.setdefault(raw_url, {"courses": set(), "anchor": "URL found in AJAX response", "source_page": source, "extension": Path(urlparse(raw_url).path).suffix.lower()})
                        candidates[raw_url]["courses"].update(hits)
            except Exception as exc:
                log(f"[AJAX-WARN] courses subjectid={subject_id}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        log(f"[AJAX-WARN] AJAX discovery unavailable: {type(exc).__name__}: {exc}")
    flipbook_pages = set(FLIPBOOK_SEED_PAGES)
    # Preserve any flipbook entry pages exposed in dynamic AJAX fragments.
    for inspected_url in inspected:
        if isinstance(inspected_url, str) and "/e_content/" in inspected_url.lower() and inspected_url.lower().endswith(".html"):
            flipbook_pages.add(inspected_url)
    # AJAX fragments can contain relative index.html links; scan all recorded response text is
    # intentionally avoided here because only verified URLs should become crawl targets.
    log(f"[AJAX] Resource URLs discovered directly from AJAX fragments: {len(candidates)}")
    return candidates, videos, inspected, flipbook_pages


FLIPBOOK_SEED_PAGES = [
    "https://www.edunet.bh/e_content/level_3/stage_10/subject_ID_28/Part_3/e_books/Math363SB/Math363SB/index.html",
]
FLIPBOOK_PATH_RE = re.compile(
    r"(?i)/e_content/level_(\d+)/stage_(\d+)/subject_ID_(\d+)/Part_(\d+)/([^/]+)/"
)

def structural_path_info(url):
    """Read verified EduNET hierarchy values from a resource URL."""
    path = urlparse(url).path
    match = FLIPBOOK_PATH_RE.search(path)
    if not match:
        return {}
    level, gradeid, subjectid, partid, category = match.groups()
    return {
        "level": int(level), "gradeid": int(gradeid),
        "subjectid": int(subjectid), "partid": int(partid),
        "category": category,
    }

def _urls_in_markup(html, page_url):
    """Extract URLs from ordinary links, download controls, and inline JS."""
    found = set()
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(True):
        for attr in ("href", "src", "data", "data-href", "data-url", "data-download", "formaction"):
            value = tag.get(attr)
            if not value:
                continue
            value = str(value).strip()
            if value.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            target = urljoin(page_url, value)
            if urlparse(target).scheme in ("http", "https"):
                found.add(target)
        onclick = tag.get("onclick") or ""
        for raw in re.findall(r"""(?:https?://[^\\s'"<>]+|(?:\\.\\.?/|/)[^\\s'"<>]+)""", onclick):
            target = urljoin(page_url, raw.rstrip(".,);]"))
            if urlparse(target).scheme in ("http", "https"):
                found.add(target)
    for raw in re.findall(r"""https?://[^\\s'"<>]+""", html or ""):
        target = raw.rstrip(".,);]")
        if urlparse(target).scheme in ("http", "https"):
            found.add(target)
    return found

def discover_flipbook_downloads(session, page_urls):
    """Inspect flipbook HTML pages and resolve their explicit or conventional PDF download links."""
    candidates = {}
    inspected = []
    queue = deque((u, 0) for u in sorted(set(page_urls)))
    seen = set()
    while queue and len(seen) < 80:
        page_url, depth = queue.popleft()
        if page_url in seen or not is_edunet_url(page_url):
            continue
        seen.add(page_url)
        try:
            response = session.get(page_url, timeout=TIMEOUT)
            inspected.append(response.url)
            if response.status_code != 200 or "text/html" not in (response.headers.get("Content-Type") or "").lower():
                continue
            html = response.text or ""
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            page_text = soup.get_text(" ", strip=True)[:12000]
            page_hits = classify_resource(page_url, title, title, page_url, page_text)
            page_info = structural_path_info(response.url)
            urls = _urls_in_markup(html, response.url)
            base_dir = response.url.rsplit("/", 1)[0] + "/"
            # If a visible Download control exists but its URL is assembled by JavaScript,
            # probe the conventional sibling PDF; download_one will validate the response.
            has_download_control = bool(re.search(r"(?i)download|تحميل|تنزيل", page_text) or
                                        re.search(r"(?i)download|تحميل|تنزيل", html))
            if has_download_control and response.url.lower().endswith("/index.html"):
                book_folder = response.url.rsplit("/", 2)[-2]
                urls.add(urljoin(base_dir, book_folder + ".pdf"))
                urls.add(urljoin(base_dir, "files/mobile/" + book_folder + ".pdf"))
            for target in sorted(urls):
                if not is_edunet_url(target):
                    continue
                path = urlparse(target).path.lower()
                ext = Path(path).suffix.lower()
                if ext in RESOURCE_EXTS:
                    anchor = ""
                    for tag in soup.find_all(["a", "button", "iframe", "source", "embed"]):
                        vals = [str(tag.get(k, "")) for k in ("href", "src", "data-href", "data-url", "onclick")]
                        if any(v and (v in target or urljoin(response.url, v) == target) for v in vals):
                            anchor = tag.get_text(" ", strip=True) or tag.get("title") or tag.get("aria-label") or ""
                            break
                    hits = classify_resource(target, anchor, title, response.url, page_text)
                    if not hits and len(page_hits) == 1:
                        hits = set(page_hits)
                    meta = {
                        "courses": set(hits), "anchor": anchor or "resource linked from flipbook",
                        "source_page": response.url, "extension": ext,
                        "structural_path": structural_path_info(target),
                    }
                    if target not in candidates:
                        candidates[target] = meta
                    else:
                        candidates[target]["courses"].update(hits)
                elif depth == 0 and path.endswith(".html") and target != response.url:
                    # Follow only nearby HTML pages within the same EduNET content tree.
                    if "/e_content/" in path and target not in seen:
                        queue.append((target, depth + 1))
        except Exception as exc:
            log(f"[FLIPBOOK-WARN] {page_url}: {type(exc).__name__}: {exc}")
    log(f"[FLIPBOOK] HTML pages inspected: {len(seen)}; direct resource candidates: {len(candidates)}")
    return candidates, inspected

def parse_candidate_pages(root, session):
    seed_urls = discover_existing(root)
    # Direct resource URLs in prior reports are candidates too; never require crawling them as HTML.
    seed_resource_candidates = {}
    for seed in seed_urls:
        if looks_like_resource(seed) and is_edunet_url(seed):
            hits = local_course_from_path(seed, "")
            if hits:
                seed_resource_candidates[seed] = {
                    "courses": set(hits), "anchor": "reused URL from existing report",
                    "source_page": "existing report", "extension": Path(urlparse(seed).path).suffix.lower()
                }
    # Only crawl EduNET pages from existing reports; binary resources are handled separately.
    page_queue = deque()
    for u in sorted(seed_urls):
        if not is_edunet_url(u):
            continue
        parsed = urlparse(u)
        path = parsed.path.lower()
        if looks_like_resource(u) or path.endswith(("/", ".zip", ".rar", ".pdf")):
            continue
        if "/error/" in path or "aspxerrorpath=" in parsed.query.lower():
            continue
        page_queue.append((u, 0))
    visited = set()
    candidates = seed_resource_candidates  # URL -> {courses, anchor, source_page, kind}
    video_links = []
    page_errors_seen = set()
    while page_queue and len(visited) < MAX_PAGES:
        url, depth = page_queue.popleft()
        url = normalize_url(url)
        if url in visited:
            continue
        visited.add(url)
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code != 200 or "text/html" not in (resp.headers.get("Content-Type") or "").lower():
                continue
            final_path = urlparse(resp.url).path.lower()
            if "/error/" in final_path or "aspxerrorpath=" in urlparse(resp.url).query.lower():
                continue
            # Some EduNET directory routes redirect to a server error page; do not crawl them further.
            if "somethingwronghit" in final_path:
                continue
            soup = BeautifulSoup(resp.content, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            page_text = soup.get_text(" ", strip=True)[:25000]
            page_hits = local_course_from_path(url, title)
            # If the page visibly names exactly one target course, its links can inherit that context.
            visible_page_hits = text_course_hits(page_text)
            if len(visible_page_hits) == 1:
                page_hits = visible_page_hits
            # Don't assign every course mentioned on a general landing page.
            for tag in soup.find_all(["a", "iframe", "source"]):
                href = tag.get("href") or tag.get("src")
                if not href:
                    continue
                target = urljoin(resp.url, href.strip())
                if urlparse(target).scheme not in ("http", "https"):
                    continue
                anchor = tag.get_text(" ", strip=True) or tag.get("title") or tag.get("aria-label") or ""
                context = " ".join([anchor, tag.get("title") or "", tag.parent.get_text(" ", strip=True)[:300] if tag.parent else ""])
                hits = classify_resource(target, anchor, tag.get("title") or "", url, context)
                if not hits and len(visible_page_hits) == 1:
                    hits = visible_page_hits
                path = urlparse(target).path.lower()
                ext = Path(path).suffix.lower()
                if ext in RESOURCE_EXTS:
                    if hits:
                        candidates.setdefault(target, {"courses": set(), "anchor": anchor, "source_page": url, "extension": ext})
                        candidates[target]["courses"].update(hits)
                    elif is_edunet_url(target) and looks_like_resource(target):
                        # Save unclassified candidates for audit only; do not download blindly.
                        candidates.setdefault(target, {"courses": set(), "anchor": anchor, "source_page": url, "extension": ext})
                elif any(host in (urlparse(target).hostname or "").lower() for host in ("youtube.com", "youtu.be", "vimeo.com")):
                    inherited_video_hits = hits or (page_hits if len(page_hits) == 1 else set())
                    if inherited_video_hits:
                        video_links.append({"url": target, "courses": sorted(inherited_video_hits), "title": anchor, "source_page": url})
                elif is_edunet_url(target) and depth < 2 and target not in visited:
                    # Avoid directory endpoints: several EduNET folders redirect to a 500 error page.
                    target_path = urlparse(target).path.lower()
                    if target_path.endswith("/") or "/error/" in target_path:
                        continue
                    low = (target + " " + anchor).lower()
                    if any(k in low for k in ("e_content", "subject", "course", "lesson", "exam", "test", "book", "content", "stage_", "level_")):
                        page_queue.append((target, depth + 1))
        except Exception as e:
            if url not in page_errors_seen:
                page_errors_seen.add(url)
                log(f"[PAGE-ERROR] {url}: {type(e).__name__}: {e}")
    return candidates, video_links, visited

def download_one(session, url, course, dest_root, hash_index, manifest):
    dest_dir = dest_root / course
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = safe_name(Path(unquote(urlparse(url).path)).name, "resource")
    temp = dest_dir / (name + ".part")
    try:
        with session.get(url, stream=True, timeout=TIMEOUT, allow_redirects=True) as resp:
            if resp.status_code != 200:
                manifest["failures"].append({"url": url, "course": course, "error": f"HTTP {resp.status_code}"})
                return False
            ext = extension_from_response(url, resp)
            if not ext:
                manifest["skipped"].append({"url": url, "course": course, "reason": "unsupported content type"})
                return False
            if not Path(name).suffix:
                name += ext
            final = dest_dir / name
            if final.exists() and valid_download(final, ext):
                digest = file_sha256(final)
                hash_index.setdefault(digest, final)
                manifest["existing"].append({"url": url, "course": course, "path": str(final), "sha256": digest})
                return True
            with temp.open("wb") as f:
                for chunk in resp.iter_content(1024 * 256):
                    if chunk:
                        f.write(chunk)
        if not valid_download(temp, ext):
            temp.unlink(missing_ok=True)
            manifest["failures"].append({"url": url, "course": course, "error": "file validation failed"})
            return False
        digest = file_sha256(temp)
        if digest in hash_index:
            duplicate = hash_index[digest]
            temp.unlink(missing_ok=True)
            manifest["duplicates"].append({"url": url, "course": course, "duplicate_of": str(duplicate), "sha256": digest})
            return True
        final = dest_dir / safe_name(name)
        if final.exists():
            final = dest_dir / f"{Path(name).stem}_{digest[:10]}{ext}"
        temp.replace(final)
        hash_index[digest] = final
        record = {"url": url, "course": course, "path": str(final), "bytes": final.stat().st_size, "sha256": digest, "extension": ext}
        manifest["downloaded"].append(record)
        if ext in ARCHIVE_EXTS:
            try:
                extract_dir = dest_dir / (safe_name(final.stem) + "_extracted")
                extract_dir.mkdir(parents=True, exist_ok=True)
                extracted = safe_extract(final, extract_dir)
                good = []
                for p in extracted:
                    if p.is_file() and p.stat().st_size:
                        h = file_sha256(p)
                        if h in hash_index:
                            # Duplicate extracted content: remove the redundant extracted copy.
                            p.unlink(missing_ok=True)
                            manifest["duplicates"].append({"path": str(p), "duplicate_of": str(hash_index[h]), "sha256": h})
                        else:
                            hash_index[h] = p
                            good.append(str(p))
                record["extracted_count"] = len(good)
                record["extracted_files"] = good[:1000]
                # Remove archive when extraction succeeded and yielded files, per user's desired output.
                if good:
                    final.unlink(missing_ok=True)
                    record["archive_removed_after_extraction"] = True
            except Exception as e:
                record["extraction_error"] = f"{type(e).__name__}: {e}"
                manifest["failures"].append({"url": url, "course": course, "error": "archive extraction failed: " + str(e), "archive_kept": str(final)})
        return True
    except Exception as e:
        temp.unlink(missing_ok=True)
        manifest["failures"].append({"url": url, "course": course, "error": f"{type(e).__name__}: {e}"})
        return False

def main():
    root = ROOT_DEFAULT
    if not root.exists():
        # Colab Drive mount is expected to be handled by the launcher before this script.
        root.mkdir(parents=True, exist_ok=True)
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    retry = Retry(
        total=4, connect=4, read=3, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": UA, "Accept-Language": "ar,en-US;q=0.8,en;q=0.6"})
    log("=" * 72)
    log("EduNET collector — resume existing project")
    log(f"Drive root: {root}")
    log(f"Course targets: {len(COURSES)}")
    log("Scanning existing reports; no previous report is overwritten.")
    old_hashes, existing_count = inventory_existing(root)
    log(f"Existing files inventoried: {existing_count}; unique content hashes: {len(old_hashes)}")
    ajax_candidates, ajax_videos, ajax_inspected, flipbook_pages = discover_ajax_resources(session)
    flipbook_candidates, flipbook_inspected = discover_flipbook_downloads(session, flipbook_pages)
    candidates, video_links, visited = parse_candidate_pages(root, session)
    for flipbook_url, flipbook_meta in flipbook_candidates.items():
        if flipbook_url not in candidates:
            candidates[flipbook_url] = flipbook_meta
        else:
            candidates[flipbook_url]["courses"].update(flipbook_meta.get("courses", set()))
    ajax_inspected.extend(flipbook_inspected)
    # Prefer the site's observed AJAX navigation over assumptions based on static URL crawling.
    for ajax_url, ajax_meta in ajax_candidates.items():
        if ajax_url not in candidates:
            candidates[ajax_url] = ajax_meta
        else:
            candidates[ajax_url]["courses"].update(ajax_meta.get("courses", set()))
    video_links.extend(ajax_videos)
    visited.update(ajax_inspected)
    # The previously validated AGA103 archive is a known candidate.
    candidates.setdefault(KNOWN_ARCHIVE, {"courses": {"AGA103"}, "anchor": "AGA103QA exam archive", "source_page": BASE, "extension": ".zip"})
    manifest = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root),
        "visited_pages": sorted(visited),
        "visited_page_count": len(visited),
        "candidate_count": len(candidates),
        "course_targets": {k: v["ar"] for k, v in COURSES.items()},
        "candidates": [],
        "downloaded": [],
        "existing": [],
        "duplicates": [],
        "skipped": [],
        "failures": [],
        "video_links": video_links,
        "notes": [
            "Resource classification uses explicit course codes, nearby link/card text, and unique course context on source pages.",
            "Unclassified resources remain in the audit report and are not downloaded blindly.",
            "External video links are recorded, not downloaded automatically.",
            "Transient HTTP errors are retried with backoff.",
        ],
        "unclassified_candidate_count": 0,
        "unclassified_candidates": [],
        "course_candidate_counts": {},
    }
    selected = []
    review_selected = []
    for url, meta in candidates.items():
        courses = sorted(meta["courses"])
        candidate_record = {"url": url, "courses": courses, "anchor": meta.get("anchor", ""), "source_page": meta.get("source_page", ""), "extension": meta.get("extension", "")}
        manifest["candidates"].append(candidate_record)
        if courses:
            for course in courses:
                selected.append((url, course))
        else:
            manifest["unclassified_candidates"].append(candidate_record)
            # Preserve discoverable public resources without falsely assigning them to a course.
            # They are downloaded into a clearly separate review folder for later content inspection.
            if is_edunet_url(url) and looks_like_resource(url):
                review_selected.append((url, "_REVIEW_UNCLASSIFIED"))
    manifest["unclassified_candidate_count"] = len(manifest["unclassified_candidates"])
    # Keep course-specific and review-only resources separate; de-duplicate URL/folder pairs.
    selected = list(dict.fromkeys(selected))
    review_selected = list(dict.fromkeys(review_selected))
    selected = (selected + review_selected)[:MAX_DOWNLOADS]
    candidate_counts = defaultdict(int)
    for _, code in selected:
        candidate_counts[code] += 1
    manifest["course_candidate_counts"] = dict(candidate_counts)
    log(f"Pages inspected: {len(visited)}")
    log(f"Resource URLs discovered: {len(candidates)}")
    log(f"Download jobs (course-specific + review-only): {len(selected)}")
    log(f"Unclassified review-only jobs queued: {sum(1 for _, code in selected if code == '_REVIEW_UNCLASSIFIED')}")
    log(f"Video links recorded: {len(video_links)}")
    log(f"Unclassified resource candidates (audit only): {manifest['unclassified_candidate_count']}")
    for item in manifest["unclassified_candidates"][:10]:
        log("  [UNCLASSIFIED] " + item["url"])
    course_counts = defaultdict(int)
    for _, course in selected:
        course_counts[course] += 1
    for code in COURSES:
        log(f"  {code}: {course_counts.get(code, 0)} candidate job(s)")
    log(f"  Review-only (not assigned to a course): {course_counts.get('_REVIEW_UNCLASSIFIED', 0)} candidate job(s)")
    # AGA archive already present in the project is not downloaded again if same content is inventoried.
    for i, (url, course) in enumerate(selected, 1):
        log(f"[{i}/{len(selected)}] {course}: {url}")
        download_one(session, url, course, downloads, old_hashes, manifest)
        if i % 15 == 0:
            (reports / "edunet_download_progress.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # Write final reports atomically.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for name, obj in [
        (f"edunet_run_{stamp}.json", manifest),
        ("edunet_latest_run.json", manifest),
    ]:
        tmp = reports / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(reports / name)
    log("=" * 72)
    log(f"Downloaded: {len(manifest['downloaded'])}")
    log(f"Existing/reused: {len(manifest['existing'])}")
    log(f"Duplicates skipped: {len(manifest['duplicates'])}")
    log(f"Failures: {len(manifest['failures'])}")
    if manifest["failures"]:
        log("Failure details:")
        for item in manifest["failures"][:20]:
            log("  - " + json.dumps(item, ensure_ascii=False))
    log(f"Report: {reports / 'edunet_latest_run.json'}")
    log("IMPORTANT: Review course coverage and failures before treating the collection as complete.")

if __name__ == "__main__":
    main()
