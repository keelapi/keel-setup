#!/usr/bin/env python3
"""Credential-free, read-only voice preparation from an immutable public bundle."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import pathlib
import stat
from pathlib import Path
import re
import subprocess
import sys


def _verify_pinned_release(bundle_sha: str, *, bundle: pathlib.Path | None = None) -> None:
    """Authenticate the complete pinned snapshot without executing bundle code.

    Each standalone entrypoint carries this gate because a sibling import cannot
    establish its own trust. The entrypoint and caller-supplied pin are trusted.
    An explicit bundle is used only by the orchestrator; credential commands
    always derive their bundle from the trusted entrypoint's installation path.
    """
    try:
        if re.fullmatch(r"[0-9a-f]{40}", bundle_sha) is None:
            raise ValueError("invalid bundle SHA")
        bundle = (pathlib.Path(__file__).resolve().parents[2] if bundle is None else bundle.resolve())

        def git(*args: str) -> bytes:
            """Read local Git data without inherited configuration or hooks."""
            result = subprocess.run(
                ["git", "--no-replace-objects", "-c", "core.fsmonitor=false",
                 "-C", str(bundle), *args],
                env={
                    "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
                },
                capture_output=True, timeout=20, check=True,
            )
            return result.stdout

        worktree = pathlib.Path(git("rev-parse", "--show-toplevel").decode().removesuffix("\n")).resolve()
        if bundle != worktree:
            raise ValueError("bundle must be the Git worktree root")
        if git("rev-parse", "HEAD").decode().strip() != bundle_sha:
            raise ValueError("bundle revision mismatch")
        if git("remote", "get-url", "origin").decode().strip() not in {
            "https://github.com/keelapi/keel-setup",
            "https://github.com/keelapi/keel-setup.git",
            "git@github.com:keelapi/keel-setup.git",
            "ssh://git@github.com/keelapi/keel-setup.git",
        }:
            raise ValueError("unofficial bundle origin")
        if git("diff", "--cached", "--name-only", "--no-ext-diff", "--no-textconv", bundle_sha):
            raise ValueError("bundle index is not clean")

        def object_bytes(kind: str, oid: str) -> bytes:
            """Authenticate raw Git objects, including the commit and trees."""
            if re.fullmatch(r"[0-9a-f]{40}", oid) is None:
                raise ValueError("invalid object ID")
            size = int(git("cat-file", "-s", oid))
            if not 0 <= size <= 8 * 1024 * 1024:
                raise ValueError("bundle object exceeds verification limit")
            data = git("cat-file", kind, oid)
            header = f"{kind} {len(data)}\0".encode("ascii")
            if len(data) != size or hashlib.sha1(header + data).hexdigest() != oid:
                raise ValueError("Git object differs from its authenticated ID")
            return data

        commit = object_bytes("commit", bundle_sha)
        tree_line = commit.split(b"\n", 1)[0]
        if re.fullmatch(rb"tree [0-9a-f]{40}", tree_line) is None:
            raise ValueError("invalid pinned commit tree")
        files: dict[pathlib.PurePosixPath, bytes] = {}
        total_bytes = 0

        def tree_files(oid: str, parent: pathlib.PurePosixPath) -> None:
            """Walk only entries from independently authenticated tree objects."""
            nonlocal total_bytes
            if len(parent.parts) > 32:
                raise ValueError("bundle tree exceeds depth limit")
            data = object_bytes("tree", oid)
            offset = 0
            names: set[str] = set()
            while offset < len(data):
                end = data.index(b"\0", offset)
                mode, raw_name = data[offset:end].split(b" ", 1)
                name = raw_name.decode("utf-8")
                if (name in {"", ".", ".."} or name.lower() == ".git"
                        or "/" in name or name in names):
                    raise ValueError("unsafe or duplicate bundle entry")
                names.add(name)
                raw_oid = data[end + 1:end + 21]
                if len(raw_oid) != 20:
                    raise ValueError("truncated bundle tree")
                offset = end + 21
                relative = parent / name
                if mode == b"40000":
                    tree_files(raw_oid.hex(), relative)
                elif mode in {b"100644", b"100755"}:
                    if len(files) >= 4096:
                        raise ValueError("bundle exceeds file limit")
                    content = object_bytes("blob", raw_oid.hex())
                    total_bytes += len(content)
                    if total_bytes > 64 * 1024 * 1024:
                        raise ValueError("bundle exceeds byte limit")
                    files[relative] = content
                else:
                    raise ValueError("unsupported bundle entry")

        tree_files(tree_line[5:].decode("ascii"), pathlib.PurePosixPath())
        if not files:
            raise ValueError("empty bundle tree")

        # Include ignored additions and reject symlinked directories. Git metadata
        # alone is exempt, including the .git file used by linked worktrees.
        actual: set[pathlib.PurePosixPath] = set()
        def walk_error(error: OSError) -> None:
            """Fail closed when any part of the checkout cannot be enumerated."""
            raise error

        for current, directories, filenames in os.walk(bundle, followlinks=False, onerror=walk_error):
            directory = pathlib.Path(current)
            if directory == bundle:
                directories[:] = [name for name in directories if name != ".git"]
                filenames = [name for name in filenames if name != ".git"]
            for name in directories:
                if (directory / name).is_symlink():
                    raise ValueError("symlink in bundle directory")
            actual.update(pathlib.PurePosixPath((directory / name).relative_to(bundle).as_posix())
                          for name in filenames)
        if actual != set(files):
            raise ValueError("checkout files differ from the pinned allowlist")

        # Descriptor-relative traversal refuses parent symlinks. Nonblocking open
        # and fstat reject FIFOs/devices before reading, with bounded file reads.
        for relative, expected in files.items():
            directory_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in relative.parts[:-1]:
                    child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = child_fd
                fd = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory_fd)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("bundle entry is not a regular file")
                    if stream.read(len(expected) + 1) != expected:
                        raise ValueError("bundle file differs from pinned bytes")
            finally:
                os.close(directory_fd)
    except Exception as exc:
        raise RuntimeError("pinned release verification failed") from exc


def prepare(bundle: Path, bundle_sha: str, profile_id: str) -> dict:
    if not re.fullmatch(r'[a-f0-9]{40}', bundle_sha):
        raise ValueError('An immutable setup release SHA is required')
    try:
        _verify_pinned_release(bundle_sha, bundle=bundle)
    except RuntimeError as exc:
        raise ValueError('Pinned voice bundle authentication failed') from exc
    subprocess.run([sys.executable, '-I', str(bundle/'scripts/check_release_bundle.py')],
                   check=True, capture_output=True, timeout=20)
    raw = (bundle/'keel-setup/reference/integration_profiles.v1.json').read_bytes()
    contract = json.loads(raw)
    if contract.get('version') != 'keel.integration_profiles.v1' or contract.get('qualification_status') != 'unqualified':
        raise ValueError('Unsupported qualification contract')
    profile = next((row for row in contract['profiles'] if row['id'] == profile_id), None)
    if profile is None or profile['readiness'] != 'preparation_only':
        raise ValueError('Requested voice transport is not supported for preparation')
    return {'version':'keel.voice_preparation.v1','stage':'preparation_only','evidence':'source-inspected',
        'bundle_sha':bundle_sha,'contract_digest':hashlib.sha256(raw).hexdigest(), 'profile':profile,
        'unresolved_gates':contract['voice_gates'], 'activation':'human_owned',
        'next_step':'Follow the pinned voice-gateway reference; designate sandbox credentials outside conversation.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--bundle-sha',required=True)
    parser.add_argument('--profile',required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.bundle.resolve(),args.bundle_sha,args.profile)
    except (ValueError,OSError,subprocess.SubprocessError):
        print(json.dumps({'stage':'blocked','reason':'Pinned voice preparation contract could not be verified.'}))
        return 1
    print(json.dumps(result,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
