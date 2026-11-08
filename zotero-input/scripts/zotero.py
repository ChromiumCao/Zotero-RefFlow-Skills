#!/usr/bin/env python3
"""Operate Zotero Desktop's local API and connector server.

This helper is dependency-free on purpose: it runs with Python 3 stdlib only,
and all Zotero calls go through the local Desktop HTTP surfaces on
http://127.0.0.1:23119.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import html
import io
from html.parser import HTMLParser
import json
import os
import platform
import posixpath
import re
import shutil
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import zipfile
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = os.environ.get("ZOTERO_LOCAL_BASE_URL", "http://127.0.0.1:23119")
LOCAL_API_PREF = "extensions.zotero.httpServer.localAPI.enabled"
LOCAL_USER = "/api/users/0"
API_VERSION_HEADERS = {"Zotero-API-Version": "3"}
CONNECTOR_HEADERS = {"X-Zotero-Connector-API-Version": "3"}

# CrossRef and NCBI ask callers to identify themselves with a reachable contact
# address. Supply one through ZOTERO_REF_EMAIL; no placeholder is baked in, so an
# unconfigured install sends only the tool name instead of a fake address.
CONTACT_EMAIL = os.environ.get("ZOTERO_REF_EMAIL", "").strip()
USER_AGENT = "ZoteroInputSkill/1.0" + (f" (mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL else "")
TEXT_LIMIT = 300
API_PAGE_LIMIT = 100
DEFAULT_TIMEOUT = 10.0  # increased from 2.0 to avoid spurious timeouts on large libraries


@dataclass(frozen=True)
class Response:
    status: int | None
    headers: dict[str, str]
    text: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "")


def dump_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=False))


def exit_with(message: str) -> None:
    raise SystemExit(message)


def zotero_roots() -> list[Path]:
    home = Path.home()
    system = platform.system()
    roots: list[Path] = []

    if system == "Darwin":
        roots.append(home / "Library/Application Support/Zotero")
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.extend([Path(appdata) / "Zotero/Zotero", Path(appdata) / "Zotero"])
    else:
        roots.extend(
            [
                home / ".zotero/zotero",
                home / ".var/app/org.zotero.Zotero/data/zotero",
            ]
        )

    # Useful fallback when scripts run under shells whose platform config is odd.
    roots.append(home / "Library/Application Support/Zotero")
    return list(dict.fromkeys(roots))


def profiles_ini_path() -> Path | None:
    for root in zotero_roots():
        candidate = root / "profiles.ini"
        if candidate.exists():
            return candidate
    return None


def profile_dir() -> Path | None:
    ini = profiles_ini_path()
    if ini is None:
        return None

    parser = configparser.RawConfigParser()
    parser.read(ini)
    root = ini.parent
    candidates: list[tuple[int, Path]] = []

    for section in parser.sections():
        if not section.lower().startswith("profile") or not parser.has_option(section, "Path"):
            continue
        raw_path = parser.get(section, "Path")
        path = (
            root / raw_path
            if parser.get(section, "IsRelative", fallback="1") == "1"
            else Path(raw_path)
        )
        score = 0
        if parser.get(section, "Default", fallback="0") == "1":
            score += 10
        if (path / "prefs.js").exists():
            score += 5
        candidates.append((score, path))

    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    profiles = sorted((root / "Profiles").glob("*.default*"))
    return profiles[0] if profiles else None


def prefs_file(profile: str | Path | None = None) -> Path | None:
    profile = Path(profile).expanduser().resolve() if profile else profile_dir()
    if profile is None:
        return None
    candidate = profile / "prefs.js"
    return candidate if candidate.exists() else None


def pref_pattern() -> re.Pattern[str]:
    return re.compile(r'user_pref\("' + re.escape(LOCAL_API_PREF) + r'",\s*(true|false)\s*\);')


def read_local_api_pref() -> bool | None:
    prefs = prefs_file()
    if prefs is None:
        return None
    match = pref_pattern().search(prefs.read_text(encoding="utf-8", errors="replace"))
    if match is None:
        return None
    return match.group(1) == "true"


def set_local_api_pref(enabled: bool, profile: str | Path | None = None) -> Path:
    prefs = prefs_file(profile) if profile else prefs_file()
    if prefs is None:
        exit_with("Could not find Zotero prefs.js. Start Zotero once, then retry.")

    backup = prefs.with_suffix(prefs.suffix + f".zotero-skill-backup-{int(time.time())}")
    shutil.copy2(prefs, backup)

    text = prefs.read_text(encoding="utf-8", errors="replace")
    new_line = f'user_pref("{LOCAL_API_PREF}", {str(enabled).lower()});'
    pattern = pref_pattern()
    if pattern.search(text):
        text = pattern.sub(new_line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + new_line + "\n"
    prefs.write_text(text, encoding="utf-8")
    return backup


def url_for(path: str, base_url: str | None = None) -> str:
    return (DEFAULT_BASE_URL if base_url is None else base_url).rstrip("/") + path


def request(
    path: str,
    *,
    method: str = "GET",
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Response:
    req_headers = dict(headers or {})
    body: bytes | None = None

    if path.startswith("/api"):
        req_headers.update({k: v for k, v in API_VERSION_HEADERS.items() if k not in req_headers})
    if path.startswith("/connector"):
        req_headers.update({k: v for k, v in CONNECTOR_HEADERS.items() if k not in req_headers})

    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, bytes):
            body = data
        else:
            body = str(data).encode("utf-8")

    try:
        req = urllib.request.Request(url_for(path), data=body, method=method, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return Response(
                status=response.status,
                headers=dict(response.headers.items()),
                text=response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        return Response(
            status=exc.code,
            headers=dict(exc.headers.items()),
            text=exc.read().decode("utf-8", errors="replace"),
            error=str(exc),
        )
    except Exception as exc:  # local server down, Zotero closed, malformed URL, etc.
        return Response(status=None, headers={}, text="", error=str(exc))


def parse_body(response: Response) -> Any:
    if "json" not in response.content_type.lower():
        return response.text
    try:
        return json.loads(response.text or "null")
    except json.JSONDecodeError:
        return response.text


def require_ok(response: Response, action: str) -> Response:
    if response.ok:
        return response
    detail = response.error or response.text[:TEXT_LIMIT] or "no response body"
    exit_with(f"{action} failed: status={response.status} detail={detail}")
    raise AssertionError("unreachable")


def api_response(path: str) -> Response:
    api_path = path if path.startswith("/api") else "/api" + path
    return require_ok(request(api_path), f"GET {api_path}")


def api_get(path: str) -> Any:
    return parse_body(api_response(path))


def query(params: dict[str, str | int | bool | None]) -> str:
    clean = {key: value for key, value in params.items() if value is not None}
    return urllib.parse.urlencode(clean)


def _profile_process(pid: int, profile: Path):
    """Resolve only the explicitly named Windows Zotero instance."""
    if platform.system() != "Windows" or pid <= 0:
        return None
    command = f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
        row = json.loads(result.stdout)
        executable = Path(row["ExecutablePath"])
        arguments = [part.strip('"') for part in shlex.split(row["CommandLine"], posix=False)]
        positions = [i for i, arg in enumerate(arguments[:-1]) if arg.lower() in {"-profile", "--profile"}]
        if (int(row["ProcessId"]) != pid or executable.name.lower() != "zotero.exe" or len(positions) != 1
                or Path(arguments[positions[0] + 1]).resolve() != profile):
            return None
        return executable
    except Exception:
        return None


def restart_zotero(wait_for_api: bool = True, *, profile=None, pid=None) -> bool:
    if not profile or not pid:
        print("Automatic restart needs an explicit profile and verified PID; restart this Zotero instance manually.", file=sys.stderr)
        return False
    profile = Path(profile).expanduser().resolve()
    executable = _profile_process(pid, profile)
    if not executable or not profile.is_dir():
        return False
    try:
        # Graceful close of this PID only; do not terminate other profiles.
        command = f"$instance = Get-Process -Id {int(pid)} -ErrorAction Stop; if (-not $instance.CloseMainWindow()) {{ exit 2 }}; $instance.WaitForExit(15000) | Out-Null; if (-not $instance.HasExited) {{ exit 3 }}"
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, timeout=20, check=False)
        if result.returncode:
            return False
        subprocess.Popen([str(executable), "-no-remote", "-profile", str(profile)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return False
    if not wait_for_api:
        return True
    for _ in range(30):
        if request("/api/", timeout=1).ok:
            return True
        time.sleep(0.5)
    return False


def creators_from_item(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for creator in data.get("creators", []) or []:
        name = creator.get("name") or " ".join(
            part for part in [creator.get("firstName"), creator.get("lastName")] if part
        )
        if name:
            names.append(name)
    return names


def year_from_date(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"(\d{4})", raw)
    return match.group(1) if match else None


DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.I)
PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{4,10})\b", re.I)
PUBMED_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{4,10})", re.I)


def normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    value = unicodedata.normalize("NFKC", raw).strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    match = DOI_RE.search(value)
    if match:
        value = match.group(0)
    value = value.strip().rstrip(".,;:)]}\u3002\uff0c\uff1b")
    return value.lower() if value else None


def normalize_pmid(raw: str | int | None) -> str | None:
    if raw is None:
        return None
    match = re.search(r"\d{4,10}", str(raw))
    return match.group(0) if match else None


def normalize_title(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip().casefold()
    text = re.sub(r"[\W_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if text and (contains_cjk(text) or len(text) >= 12) else None


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff\U00020000-\U0002ffff]", text))


def short_cjk_title(title: str | None) -> bool:
    return bool(title and contains_cjk(title) and len(re.sub(r"\s", "", title)) < 12)


FIELD_ALIASES = {
    "doi": {"doi", "do", "digital object identifier"},
    "pmid": {"pmid", "pubmed", "pubmed id"},
    "title": {"title", "article title", "paper title", "[title]", "\u9898\u540d", "\u6807\u9898", "\u6587\u732e\u6807\u9898", "\u8bba\u6587\u9898\u76ee"},
    "year": {"year", "publication year", "date", "\u5e74\u4efd", "\u5e74", "\u51fa\u7248\u5e74", "\u53d1\u8868\u5e74", "\u53d1\u8868\u65e5\u671f"},
    "first_author": {"author", "authors", "first author", "first_author", "\u4f5c\u8005", "\u7b2c\u4e00\u4f5c\u8005", "\u9996\u4f5c\u8005"},
    "label": {"label", "number", "reference number", "id", "\u7f16\u53f7", "\u5e8f\u53f7", "\u6587\u732e\u7f16\u53f7"},
    "journal": {"journal", "publicationtitle", "\u671f\u520a", "\u520a\u540d", "\u6765\u6e90", "\u6742\u5fd7"},
    "abstract": {"abstract", "abstractnote", "\u6458\u8981"},
}


def field_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def read_source_text(path: Path, encoding: str | None = None) -> tuple[str, str]:
    raw = path.read_bytes()
    choices = [encoding] if encoding else ["utf-8-sig", "gb18030"]
    for codec in choices:
        try:
            return raw.decode(codec, errors="strict"), codec
        except UnicodeDecodeError:
            pass
    raise ValueError(f"Cannot decode {path.name}; supply --encoding or export UTF-8")


def extract_doi(text: str | None) -> str | None:
    if not text:
        return None
    match = DOI_RE.search(unicodedata.normalize("NFKC", text))
    return normalize_doi(match.group(0)) if match else None


def extract_pmid(text: str | None) -> str | None:
    if not text:
        return None
    match = PMID_RE.search(text) or PUBMED_URL_RE.search(text)
    return normalize_pmid(match.group(1)) if match else None


def pmid_from_item(data: dict[str, Any]) -> str | None:
    for key in ["PMID", "pmid"]:
        value = normalize_pmid(data.get(key))
        if value:
            return value
    extra = data.get("extra")
    return extract_pmid(extra) if isinstance(extra, str) else None


def summarize_item(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", item)
    return {
        "key": item.get("key") or data.get("key"),
        "itemType": data.get("itemType"),
        "title": data.get("title"),
        "creators": creators_from_item(data),
        "year": year_from_date(data.get("date")),
        "DOI": data.get("DOI"),
        "PMID": pmid_from_item(data),
        "collections": data.get("collections") or [],
    }


def summarize_collection(collection: dict[str, Any]) -> dict[str, Any]:
    data = collection.get("data", collection)
    return {
        "key": collection.get("key") or data.get("key"),
        "name": data.get("name"),
        "parentCollection": data.get("parentCollection"),
        "version": collection.get("version"),
    }


def summarize_tag(tag: dict[str, Any]) -> dict[str, Any]:
    return {"tag": tag.get("tag"), "numItems": (tag.get("meta") or {}).get("numItems")}


def summarize_group(group: dict[str, Any]) -> dict[str, Any]:
    data = group.get("data", group)
    return {
        "id": group.get("id") or data.get("id"),
        "name": data.get("name"),
        "type": data.get("type"),
    }


def print_items(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        creators = ", ".join(row.get("creators") or [])
        print(
            f"{row.get('key') or '':10} "
            f"{row.get('itemType') or '':14} "
            f"{row.get('year') or '':4} "
            f"{row.get('title') or ''} | {creators}"
        )


def extract_bibtex_keys(text: str) -> list[str]:
    return re.findall(r"@\w+\s*\{\s*([^,\s]+)", text)


def count_bibtex_entries(text: str) -> int:
    return len(extract_bibtex_keys(text))


def _clean_heading(line: str) -> bool:
    text = line.strip().lower()
    return text in {"references", "bibliography", "reference", "works cited", "\u53c2\u8003\u6587\u732e", "\u53c2\u8003\u8d44\u6599"}


def _clean_raw_reference(line: str) -> str:
    line = re.sub(r"^\s*(\[\d+\]|\d+[\).])\s*", "", line)
    return re.sub(r"\s+", " ", line).strip()


def title_guess_from_raw(raw: str) -> str | None:
    text = _clean_raw_reference(raw)
    text = DOI_RE.sub("", text)
    text = PMID_RE.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .;")
    return text if normalize_title(text) else None


def reference_from_fields(fields: dict[str, Any], *, raw_text: str | None = None) -> dict[str, Any]:
    lowered = {field_key(str(k)): v for k, v in fields.items() if k is not None}

    def first_value(names: list[str]) -> str | None:
        for name in names:
            value = lowered.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    raw = raw_text or " ".join(str(v).strip() for v in fields.values() if str(v).strip())
    doi = normalize_doi(first_value(sorted(FIELD_ALIASES["doi"]))) or extract_doi(raw)
    raw_pmid = first_value(sorted(FIELD_ALIASES["pmid"]))
    pmid = normalize_pmid(raw_pmid) or extract_pmid(raw)
    title = first_value(sorted(FIELD_ALIASES["title"]))
    year = first_value(sorted(FIELD_ALIASES["year"]))
    author = first_value(sorted(FIELD_ALIASES["first_author"]))
    if not title and not doi and not pmid:
        title = title_guess_from_raw(raw)
    record = {
        "raw_text": raw,
        "label": first_value(sorted(FIELD_ALIASES["label"])),
        "doi": doi,
        "pmid": pmid,
        "title": title,
        "title_norm": normalize_title(title),
        "year": year_from_date(year),
        "first_author": author,
    }
    if raw_pmid and not normalize_pmid(raw_pmid):
        record["extraction_errors"] = ["Explicit PMID is not a complete integer identifier: " + raw_pmid]
    return record


def references_from_lines(lines: list[str]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for line in lines:
        raw = _clean_raw_reference(line)
        if not raw or _clean_heading(raw):
            continue
        ref = reference_from_fields({}, raw_text=line.strip())
        parsed = parse_marked_reference(raw)
        if parsed:
            ref.update(parsed)
        label = re.match(r"^\s*(\[\d+\]|\d+[\).])\s*", line)
        ref["label"] = label.group(1) if label else None
        refs.append(ref)
    return refs


def parse_marked_reference(raw: str) -> dict[str, Any] | None:
    # Parse only citations with an explicit document-type marker, not prose.
    match = re.match(r"^(?P<author>[^.\u3002]+)[.\u3002]\s*(?P<title>.+?)\s*"
                     r"\[(?:J|M|D|C)(?:/OL)?\][.\u3002]?\s*(?P<tail>.*)$", raw, re.I)
    if not match:
        return None
    author, title, tail = (match.group(k).strip() for k in ("author", "title", "tail"))
    if not contains_cjk(author + title):
        return None
    first_author = re.split(r"[,;\uff0c\uff1b\u3001]|\s+\u7b49\b", author)[0].strip()
    return {"title": title, "title_norm": normalize_title(title), "first_author": first_author,
            "year": year_from_date(tail)}


def _docx_lines(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    lines: list[str] = []
    for para in root.iter():
        if not para.tag.endswith("}p"):
            continue
        text = "".join(node.text or "" for node in para.iter() if node.tag.endswith("}t"))
        if text.strip():
            lines.append(text.strip())
    return lines


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings: list[str] = []
    for si in root.iter():
        if si.tag.endswith("}si"):
            strings.append("".join(si.itertext()))
    return strings


def _xlsx_cell_text(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(cell.itertext()).strip()
    value_node = next((child for child in cell if child.tag.endswith("}v")), None)
    if value_node is None or value_node.text is None:
        return ""
    value = value_node.text.strip()
    if cell_type == "s":
        idx = int(value) if value.isdigit() else -1
        return shared[idx].strip() if 0 <= idx < len(shared) else ""
    return value


def _xlsx_tables(path: Path):
    """Keep each worksheet's headers and physical row coordinates independent."""
    tables = []
    with zipfile.ZipFile(path) as archive:
        shared = _xlsx_shared_strings(archive)
        if "xl/workbook.xml" in archive.namelist():
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels
                       if r.attrib.get("TargetMode") != "External"}
            sheets = []
            for sheet in workbook.iter():
                if not sheet.tag.endswith("}sheet"):
                    continue
                rel = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = targets.get(rel)
                if not target:
                    raise ValueError("Worksheet relationship is missing")
                member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not member.startswith("xl/") or member not in archive.namelist():
                    raise ValueError("Invalid worksheet relationship")
                sheets.append((member, sheet.attrib.get("name"), sheet.attrib.get("state", "visible")))
        else:
            # Older minimal exports omit workbook relationships.
            members = sorted((n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                             key=lambda n: int(re.search(r"sheet(\d+)", n).group(1)))
            sheets = [(n, Path(n).stem, "visible") for n in members]
        for sheet_name, label, visibility in sheets:
            rows, locations = [], []
            root = ET.fromstring(archive.read(sheet_name))
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                values: list[str] = []
                errors = []
                for cell in row:
                    if not cell.tag.endswith("}c"):
                        continue
                    address = re.match(r"([A-Z]+)", cell.attrib.get("r", ""))
                    column = 0
                    if address:
                        for letter in address.group(1):
                            column = column * 26 + ord(letter) - ord("A") + 1
                        column -= 1
                    else:
                        column = len(values)
                    while len(values) <= column:
                        values.append("")
                    values[column] = _xlsx_cell_text(cell, shared)
                    if any(child.tag.endswith("}f") for child in cell) and not values[column]:
                        errors.append("Formula has no cached value at " + cell.attrib.get("r", "unknown cell"))
                if errors or any(value.strip() for value in values):
                    rows.append(values)
                    locations.append({"kind": "xlsx_row", "sheet": label, "sheet_path": sheet_name,
                                      "row": int(row.attrib.get("r", len(locations) + 1)), "visibility": visibility,
                                      "extraction_errors": errors})
            tables.append((rows, locations))
    return tables


def _xlsx_rows(path: Path) -> list[list[str]]:
    return [row for rows, _ in _xlsx_tables(path) for row in rows]


def _records_from_table_rows(rows: list[list[str]], locations=None) -> list[dict[str, Any]]:
    if not rows:
        return []
    header = [field_key(cell) for cell in rows[0]]
    known = set().union(*(FIELD_ALIASES[k] for k in ("doi", "pmid", "title", "year", "first_author")))
    has_header = bool(set(header) & known)
    records: list[dict[str, Any]] = []
    for index in range(1 if has_header else 0, len(rows)):
        row = rows[index]
        errors = locations[index].get("extraction_errors", []) if locations else []
        if not errors and not any(str(v).strip() for v in row):
            continue
        if has_header and [field_key(cell) for cell in row] == header:
            continue
        if has_header:
            fields = {rows[0][i]: row[i] for i in range(min(len(rows[0]), len(row)))}
            record = reference_from_fields(fields)
        else:
            record = reference_from_fields({}, raw_text=" ".join(row))
        if locations:
            record["source_location"] = locations[index]
        if errors:
            record.setdefault("extraction_errors", []).extend(errors)
        records.append(record)
    return [record for record in records if record.get("raw_text", "").strip() or record.get("extraction_errors")]


def extract_source_references(path: Path, *, encoding: str | None = None) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        text, codec = read_source_text(path, encoding)
        return [{**r, "source_encoding": codec} for r in references_from_lines(text.splitlines())]
    if suffix == ".csv":
        text, codec = read_source_text(path, encoding)
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text, newline=""), dialect)
        rows, locations = [], []
        for row in reader:
            rows.append(row)
            locations.append({"kind": "csv_row", "record": len(rows), "end_line": reader.line_num})
        return [{**r, "source_encoding": codec} for r in _records_from_table_rows(rows, locations)]
    if suffix == ".docx":
        return references_from_lines(_docx_lines(path))
    if suffix == ".xlsx":
        return [record for rows, locations in _xlsx_tables(path) for record in _records_from_table_rows(rows, locations)]
    if suffix == ".pdf":
        import pdf_reader
        pages = pdf_reader.read_pages(path)
        return [dict(ref, source_location={"kind": "pdf_page", "pdf_page_index": n})
                for n, text in pages for ref in references_from_lines(text.splitlines())]
    exit_with(f"Unsupported source format: {suffix or path.name}")


