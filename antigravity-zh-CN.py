"""Apply or restore the Simplified Chinese resource patch for Antigravity 2.34.0."""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path


PATCH_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_INSTALL = Path(os.environ.get("LOCALAPPDATA", ".")) / "Programs/antigravity"
TARGETS = (
    ("resources/app.asar", "01-desktop.zh-CN.json", "8934adef0622c9271c92eea950a71848764a66f7b10023ccbd82e54b959adb0e"),
    ("resources/bin/language_server.exe", "02-interface.zh-CN.json", "cc94d648c39a67c293d0fdc8befc51e953d1a8f55617bf49fff69885ba475db9"),
)
WEB_START = 0xAD169C4
WEB_END = 0xB2E693B
BACKUP_DIRECTORY = "zh-CN-backup-2.34.0"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def replace_text(source, edits):
    chunks = []
    cursor = 0
    for offset, old, new in edits:
        expected = old.encode("utf-8")
        end = offset + len(expected)
        if not cursor <= offset <= end <= len(source) or source[offset:end] != expected:
            raise ValueError(f"Resource text did not match at byte {offset}")
        chunks.extend((source[cursor:offset], new.encode("utf-8")))
        cursor = end
    chunks.append(source[cursor:])
    return b"".join(chunks)


def asar_entries(tree, prefix=""):
    for name, item in tree.items():
        path = prefix + name
        if "files" in item:
            yield from asar_entries(item["files"], path + "/")
        elif "offset" in item:
            yield path, item


def asar_contents(source):
    first, header_size, _, json_size = struct.unpack_from("<IIII", source)
    if first != 4:
        raise ValueError("Unrecognized ASAR header")
    return json.loads(source[16:16 + json_size]), 8 + header_size


def patch_asar(source, edits):
    index, data_start = asar_contents(source)
    entries = sorted(asar_entries(index["files"]), key=lambda item: int(item[1]["offset"]))
    patched = set()
    body = io.BytesIO()
    old_cursor = 0
    for path, item in entries:
        offset = int(item["offset"])
        if offset != old_cursor:
            raise ValueError(f"Unexpected ASAR layout at {path}")
        old_cursor += item["size"]
        contents = source[data_start + offset:data_start + old_cursor]
        integrity = item.get("integrity")
        if integrity and sha(contents) != integrity["hash"]:
            raise ValueError(f"Invalid original ASAR entry: {path}")
        if path in edits:
            contents = replace_text(contents, edits[path])
            patched.add(path)
            if integrity:
                block = integrity["blockSize"]
                integrity["hash"] = sha(contents)
                integrity["blocks"] = [
                    sha(contents[i:i + block]) for i in range(0, len(contents), block)
                ] or [sha(b"")]
        item["size"] = len(contents)
        item["offset"] = str(body.tell())
        body.write(contents)
    if patched != edits.keys() or data_start + old_cursor != len(source):
        raise ValueError("ASAR entry set or size did not match")
    header = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padding = b"\0" * (-len(header) % 4)
    padded_size = len(header) + len(padding)
    return struct.pack("<IIII", 4, padded_size + 8, padded_size + 4, len(header)) + header + padding + body.getvalue()


def patch_web(source, edits):
    original = source[WEB_START:WEB_END]
    stream = io.BytesIO()
    directory = []
    with zipfile.ZipFile(io.BytesIO(original)) as old:
        if set(edits) - set(old.namelist()):
            raise ValueError("Embedded ZIP entry set did not match")
        for entry in old.infolist():
            offset = stream.tell()
            size = entry.file_size
            checksum = entry.CRC
            data = zip_deflate_payload(original, entry)
            if entry.filename in edits:
                contents = replace_text(old.read(entry), edits[entry.filename])
                size = len(contents)
                checksum = zlib.crc32(contents)
                data = compress(contents, strong=entry.filename == "main.js")
            name = entry.filename.encode("utf-8")
            year, month, day, hour, minute, second = entry.date_time
            date = (year - 1980) << 9 | month << 5 | day
            time = hour << 11 | minute << 5 | second // 2
            fields = (20, 0x800, entry.compress_type, time, date, checksum, len(data), size, len(name), 0)
            stream.write(struct.pack("<4s5H3I2H", b"PK\x03\x04", *fields))
            stream.write(name)
            stream.write(data)
            creator = (entry.create_system << 8) | entry.create_version
            directory.append(
                struct.pack("<4s6H3I5H2I", b"PK\x01\x02", creator, *fields, 0, 0, entry.internal_attr, entry.external_attr, offset)
                + name
            )

    central = b"".join(directory)
    padding = len(original) - stream.tell() - len(central) - 22
    if padding < 0:
        raise ValueError(f"Embedded ZIP cannot be replaced at its original size (padding={padding})")
    comment = min(padding, 65535)
    stream.write(b"\0" * (padding - comment))
    central_offset = stream.tell()
    stream.write(central)
    count = len(directory)
    stream.write(struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, count, count, len(central), central_offset, comment))
    stream.write(b"\0" * comment)
    packed = stream.getvalue()
    if len(packed) != len(original):
        raise ValueError("Embedded ZIP size changed")
    with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(io.BytesIO(packed)) as new:
        if old.namelist() != new.namelist() or new.testzip() is not None:
            raise ValueError("Embedded ZIP validation failed")
        for name in old.namelist():
            original_entry = old.read(name)
            expected = replace_text(original_entry, edits[name]) if name in edits else original_entry
            if new.read(name) != expected:
                raise ValueError(f"Unexpected change in {name}")
    return b"".join((source[:WEB_START], packed, source[WEB_END:]))


