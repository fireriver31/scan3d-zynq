"""Check delivered privacy copies and their original-to-copy hash bindings."""
from pathlib import Path
import hashlib
import io
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PATH = re.compile(
    rb'[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+'
    rb'|[A-Za-z]:[\\/]+(?:vivado|jupyter_notebooks)[\\/]+'
    rb'|\$(?:USER|HOME)[\\/]+\S'
    rb'|(?:Documents|Desktop)[\\/]+[^\r\n\"\']*[\\/]\d{2,4}\.\d{1,2}\.\d{1,2}[\\/]', re.I)
ACCOUNT = re.compile(rb'\btb\d{7,}\b', re.I)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def records(root=ROOT):
    value = json.loads((root / 'data/privacy/redaction_manifest.json').read_text(encoding='utf8'))
    rows = value['records']
    assert len({r['path'] for r in rows}) == len(rows)
    return {r['path']: r for r in rows}


def verify_original(path, expected, root=ROOT):
    """Expected is the historic original SHA, not a claim that the copy is pristine."""
    path = Path(path).resolve()
    name = path.relative_to(root.resolve()).as_posix()
    actual = digest(path.read_bytes())
    if actual == expected:
        return 'unchanged'
    row = records(root).get(name)
    assert row and row['original_sha256'] == expected, name
    assert row['delivered_sha256'] == actual, name
    return 'privacy_copy'


def audit(root=ROOT):
    root = Path(root)
    findings = []
    scanned = 0
    members = 0

    def inspect(raw, name, depth=0):
        nonlocal scanned, members
        scanned += 1
        if LOCAL_PATH.search(raw) or ACCOUNT.search(raw):
            findings.append(name)
        if raw.startswith(b'PK\x03\x04') and depth < 3:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    members += 1
                    inspect(archive.read(member), name + '!' + member.filename, depth + 1)

    for path in root.rglob('*'):
        if path.is_file() and '.git' not in path.relative_to(root).parts:
            inspect(path.read_bytes(), path.relative_to(root).as_posix())
    assert not findings, findings
    rows = records(root)
    for name, row in rows.items():
        path = root / name
        assert row['delivered_sha256'] == digest(path.read_bytes()), name
        assert row['original_sha256'] != row['delivered_sha256'], name
        assert row['path_replacements'] == len(row['changes']) > 0, name
        assert row['delivered_ufffd_count'] == 0, name
        assert b'\xef\xbf\xbd' not in path.read_bytes(), name
    auth_dir = root / 'third_party/authorization'
    auth = json.loads((auth_dir / '20261101_acz702_permission_v1.json').read_text(encoding='utf8'))
    assert auth['screenshot_redacted'] and not auth['screenshot_contains_requester_account']
    assert not auth['screenshot_unmodified'] and not auth['original_screenshot_in_delivery']
    assert auth['screenshot_sha256'] == digest((auth_dir / auth['screenshot_file']).read_bytes())
    assert auth['transcript_sha256'] == digest((auth_dir / auth['transcript_file']).read_bytes())
    assert all(digest(p.read_bytes()) != auth['original_screenshot_sha256']
               for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts)
    return dict(passed=True, local_paths_or_requester_accounts=[], payload_files_and_members=scanned,
                archive_members=members, path_redacted_files=len(rows), screenshot_account_redacted=True,
                original_evidence_distributed=False)