def total_results(response: Response) -> int | None:
    raw = response.headers.get("Total-Results")
    return int(raw) if raw and raw.isdigit() else None


# -------- EXTERNAL METADATA LOOKUP (CrossRef + PubMed) --------

def _search_crossref(
    title: str,
    *,
    first_author: str | None = None,
    year: str | int | None = None,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Search CrossRef by title (optionally author + year) for full metadata.

    Returns up to 5 results, each with complete author names (given + family),
    correct title, journal, volume, pages, year, and DOI.
    """
    params = {"query.title": title, "rows": 5}
    if first_author:
        params["query.author"] = first_author
    if year:
        params["filter"] = f"from-pub-date:{year}"

    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    items = data.get("message", {}).get("items", [])
    results: list[dict[str, Any]] = []
    for item in items:
        authors = []
        for author in item.get("author", []):
            given = author.get("given", "")
            family = author.get("family", "")
            if given and family:
                authors.append({"firstName": given, "lastName": family})
            elif family:
                authors.append({"firstName": "", "lastName": family})

        # Extract volume / issue / pages
        volume = item.get("volume", "")
        issue = item.get("issue", "")
        pages = ""
        page_info = item.get("page", "")
        if page_info:
            pages = page_info.replace("\u2013", "-").replace("\u2014", "-")

        # Extract year from issued date
        issued = item.get("issued", {}).get("date-parts", [[]])
        yr = str(issued[0][0]) if issued and issued[0] else ""

        results.append({
            "doi": item.get("DOI", ""),
            "title": (item.get("title") or [""])[0],
            "creators": authors,
            "year": yr,
            "journal": (item.get("container-title") or [""])[0],
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "publisher": item.get("publisher", ""),
            "type": item.get("type", ""),
            "abstract": metadata_text(item.get("abstract", "") or ""),
        })
    return results


class _MetadataTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.split(":")[-1] in {"p", "sec", "title", "br"}:
            self.parts.append(" ")


def metadata_text(value: str) -> str:
    parser = _MetadataTextParser()
    parser.feed(value)
    return " ".join(html.unescape("".join(parser.parts)).split())


def ris_value(value: Any) -> str:
    return " ".join(str(value).split())


def _doi_to_full_ris(entry: dict[str, Any]) -> str:
    """Convert a CrossRef result entry into a RIS format string."""
    entry = {k: (ris_value(v) if isinstance(v, (str, int)) else v) for k, v in entry.items()}
    lines = ["TY  - " + entry.get("ris_type", "JOUR")]
    for author in entry.get("creators", []):
        first = ris_value(author.get("firstName", ""))
        last = ris_value(author.get("lastName") or author.get("name", ""))
        lines.append(f"AU  - {last}, {first}")
    title = entry.get("title", "")
    if title:
        lines.append(f"TI  - {title}")
    journal = entry.get("journal", "")
    if journal:
        lines.append(f"JO  - {journal}")
    year = entry.get("year", "")
    if year:
        lines.append(f"PY  - {year}")
    vol = entry.get("volume", "")
    if vol:
        lines.append(f"VL  - {vol}")
    iss = entry.get("issue", "")
    if iss:
        lines.append(f"IS  - {iss}")
    pages = entry.get("pages", "")
    if pages:
        lines.append(f"SP  - {pages.split('-')[0]}")
        if "-" in pages:
            lines.append(f"EP  - {pages.split('-')[1]}")
    doi = entry.get("doi", "")
    if doi:
        lines.append(f"DO  - {doi}")
    url = entry.get("url") or (f"https://doi.org/{doi}" if doi else None)
    if url:
        lines.append(f"UR  - {url}")
    if entry.get("abstract"):
        lines.append("AB  - " + metadata_text(entry["abstract"]))
    lines.append("ER  -")
    return "\n".join(lines) + "\n"


def _crossref_item_to_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a CrossRef API message dict to standard entry format."""
    authors = []
    for author in item.get("author", []):
        given = author.get("given", "")
        family = author.get("family", "")
        if given and family:
            authors.append({"firstName": given, "lastName": family})
        elif family:
            authors.append({"firstName": "", "lastName": family})

    issued = item.get("issued", {}).get("date-parts", [[]])
    yr = str(issued[0][0]) if issued and issued[0] else ""
    pages_raw = item.get("page", "")
    pages = pages_raw.replace("-", "-") if pages_raw else ""

    return {
        "doi": item.get("DOI", ""),
        "title": (item.get("title") or [""])[0],
        "creators": authors,
        "year": yr,
        "journal": (item.get("container-title") or [""])[0],
        "volume": item.get("volume", ""),
        "issue": item.get("issue", ""),
        "pages": pages,
        "publisher": item.get("publisher", ""),
        "type": item.get("type", ""),
        "abstract": metadata_text(item.get("abstract", "") or ""),
    }


def _lookup_by_doi(doi: str, *, timeout: float = 15.0) -> dict[str, Any] | None:
    """Fetch a single work from CrossRef by DOI (most reliable match)."""
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}"
    headers = {"User-Agent": USER_AGENT}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _crossref_item_to_entry(data.get("message", {}))
    except Exception:
        return None