def zip_deflate_payload(archive, entry):
    offset = entry.header_offset
    name_size, extra_size = struct.unpack_from("<HH", archive, offset + 26)
    start = offset + 30 + name_size + extra_size
    return archive[start:start + entry.compress_size]


def compress(contents, strong=False):
    if strong:
        found = [
            shutil.which("7z.exe"),
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe",
            Path("C:/msys32/usr/lib/p7zip/7za.exe"),
        ]
        tool = next((Path(path) for path in found if path and Path(path).is_file()), None)
        if tool:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "main.js").write_bytes(contents)
                subprocess.run(
                    [str(tool), "a", "-tzip", "-mm=Deflate", "-mx=9", "-mfb=128", "-mpass=10", "ui.zip", "main.js"],
                    cwd=root, check=True, stdout=subprocess.DEVNULL,
                )
                result = (root / "ui.zip").read_bytes()
                with zipfile.ZipFile(io.BytesIO(result)) as archive:
                    entry = archive.getinfo("main.js")
                    if entry.compress_type != zipfile.ZIP_DEFLATED:
                        raise ValueError("Unexpected 7-Zip compression method")
                    return zip_deflate_payload(result, entry)
    compressor = zlib.compressobj(9, wbits=-15, memLevel=7)
    return compressor.compress(contents) + compressor.flush()


def load_patch(path):
    patch = json.loads((PATCH_DIRECTORY / path).read_text(encoding="utf-8"))
    if patch["version"] != "2.34.0":
        raise ValueError("Patch version did not match")
    return patch["edits"]


def build(source, index):
    patcher = (patch_asar, patch_web)[index]
    return patcher(source, load_patch(TARGETS[index][1]))


def verify_or_backup(install, backup, index):
    relative, _, original_hash = TARGETS[index]
    installed = install / relative
    saved = backup / relative
    if not saved.exists():
        if file_hash(installed) != original_hash:
            raise ValueError(f"Expected an unmodified 2.34.0 file: {installed}")
        saved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=saved.parent, delete=False) as temporary:
            staged = Path(temporary.name)
            with installed.open("rb") as stream:
                shutil.copyfileobj(stream, temporary)
        try:
            if file_hash(staged) != original_hash:
                raise ValueError(f"Backup hash did not match: {installed}")
            os.replace(staged, saved)
        finally:
            staged.unlink(missing_ok=True)
    if file_hash(saved) != original_hash:
        raise ValueError(f"Original backup hash did not match: {saved}")
    return installed, saved, original_hash


def running():
    result = subprocess.run(["tasklist.exe", "/fo", "csv", "/nh"], check=True, stdout=subprocess.PIPE)
    return any(
        row and row[0].casefold() in {"antigravity.exe", "language_server.exe"}
        for row in csv.reader(result.stdout.decode(errors="replace").splitlines())
    )


def staged_bytes(target, data):
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        path = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def update(install, mode):
    if mode != "check" and running():
        raise ValueError("Close Antigravity before applying or restoring its files.")
    backup = install / BACKUP_DIRECTORY
    changes = []
    completed = []
    try:
        for index, (relative, _, original_hash) in enumerate(TARGETS):
            target = install / relative
            if mode == "check":
                if file_hash(target) != original_hash:
                    raise ValueError(f"Expected an unmodified 2.34.0 file: {target}")
                result = build(target.read_bytes(), index)
                print(f"Verified {relative}: {sha(result)}")
                continue
            if mode == "restore" and not (backup / relative).exists():
                raise ValueError(f"Original backup is missing: {backup / relative}")
            target, saved, original_hash = verify_or_backup(install, backup, index)
            original = saved.read_bytes()
            patched = build(original, index)
            patched_hash = sha(patched)
            current_hash = file_hash(target)
            if current_hash not in {original_hash, patched_hash}:
                raise ValueError(f"Installed file has changed since 2.34.0: {target}")
            next_data = original if mode == "restore" else patched
            next_hash = original_hash if mode == "restore" else patched_hash
            if current_hash != next_hash:
                next_file = staged_bytes(target, next_data)
                try:
                    previous_file = staged_bytes(target, target.read_bytes())
                except BaseException:
                    next_file.unlink(missing_ok=True)
                    raise
                changes.append((target, next_file, previous_file, current_hash, next_hash))
            print(f"Verified {relative}: {next_hash}")
        if mode != "check" and running():
            raise ValueError("Antigravity opened during preparation. Close it and rerun.")
        for target, next_file, previous_file, current_hash, next_hash in changes:
            if file_hash(target) != current_hash:
                raise ValueError(f"Installed file changed during preparation: {target}")
            os.replace(next_file, target)
            completed.append((target, previous_file))
            if file_hash(target) != next_hash:
                raise ValueError(f"Installed file hash did not match: {target}")
    except BaseException:
        for target, previous_file in reversed(completed):
            try:
                os.replace(previous_file, target)
            except OSError as error:
                print(f"Automatic rollback failed for {target}: {error}; recovery file {previous_file}", file=sys.stderr)
        raise
    finally:
        completed_files = {previous_file for _, previous_file in completed}
        for _, next_file, previous_file, _, _ in changes:
            next_file.unlink(missing_ok=True)
            if previous_file not in completed_files or sys.exc_info()[0] is None:
                previous_file.unlink(missing_ok=True)
    if mode != "check":
        print(f"Original files: {backup}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply", "restore"))
    parser.add_argument("--install", type=Path, default=DEFAULT_INSTALL)
    args = parser.parse_args()
    try:
        update(args.install.resolve(), args.mode)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
