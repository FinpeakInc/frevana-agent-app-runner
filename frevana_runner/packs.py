import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from filelock import FileLock

from .auth import RunnerError, request_json

MAX_ZIP = 100 * 1024**2
MAX_EXPANDED = 512 * 1024**2
WINDOWS_DEVICE = re.compile(
    r"(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])", re.IGNORECASE
)


def share_code(url, environment):
    parts = urllib.parse.urlsplit(url)
    domains = {
        "production": {"www.frevana.com", "frevana.com"},
        "development": {"dev-www.frevana.com"},
    }
    found = re.fullmatch(
        r"(?:/[a-z]{2}(?:-[A-Z]{2})?)?/agentapp/s/([A-Za-z0-9_-]{1,128})(?:/preview)?/?",
        parts.path,
    )
    if parts.scheme != "https" or parts.netloc not in domains[environment] or not found:
        raise RunnerError(
            "Expected a Frevana Agent App share link for this environment."
        )
    return found[1]


def share_environment(url, explicit=None):
    environment = (
        "development"
        if urllib.parse.urlsplit(url).hostname == "dev-www.frevana.com"
        else "production"
    )
    share_code(url, environment)
    if explicit is not None and explicit != environment:
        raise RunnerError(
            f"This share uses {environment}; --environment {explicit} does not match."
        )
    return environment


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, delete=False, mode="w", encoding="utf8"
    ) as tmp:
        json.dump(value, tmp, ensure_ascii=False)
    try:
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def unpack(source, dest):
    seen = set()
    total = 0
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if not 0 < len(entries) <= 10000:
            raise RunnerError("Invalid number of archive entries.")
        for item in entries:
            name = item.filename.rstrip("/")
            path = PurePosixPath(name)
            kind = stat.S_IFMT(item.external_attr >> 16)
            if (
                not name
                or path.is_absolute()
                or any(
                    s in (".", "..", "")
                    or ":" in s
                    or s.endswith((" ", "."))
                    or WINDOWS_DEVICE.fullmatch(s.split(".", 1)[0].rstrip(" "))
                    for s in name.split("/")
                )
                or "\\" in name
                or "\x00" in item.orig_filename
                or item.flag_bits & 1
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
            ):
                raise RunnerError("Invalid archive entry.")
            normalized = unicodedata.normalize("NFC", name).casefold()
            if normalized in seen:
                raise RunnerError("Duplicate archive entry.")
            seen.add(normalized)
            total += item.file_size
            if total > MAX_EXPANDED:
                raise RunnerError("Expanded package exceeds the size limit.")
            target = dest.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out, 65536)
                if item.external_attr >> 16 & 0o111:
                    target.chmod(0o700)


def download(desc, path):
    url = urllib.parse.urlsplit(desc["download_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise RunnerError("Invalid signed package URL.")
    digest = hashlib.sha256()
    size = 0
    # The signed object URL carries its own authority. No Frevana credential is sent.
    with (
        urllib.request.urlopen(desc["download_url"], timeout=120) as res,
        path.open("wb") as out,
    ):
        while block := res.read(65536):
            size += len(block)
            if size > MAX_ZIP:
                raise RunnerError("Package exceeds the download size limit.")
            digest.update(block)
            out.write(block)
    if not size or digest.hexdigest() != desc["sha256"].lower():
        raise RunnerError("Package SHA-256 mismatch.")


def app_jobs(root):
    manifest = json.loads((root / "skillpack.json").read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise RunnerError("Invalid skillpack.json.")
    job_file = root / "job.json"
    data = (
        json.loads(job_file.read_text(encoding="utf-8-sig"))
        if job_file.exists()
        else {}
    )
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list) or any(
        not isinstance(job, dict) or not isinstance(job.get("id"), str) for job in jobs
    ):
        raise RunnerError("Invalid job.json.")
    return [
        {"id": j["id"], "name": j.get("name", j["id"])}
        for j in jobs
        if j.get("enabled", True)
    ]


def prepare(creds, code):
    grant = creds.token(login=True)["grant_id"]
    home = creds.root / "grants" / key(grant)

    def descriptor():
        return request_json(
            creds.business_api + "/agent-app-shares/" + code + "/download",
            method="POST",
            headers={"x-policy-version": "2"},
            bearer=creds.token(expected_grant=grant)["access_token"],
        )

    ref = home / "shares" / (code + ".json")
    with FileLock(
        str(creds.root / ("prepare-" + key(grant + code) + ".lock")), timeout=360
    ):
        ref.unlink(missing_ok=True)
        for attempt in range(2):
            desc = descriptor()
            app = home / "apps" / key(desc["agent_app_id"])
            snapshots = app / "snapshots"
            snapshots.mkdir(parents=True, exist_ok=True)
            dest = snapshots / key(desc["snapshot_id"] + ":" + desc["sha256"])
            with FileLock(str(dest) + ".lock", timeout=360):
                if not dest.exists():
                    with tempfile.TemporaryDirectory(dir=snapshots) as tmp:
                        archive = Path(tmp) / "download.zip"
                        try:
                            download(desc, archive)
                        except urllib.error.HTTPError as exc:
                            if attempt == 0 and exc.code in (401, 403):
                                continue
                            raise RunnerError(
                                f"Package download failed (HTTP {exc.code})."
                            ) from None
                        folder = Path(tmp) / "pack"
                        folder.mkdir()
                        unpack(archive, folder)
                        if not (folder / "skillpack.json").exists():
                            entries = list(folder.iterdir())
                            if len(entries) == 1 and entries[0].is_dir():
                                folder = entries[0]
                        app_jobs(folder)
                        folder.rename(dest)
            info = {
                "pack_root": str(dest),
                "data_root": str(app),
                "agent_app_id": desc["agent_app_id"],
                "snapshot_id": desc["snapshot_id"],
                "share_id": desc["share_id"],
                "jobs": app_jobs(dest),
            }
            write_json(ref, info)
            return info
    raise RunnerError("Package URL expired.")


def prepared(creds, code, grant):
    path = creds.root / "grants" / key(grant) / "shares" / (code + ".json")
    if not path.exists():
        raise RunnerError("Prepare this share for the current authorization first.")
    return json.loads(path.read_text(encoding="utf8"))