def cmd_crossref_lookup(args: argparse.Namespace) -> None:
    """Look up references on CrossRef to get complete metadata."""
    if args.doi:
        entry = _lookup_by_doi(args.doi)
        results = [entry] if entry else []
    else:
        results = _search_crossref(args.title, first_author=args.author, year=args.year)

    if args.ris:
        for entry in results:
            if entry.get("title"):
                print(_doi_to_full_ris(entry), end="")
    else:
        dump_json(results)


# -------- EXTERNAL METADATA LOOKUP (PubMed / NCBI Entrez) --------

NCBI_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
NCBI_UA = USER_AGENT


def _ncbi_query(**params: Any) -> str:
    """Add NCBI's requested tool/contact identification to an E-utilities query."""
    payload: dict[str, Any] = {"tool": "zotero-input", **params}
    if CONTACT_EMAIL:
        payload["email"] = CONTACT_EMAIL
    return urllib.parse.urlencode(payload)


def _pubmed_search(title: str, author: str | None = None, year: int | None = None) -> list[str]:
    """Search PubMed by title + optional author + year, return PMID list."""
    terms = [f'"{title}"[Title]']
    if author:
        terms.append(f'{author}[Author]')
    if year:
        terms.append(f'{year}[Date - Publication]')
    query = " AND ".join(terms)
    params = _ncbi_query(db="pubmed", term=query, retmode="json", retmax=5)
    try:
        req = urllib.request.Request(f"{NCBI_SEARCH}?{params}", headers={"User-Agent": NCBI_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []


def _pubmed_fetch(pmid: str) -> dict[str, Any] | None:
    """Fetch full metadata for a PMID via NCBI efetch (XML).

    Uses efetch instead of esummary: esummary abbreviates given names to initials
    (e.g. "Chen Q" — no full first name available), while efetch's <ForeName>
    carries the complete given name, so full author names land in the RIS.
    """
    params = _ncbi_query(db="pubmed", id=pmid, retmode="xml")
    try:
        req = urllib.request.Request(f"{NCBI_EFETCH}?{params}", headers={"User-Agent": NCBI_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    record = next((r for r in root.findall(".//PubmedArticle")
                   if r.findtext("MedlineCitation/PMID") == str(pmid)), None)
    if record is None:
        return None
    article = record.find("MedlineCitation/Article")
    if article is None:
        return None

    # Full author names: <ForeName> = complete given name, <Initials> as fallback
    authors = []
    for author in article.findall(".//AuthorList/Author"):
        last = author.findtext("LastName", "") or ""
        if not last:
            collective = author.findtext("CollectiveName", "") or ""
            if collective:
                authors.append({"firstName": "", "lastName": collective})
            continue
        forename = author.findtext("ForeName", "") or author.findtext("Initials", "") or ""
        authors.append({"firstName": forename, "lastName": last})

    et_title = article.find(".//ArticleTitle")
    title = ""
    if et_title is not None:
        title = "".join(et_title.itertext()).strip()
        suffix = article.findtext(".//TitleSuffix", "") or ""
        if suffix:
            title = f"{title} {suffix}"

    journal = (
        article.findtext(".//Journal/Title", "")
        or article.findtext(".//Journal/ISOAbbreviation", "")
        or ""
    )

    volume = article.findtext(".//Journal/JournalIssue/Volume", "") or ""
    issue = article.findtext(".//Journal/JournalIssue/Issue", "") or ""
    pages = (article.findtext(".//MedlinePgn", "") or "").replace("-", "-")

    pubdate = article.find(".//Journal/JournalIssue/PubDate")
    year = ""
    if pubdate is not None:
        year = pubdate.findtext("Year", "") or pubdate.findtext("MedlineDate", "") or ""
        year = year[:4]

    doi = ""
    for eloc in article.findall(".//ELocationID"):
        if eloc.get("EIdType") == "doi":
            doi = eloc.text or ""
            break

    if not doi:
        doi = record.findtext("PubmedData/ArticleIdList/ArticleId[@IdType='doi']", "")
    abstract_parts = []
    for section in article.findall("Abstract/AbstractText"):
        body = " ".join("".join(section.itertext()).split())
        label = section.get("Label", "")
        if body:
            abstract_parts.append(f"{label}: {body}" if label else body)

    return {
        "doi": doi,
        "pmid": pmid,
        "title": title,
        "creators": authors,
        "year": year,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "abstract": "\n\n".join(abstract_parts),
    }


def _pubmed_to_ris(entry: dict[str, Any]) -> str:
    """Convert a PubMed entry dict to RIS format string."""
    entry = {k: (ris_value(v) if isinstance(v, (str, int)) else v) for k, v in entry.items()}
    lines = ["TY  - " + entry.get("ris_type", "JOUR")]
    for a in entry.get("creators", []):
        lines.append(f"AU  - {ris_value(a.get('lastName') or a.get('name', ''))}, {ris_value(a.get('firstName', ''))}")
    t = entry.get("title", "")
    if t: lines.append(f"TI  - {t}")
    j = entry.get("journal", "")
    if j: lines.append(f"JO  - {j}")
    y = entry.get("year", "")
    if y: lines.append(f"PY  - {y}")
    v = entry.get("volume", "")
    if v: lines.append(f"VL  - {v}")
    i = entry.get("issue", "")
    if i: lines.append(f"IS  - {i}")
    p = entry.get("pages", "")
    if p:
        parts = p.split("-")
        lines.append(f"SP  - {parts[0]}")
        if len(parts) > 1: lines.append(f"EP  - {parts[1]}")
    pmid = entry.get("pmid", "")
    if pmid:
        lines.append(f"UR  - https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        lines.append(f"M2  - PMID: {pmid}")
    doi = entry.get("doi", "")
    if doi:
        lines.append(f"DO  - {doi}")
        if not pmid:
            lines.append(f"UR  - https://doi.org/{doi}")
    if entry.get("abstract"):
        lines.append("AB  - " + " ".join(entry["abstract"].split()))
    lines.append("ER  -")
    return "\n".join(lines) + "\n"


def cmd_pubmed_lookup(args: argparse.Namespace) -> None:
    """Look up references on PubMed via NCBI Entrez API."""
    entries = []
    if args.pmid:
        entry = _pubmed_fetch(args.pmid)
        if entry:
            entries = [entry]
    else:
        pmids = _pubmed_search(args.title, author=args.author, year=args.year)
        for pmid in pmids[:1]:  # Take best match
            entry = _pubmed_fetch(pmid)
            if entry:
                entries.append(entry)

    if args.ris:
        for entry in entries:
            print(_pubmed_to_ris(entry), end="")
    else:
        dump_json(entries)


def _paginated_get(endpoint: str, params: dict[str, Any]) -> list[Any]:
    """GET a paginated Zotero endpoint, return all items across pages."""
    params = dict(params)
    params.setdefault("limit", API_PAGE_LIMIT)
    start = 0
    all_items: list[Any] = []
    while True:
        params["start"] = start
        query_str = query(params)
        resp = api_response(f"{LOCAL_USER}/{endpoint}?{query_str}")
        page = parse_body(resp)
        if isinstance(page, list):
            all_items.extend(page)
        else:
            all_items.append(page)
        total = total_results(resp)
        start += API_PAGE_LIMIT
        if total is not None and start >= total:
            break
        if total is None and (not isinstance(page, list) or len(page) < API_PAGE_LIMIT):
            break
    return all_items


def collection_items(collection_key: str, *, include_children: bool = False) -> list[dict[str, Any]]:
    endpoint = "items" if include_children else "items/top"
    quoted = urllib.parse.quote(collection_key)
    return _paginated_get(f"collections/{quoted}/{endpoint}", {"sort": "title", "direction": "asc"})


def item_identifier_values(item: dict[str, Any]) -> dict[str, str | None]:
    data = item.get("data", item)
    title = data.get("title")
    return {
        "doi": normalize_doi(data.get("DOI") or data.get("doi")),
        "pmid": pmid_from_item(data),
        "title": normalize_title(title),
    }


def ref_identifier_values(ref: dict[str, Any]) -> dict[str, str | None]:
    return {
        "doi": normalize_doi(ref.get("doi")),
        "pmid": normalize_pmid(ref.get("pmid")),
        "title": normalize_title(ref.get("title")) or normalize_title(ref.get("raw_text")),
    }


def identifier_keys(values: dict[str, str | None]) -> list[str]:
    keys: list[str] = []
    for kind in ["doi", "pmid", "title"]:
        value = values.get(kind)
        if value:
            keys.append(f"{kind}:{value}")
    return keys


def build_item_index(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for key in identifier_keys(item_identifier_values(item)):
            index.setdefault(key, []).append(item)
    return index


def build_reference_index(refs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for ref in refs:
        for key in identifier_keys(ref_identifier_values(ref)):
            index.setdefault(key, []).append(ref)
    return index


def match_by_priority(
    values: dict[str, str | None],
    index: dict[str, list[dict[str, Any]]],
) -> tuple[str | None, list[dict[str, Any]]]:
    for kind in ["doi", "pmid", "title"]:
        value = values.get(kind)
        if not value:
            continue
        key = f"{kind}:{value}"
        matches = [
            item for item in index.get(key, [])
            if all(
                not values.get(identifier) or not item_identifier_values(item).get(identifier)
                or values[identifier] == item_identifier_values(item)[identifier]
                for identifier in ("doi", "pmid")
            )
        ]
        if matches:
            return key, matches
    return None, []


def resolve_reference_pmid_doi(ref: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if ref.get("doi") or not ref.get("pmid"):
        return ref, False
    entry = _pubmed_fetch(str(ref["pmid"]))
    if not entry or not entry.get("doi"):
        return ref, False
    enriched = dict(ref)
    enriched["doi"] = normalize_doi(entry.get("doi"))
    enriched["pubmed_title"] = entry.get("title")
    enriched["pubmed_year"] = entry.get("year")
    return enriched, True


# -------- CONCURRENT HELPERS --------

def _fetch_bibtex_chunk(start: int, endpoint: str) -> str:
    """Fetch one page of BibTeX export (used by ThreadPoolExecutor)."""
    params = query({
        "format": "bibtex",
        "sort": "title",
        "direction": "asc",
        "limit": API_PAGE_LIMIT,
        "start": start,
    })
    resp = api_response(f"{LOCAL_USER}/{endpoint}?{params}")
    return resp.text.strip()


def export_bibtex(item_key: str | None = None, *, include_children: bool = False) -> str:
    if item_key:
        params = query({"itemKey": item_key, "format": "bibtex", "limit": API_PAGE_LIMIT})
        return api_response(f"{LOCAL_USER}/items?{params}").text

    endpoint = "items" if include_children else "items/top"

    # First page to get total count
    params = query({
        "format": "bibtex",
        "sort": "title",
        "direction": "asc",
        "limit": API_PAGE_LIMIT,
        "start": 0,
    })
    first_resp = api_response(f"{LOCAL_USER}/{endpoint}?{params}")
    total = total_results(first_resp)
    chunks: list[str] = [first_resp.text.strip()] if first_resp.text.strip() else []

    if total is not None and total > API_PAGE_LIMIT:
        remaining_starts = list(range(API_PAGE_LIMIT, total, API_PAGE_LIMIT))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(_fetch_bibtex_chunk, start, endpoint): start
                for start in remaining_starts
            }
            for f in as_completed(futures):
                chunk = f.result()
                if chunk:
                    chunks.append(chunk)

    text = "\n\n".join(chunks)
    return text + "\n" if text else ""


def write_text_output(text: str, out: str | None) -> None:
    if out is None:
        print(text, end="" if text.endswith("\n") else "\n")
        return

    path = Path(out).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    dump_json(
        {
            "path": str(path),
            "bytes": len(text.encode("utf-8")),
            "bibtex_entries": count_bibtex_entries(text),
        }
    )


def append_bib_entry(bib_path: Path, entry: str) -> tuple[str, bool]:
    keys = extract_bibtex_keys(entry)
    if not keys:
        exit_with("Could not extract a BibTeX key from Zotero export")
    key = keys[0]

    existing = bib_path.read_text(encoding="utf-8", errors="replace") if bib_path.exists() else ""
    already_present = re.search(r"@\w+\s*\{\s*" + re.escape(key) + r"\s*,", existing) is not None
    if already_present:
        return key, False

    bib_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = existing.rstrip("\n") + "\n\n" if existing else ""
    bib_path.write_text(prefix + entry.strip() + "\n", encoding="utf-8")
    return key, True


def insert_citation(target: Path, citation: str, marker: str | None) -> None:
    text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    if marker:
        if marker not in text:
            exit_with(f"Marker not found in {target}: {marker!r}")
        target.write_text(text.replace(marker, citation, 1), encoding="utf-8")
        return

    suffix = "" if not text or text.endswith("\n") else "\n"
    target.write_text(text + suffix + citation + "\n", encoding="utf-8")


def find_item(*, item_key: str | None, query_text: str | None) -> dict[str, Any]:
    if item_key:
        return api_get(f"{LOCAL_USER}/items/{urllib.parse.quote(item_key)}")
    if not query_text:
        exit_with("Provide --item-key or --query")

    params = query({"q": query_text})
    matches = api_get(f"{LOCAL_USER}/items/top?{params}")
    if not matches:
        exit_with(f"No top-level Zotero items matched query: {query_text}")
    if len(matches) > 1:
        exit_with(f"Ambiguous query ({len(matches)} candidates); select --item-key explicitly: "
                  + ", ".join(str(i.get("key")) for i in matches))
    return matches[0]


def status_payload() -> dict[str, Any]:
    root = request("/api/", timeout=DEFAULT_TIMEOUT)
    connector = request("/connector/ping", timeout=DEFAULT_TIMEOUT)
    profile = profile_dir()
    prefs = prefs_file()
    return {
        "profile": str(profile) if profile else None,
        "prefs_file": str(prefs) if prefs else None,
        "local_api_enabled_pref": read_local_api_pref(),
        "api_running": root.ok,
        "api_status": root.status,
        "api_error": root.error,
        "zotero_version": root.headers.get("X-Zotero-Version")
        or connector.headers.get("X-Zotero-Version"),
        "api_version": root.headers.get("Zotero-API-Version"),
        "schema_version": root.headers.get("Zotero-Schema-Version"),
        "connector_running": connector.ok,
        "connector_status": connector.status,
        "connector_error": connector.error,
        "base_url": DEFAULT_BASE_URL,
    }


def cmd_status(args: argparse.Namespace) -> None:
    payload = status_payload()
    if args.json:
        dump_json(payload)
        return
    print(f"Zotero local API pref: {payload['local_api_enabled_pref']} ({payload['prefs_file']})")
    print(
        "API running: "
        f"{payload['api_running']} status={payload['api_status']} "
        f"version={payload['api_version']} zotero={payload['zotero_version']}"
    )
    print(f"Connector running: {payload['connector_running']} status={payload['connector_status']}")


def cmd_set_pref(args: argparse.Namespace, enabled: bool) -> None:
    if not args.profile:
        exit_with("Specify --profile for preference edits; the API URL does not identify the profile.")
    backup = set_local_api_pref(enabled, args.profile)
    restarted = restart_zotero(wait_for_api=enabled, profile=args.profile, pid=args.pid) if args.restart else False
    dump_json(
        {
            "enabled": enabled,
            "backup": str(backup),
            "restarted": restarted,
            "status": status_payload(),
        }
    )


def cmd_restart(args: argparse.Namespace) -> None:
    dump_json({"restarted": restart_zotero(wait_for_api=True, profile=args.profile, pid=args.pid)})


ENDPOINTS = [
    ("root", "/api/"),
    ("schema", "/api/schema"),
    ("itemTypes", "/api/itemTypes"),
    ("itemFields", "/api/itemFields"),
    ("creatorFields", "/api/creatorFields"),
    ("collections", f"{LOCAL_USER}/collections"),
    ("topCollections", f"{LOCAL_USER}/collections/top"),
    ("topItems", f"{LOCAL_USER}/items/top"),
    ("tags", f"{LOCAL_USER}/tags"),
    ("searches", f"{LOCAL_USER}/searches"),
    ("groups", f"{LOCAL_USER}/groups"),
    ("fulltextVersions", f"{LOCAL_USER}/fulltext?since=0"),
    ("connectorPing", "/connector/ping"),
]


def cmd_probe(args: argparse.Namespace) -> None:
    rows: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(request, path): label for label, path in ENDPOINTS}
        results = []
        for f in as_completed(futures):
            label = futures[f]
            response = f.result()
            parsed = parse_body(response)
            if isinstance(parsed, list):
                summary: Any = {"type": "array", "len": len(parsed)}
            elif isinstance(parsed, dict):
                summary = {"type": "object", "keys": list(parsed)[:8]}
            else:
                summary = str(parsed)[:160]
            results.append({
                "label": label,
                "path": dict(ENDPOINTS)[label],
                "status": response.status,
                "content_type": response.content_type,
                "total": response.headers.get("Total-Results"),
                "summary": summary,
            })

    # Sort results by label for deterministic output
    rows = sorted(results, key=lambda r: r["label"])

    if args.json:
        dump_json(rows)
        return
    for row in rows:
        print(
            f"{row['status'] or 'ERR':>3} {row['label']:18} {row['path']:45} total={row['total']} {row['summary']}"
        )


def cmd_inventory(args: argparse.Namespace) -> None:
    endpoint = "items" if args.include_children else "items/top"
    items = _paginated_get(endpoint, {"sort": "title", "direction": "asc"})
    rows = [summarize_item(item) for item in items]
    dump_json(rows) if args.json else print_items(rows)


def cmd_extract_source(args: argparse.Namespace) -> None:
    refs = extract_source_references(Path(args.source).expanduser().resolve())
    dump_json(refs) if args.json else dump_json({"references": refs, "count": len(refs)})


def cmd_collection_items(args: argparse.Namespace) -> None:
    items = collection_items(args.collection_key, include_children=args.include_children)
    rows = [summarize_item(item) for item in items]
    dump_json(rows) if args.json else print_items(rows)


def collection_import_plan(args: argparse.Namespace, *, include_prune: bool) -> dict[str, Any]:
    refs = extract_source_references(Path(args.source).expanduser().resolve())
    target_items = collection_items(args.collection_key)
    target_index = build_item_index(target_items)
    resolved_pmid_doi = 0
    plan: dict[str, Any] = {
        "source": str(Path(args.source).expanduser().resolve()),
        "collection": args.collection_key,
        "counts": {
            "source_references": len(refs),
            "target_collection_items": len(target_items),
        },
        "already_in_target": [],
        "import_new": [],
        "ambiguous": [],
    }
    if include_prune:
        plan["prune_candidates"] = []

    target_needs_pmid_resolution = any(
        (identifiers := item_identifier_values(item))["doi"] and not identifiers["pmid"]
        for item in target_items
    )
    for index, ref in enumerate(refs):
        values = ref_identifier_values(ref)
        match_key, matches = match_by_priority(values, target_index)
        if not matches and target_needs_pmid_resolution and not args.no_resolve_pmid_doi:
            ref, resolved = resolve_reference_pmid_doi(ref)
            resolved_pmid_doi += int(resolved)
            refs[index] = ref
            values = ref_identifier_values(ref)
            match_key, matches = match_by_priority(values, target_index)
        if len(matches) > 1:
            plan["ambiguous"].append({"source": ref, "scope": "target", "match_key": match_key})
            continue
        if matches:
            plan["already_in_target"].append(
                {"source": ref, "match_key": match_key, "item": summarize_item(matches[0])}
            )
            continue

        plan["import_new"].append({"source": ref, "lookup_priority": identifier_keys(values)})

    if include_prune:
        source_index = build_reference_index(refs)
        for item in target_items:
            values = item_identifier_values(item)
            match_key, matches = match_by_priority(values, source_index)
            if not matches:
                plan["prune_candidates"].append({"item": summarize_item(item), "checked_keys": identifier_keys(values)})
            elif len(matches) > 1:
                plan["ambiguous"].append(
                    {"item": summarize_item(item), "scope": "source", "match_key": match_key}
                )

    plan["counts"]["resolved_pmid_doi"] = resolved_pmid_doi
    plan["counts"].update({
        "already_in_target": len(plan["already_in_target"]),
        "import_new": len(plan["import_new"]),
        "ambiguous": len(plan["ambiguous"]),
    })
    if include_prune:
        plan["counts"]["prune_candidates"] = len(plan["prune_candidates"])
    return plan


def print_collection_plan(plan: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        dump_json(plan)
        return
    for key, value in plan["counts"].items():
        print(f"{key}: {value}")


def cmd_import_plan(args: argparse.Namespace) -> None:
    print_collection_plan(collection_import_plan(args, include_prune=False), as_json=args.json)


def cmd_sync_plan(args: argparse.Namespace) -> None:
    print_collection_plan(collection_import_plan(args, include_prune=True), as_json=args.json)


def cmd_collections(args: argparse.Namespace) -> None:
    rows = [summarize_collection(collection) for collection in api_get(f"{LOCAL_USER}/collections")]
    if args.json:
        dump_json(rows)
        return
    for row in rows:
        parent = f" parent={row['parentCollection']}" if row.get("parentCollection") else ""
        print(f"{row.get('key') or '':10} {row.get('name') or ''}{parent}")


def cmd_tags(args: argparse.Namespace) -> None:
    rows = [summarize_tag(tag) for tag in api_get(f"{LOCAL_USER}/tags")]
    if args.json:
        dump_json(rows)
        return
    for row in rows:
        print(f"{row.get('tag') or ''} ({row.get('numItems') or 0})")


def cmd_groups(args: argparse.Namespace) -> None:
    rows = [summarize_group(group) for group in api_get(f"{LOCAL_USER}/groups")]
    if args.json:
        dump_json(rows)
        return
    for row in rows:
        print(f"{row.get('id') or '':>10} {row.get('type') or '':12} {row.get('name') or ''}")


def cmd_search(args: argparse.Namespace) -> None:
    items = _paginated_get("items/top", {"q": args.query})
    rows = [summarize_item(item) for item in items]
    if args.with_bibtex_keys:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(export_bibtex, row.get("key")): i for i, row in enumerate(rows)}
            for f in as_completed(futures):
                idx = futures[f]
                bibtex = f.result()
                keys = extract_bibtex_keys(bibtex)
                rows[idx]["bibtexKey"] = keys[0] if keys else None
    dump_json(rows) if args.json else print_items(rows)


def cmd_export_bibtex(args: argparse.Namespace) -> None:
    write_text_output(
        export_bibtex(args.item_key, include_children=args.include_children), args.out
    )


def cmd_sync_bib(args: argparse.Namespace) -> None:
    text = export_bibtex(include_children=args.include_children)
    path = Path(args.out).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    dump_json({"path": str(path), "entries": count_bibtex_entries(text)})


def cmd_citations(args: argparse.Namespace) -> None:
    items = _paginated_get("items/top", {"include": "data,citation", "style": args.style})
    rows: list[dict[str, Any]] = []
    for item in items:
        row = summarize_item(item)
        row["citation"] = item.get("citation")
        rows.append(row)
    if args.json:
        dump_json(rows)
        return
    for row in rows:
        print(f"{row.get('key')} {row.get('citation')}")


def cmd_children(args: argparse.Namespace) -> None:
    data = api_get(f"{LOCAL_USER}/items/{urllib.parse.quote(args.item_key)}/children")
    rows = [summarize_item(item) for item in data]
    dump_json(rows) if args.json else print_items(rows)


def cmd_fulltext(args: argparse.Namespace) -> None:
    data = api_get(f"{LOCAL_USER}/items/{urllib.parse.quote(args.attachment_key)}/fulltext")
    content = data.get("content", "") if isinstance(data, dict) else str(data)
    if args.out is None:
        print(content)
        return
    path = Path(args.out).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    dump_json(
        {
            "path": str(path),
            "chars": len(content),
            "indexedPages": data.get("indexedPages") if isinstance(data, dict) else None,
            "totalPages": data.get("totalPages") if isinstance(data, dict) else None,
        }
    )


def cmd_file_url(args: argparse.Namespace) -> None:
    print(api_get(f"{LOCAL_USER}/items/{urllib.parse.quote(args.attachment_key)}/file/view/url"))


def cmd_cite(args: argparse.Namespace) -> None:
    item = find_item(item_key=args.item_key, query_text=args.query)
    item_key = item.get("key")
    if not item_key:
        exit_with("Matched Zotero item has no key")

    citekey, added = append_bib_entry(
        Path(args.bib).expanduser().resolve(), export_bibtex(item_key)
    )
    citation = f"\\cite{{{citekey}}}" if args.tex else f"[@{citekey}]"
    target = Path(args.tex or args.markdown).expanduser().resolve()
    insert_citation(target, citation, args.marker)
    dump_json(
        {
            "item_key": item_key,
            "title": summarize_item(item).get("title"),
            "bibtex_key": citekey,
            "bib_path": str(Path(args.bib).expanduser().resolve()),
            "bib_entry_added": added,
            "edited_file": str(target),
            "inserted": citation,
        }
    )


def connector_post(path: str, payload: Any, *, timeout: float = 60.0, content_type: str = "application/json") -> Response:
    return request(path, method="POST", data=payload, timeout=timeout, headers={"Content-Type": content_type})


def cmd_selected_target(args: argparse.Namespace) -> None:
    response = require_ok(
        connector_post("/connector/getSelectedCollection", {}),
        "POST /connector/getSelectedCollection",
    )
    payload = parse_body(response)
    print(json.dumps(payload, indent=2) if args.json else payload)


def clean_ris_for_import(text: str) -> str:
    """Keep metadata, drop child-item fields, and repair legacy PMID abstracts."""
    fields: list[tuple[str, str]] = []
    for line in text.lstrip("\ufeff").splitlines():
        match = re.match(r"^([A-Z0-9]{2})  - ?(.*)$", line)
        if match:
            fields.append((match[1], match[2]))
        elif line.strip():
            if not fields or fields[-1][0] == "ER":
                raise ValueError("Invalid RIS: unexpected text outside a field")
            tag, value = fields[-1]
            fields[-1] = (tag, value + "\n" + line)
    output = []
    in_record = False
    records = 0
    # Unknown RIS fields can become automatic "unmapped fields" child notes.
    metadata_tags = {
        "TY", "ER", "AU", "A1", "TI", "T1", "JO", "JF", "T2", "J2",
        "PY", "Y1", "DA", "VL", "IS", "SP", "EP", "DO", "UR", "AB",
        "N2", "M2", "KW", "LA", "SN", "ST", "DB", "DP", "AN", "CN",
    }
    for tag, value in fields:
        if tag == "TY":
            if in_record:
                raise ValueError("Invalid RIS: missing ER before TY")
            in_record = True
        elif not in_record:
            raise ValueError("Invalid RIS: field outside a record")
        if tag == "ER":
            in_record = False
            records += 1
        if tag in {"N1", "RN", "L1", "L2", "L3", "L4", "LK"}:
            continue
        if tag in {"AB", "N2"} and re.fullmatch(r"\s*PMID:\s*\d+\s*", value, re.I):
            tag = "M2"
        if tag not in metadata_tags:
            value = f"RIS {tag}: {value}"
            tag = "M2"
        output.append(f"{tag}  - {value}".rstrip())
    if in_record or not records:
        raise ValueError("Invalid RIS: no complete records")
    return "\n".join(output) + "\n"


def cmd_import_records(args: argparse.Namespace, kind: str) -> None:
    if not args.yes:
        exit_with(
            f"Refusing to write to Zotero without --yes. "
            f"This imports {kind} into the selected Zotero library/collection."
        )
    text = Path(args.file).expanduser().read_text(encoding="utf-8") if args.file else args.text
    if not text:
        exit_with("Provide --file or --text")
    if kind == "RIS":
        text = clean_ris_for_import(text)

    session = args.session or f"codex-{uuid.uuid4().hex}"
    path = f"/connector/import?{query({'session': session})}"
    response = require_ok(connector_post(path, text, content_type="text/plain"), f"POST {path}")
    dump_json({"status": response.status, "session": session, "response": parse_body(response)})


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of a compact text summary"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate Zotero Desktop local API and connector server."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status", help="Show Zotero local API and connector readiness")
    add_json_flag(status)
    status.set_defaults(func=cmd_status)

    enable = subcommands.add_parser("enable", help="Enable Zotero's local Desktop API preference")
    enable.add_argument(
        "--restart", action="store_true", help="Restart Zotero after editing prefs.js"
    )
    enable.set_defaults(func=lambda args: cmd_set_pref(args, True))
    enable.add_argument("--profile", help="Explicit profile directory for configuration only")
    enable.add_argument("--pid", type=int, help="Verified instance PID for optional restart")

    disable = subcommands.add_parser(
        "disable", help="Disable Zotero's local Desktop API preference"
    )
    disable.add_argument(
        "--restart", action="store_true", help="Restart Zotero after editing prefs.js"
    )
    disable.set_defaults(func=lambda args: cmd_set_pref(args, False))
    disable.add_argument("--profile")
    disable.add_argument("--pid", type=int)

    restart = subcommands.add_parser("restart", help="Restart Zotero and wait for the local API")
    restart.set_defaults(func=cmd_restart)
    restart.add_argument("--profile")
    restart.add_argument("--pid", type=int)

    probe = subcommands.add_parser("probe", help="Probe common safe local API routes")
    add_json_flag(probe)
    probe.set_defaults(func=cmd_probe)

    inventory = subcommands.add_parser("inventory", help="List Zotero items")
    inventory.add_argument(
        "--include-children", action="store_true", help="Include child notes and attachments"
    )
    inventory.add_argument(
        "--all", action="store_true", dest="include_children", help=argparse.SUPPRESS
    )
    add_json_flag(inventory)
    inventory.set_defaults(func=cmd_inventory)

    extract_source = subcommands.add_parser(
        "extract-source", help="Extract lightweight reference keys from a source document"
    )
    extract_source.add_argument("--source", required=True)
    add_json_flag(extract_source)
    extract_source.set_defaults(func=cmd_extract_source)

    collection_items_cmd = subcommands.add_parser(
        "collection-items", help="List top-level items in one Zotero collection"
    )
    collection_items_cmd.add_argument("collection_key")
    collection_items_cmd.add_argument(
        "--include-children", action="store_true", help="Include child notes and attachments"
    )
    add_json_flag(collection_items_cmd)
    collection_items_cmd.set_defaults(func=cmd_collection_items)

    for name, handler, help_text in [
        ("import-plan", cmd_import_plan, "Dry-run an import against one target collection"),
        ("sync-plan", cmd_sync_plan, "Dry-run a source-document to collection sync"),
    ]:
        plan_cmd = subcommands.add_parser(name, help=help_text)
        plan_cmd.add_argument("--source", required=True)
        plan_cmd.add_argument("--collection", required=True, dest="collection_key")
        plan_cmd.add_argument(
            "--no-resolve-pmid-doi",
            action="store_true",
            help="Do not resolve unmatched source PMIDs to DOI via PubMed",
        )
        add_json_flag(plan_cmd)
        plan_cmd.set_defaults(func=handler)

    collections = subcommands.add_parser("collections", help="List collections")
    add_json_flag(collections)
    collections.set_defaults(func=cmd_collections)

    tags = subcommands.add_parser("tags", help="List tags")
    add_json_flag(tags)
    tags.set_defaults(func=cmd_tags)

    groups = subcommands.add_parser("groups", help="List synced group libraries visible locally")
    add_json_flag(groups)
    groups.set_defaults(func=cmd_groups)

    search = subcommands.add_parser("search", help="Search top-level Zotero items")
    search.add_argument("query")
    search.add_argument("--with-bibtex-keys", action="store_true")
    add_json_flag(search)
    search.set_defaults(func=cmd_search)

    export = subcommands.add_parser("export-bibtex", help="Export Zotero items as BibTeX")
    export.add_argument("--item-key")
    export.add_argument("--include-children", action="store_true")
    export.add_argument(
        "--all", action="store_true", dest="include_children", help=argparse.SUPPRESS
    )
    export.add_argument("--out")
    export.set_defaults(func=cmd_export_bibtex)

    sync_bib = subcommands.add_parser("sync-bib", help="Write a references.bib export")
    sync_bib.add_argument("--out", default="references.bib")
    sync_bib.add_argument("--include-children", action="store_true")
    sync_bib.add_argument(
        "--all", action="store_true", dest="include_children", help=argparse.SUPPRESS
    )
    sync_bib.set_defaults(func=cmd_sync_bib)

    citations = subcommands.add_parser("citations", help="Render formatted citations")
    citations.add_argument("--style", default="apa")
    add_json_flag(citations)
    citations.set_defaults(func=cmd_citations)

    children = subcommands.add_parser("children", help="List child notes/attachments for an item")
    children.add_argument("item_key")
    add_json_flag(children)
    children.set_defaults(func=cmd_children)

    fulltext = subcommands.add_parser(
        "fulltext", help="Print or save indexed full text for an attachment"
    )
    fulltext.add_argument("attachment_key")
    fulltext.add_argument("--out")
    fulltext.set_defaults(func=cmd_fulltext)

    file_url = subcommands.add_parser(
        "file-url", help="Print Zotero's local file URL for an attachment"
    )
    file_url.add_argument("attachment_key")
    file_url.set_defaults(func=cmd_file_url)

    cite = subcommands.add_parser(
        "cite", help="Insert a citation into a TeX or Markdown file and update a .bib file"
    )
    source = cite.add_mutually_exclusive_group(required=True)
    source.add_argument("--item-key")
    source.add_argument("--query")
    target = cite.add_mutually_exclusive_group(required=True)
    target.add_argument("--tex")
    target.add_argument("--markdown")
    cite.add_argument("--bib", default="references.bib")
    cite.add_argument(
        "--marker", help="Replace this marker with the citation; otherwise append the citation"
    )
    cite.set_defaults(func=cmd_cite)

    selected = subcommands.add_parser(
        "selected-target", help="Show the currently selected Zotero library/collection"
    )
    add_json_flag(selected)
    selected.set_defaults(func=cmd_selected_target)

    crossref = subcommands.add_parser(
        "crossref-lookup",
        help="Look up a reference on CrossRef API to get complete metadata (full author names, DOI, etc.)",
    )
    # Either --doi (exact) or title + optional author/year (fuzzy)
    crossref.add_argument("--doi", help="DOI to look up (most reliable)")
    crossref.add_argument("title", nargs="?", help="Work title to search (used when --doi is not given)")
    crossref.add_argument("--author", help="First author surname to narrow title search")
    crossref.add_argument("--year", help="Publication year to narrow title search")
    crossref.add_argument(
        "--ris",
        action="store_true",
        help="Output as RIS format (suitable for piping into import-ris)",
    )
    crossref.set_defaults(func=cmd_crossref_lookup)

    pubmed = subcommands.add_parser(
        "pubmed-lookup",
        help="Look up a reference on PubMed via NCBI Entrez (get PMID, full author names, DOI)",
    )
    pubmed.add_argument("--pmid", help="PubMed ID to look up (most reliable)")
    pubmed.add_argument("title", nargs="?", help="Work title to search (used when --pmid is not given)")
    pubmed.add_argument("--author", help="Author surname to narrow title search")
    pubmed.add_argument("--year", type=int, help="Publication year to narrow title search")
    pubmed.add_argument(
        "--ris",
        action="store_true",
        help="Output as RIS format (suitable for piping into import-ris)",
    )
    pubmed.set_defaults(func=cmd_pubmed_lookup)

    for command, kind in [("import-bibtex", "BibTeX"), ("import-ris", "RIS")]:
        import_cmd = subcommands.add_parser(command, help=f"Import {kind} through Zotero Connector")
        input_group = import_cmd.add_mutually_exclusive_group(required=True)
        input_group.add_argument("--file")
        input_group.add_argument("--text")
        import_cmd.add_argument("--session")
        import_cmd.add_argument("--yes", action="store_true", help="Confirm Zotero library write")
        import_cmd.set_defaults(func=lambda args, kind=kind: cmd_import_records(args, kind))

    import ingest
    ingest.add_commands(subcommands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
