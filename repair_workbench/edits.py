"""Exact, version-bound edits against an immutable baseline."""
import hashlib
from pathlib import Path

from .candidate import validate_source_path
from .snapshot import verify_snapshot


class EditRejected(ValueError):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


def apply_edits(baseline: Path, edits: list[dict], allowed_paths: set[str]) -> dict[str, str]:
    manifest = verify_snapshot(baseline)
    hashes = {item['path']: item['sha256'] for item in manifest['files']}
    if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise EditRejected('invalid_edit_count', maximum=32)
    grouped = {}
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'path', 'sha256', 'start_line', 'old_text', 'new_text'}:
            raise EditRejected('invalid_edit_fields')
        path = edit['path']
        if not isinstance(path, str):
            raise EditRejected('invalid_path')
        try:
            validate_source_path(path, allowed_paths)
        except ValueError:
            raise EditRejected('path_not_allowed') from None
        if path not in hashes:
            raise EditRejected('path_not_in_baseline')
        if edit['sha256'] != hashes[path]:
            raise EditRejected('file_version_mismatch', path=path, expected_sha256=hashes[path])
        old, new, line = edit['old_text'], edit['new_text'], edit['start_line']
        if (not isinstance(old, str) or not old or not isinstance(new, str)
                or len(old.encode()) > 65536 or len(new.encode()) > 65536
                or type(line) is not int or line < 1):
            raise EditRejected('invalid_edit_content', path=path)
        if path not in grouped:
            raw = (baseline / 'workspace' / path).read_bytes()
            if len(raw) > 1024 * 1024:
                raise EditRejected('source_too_large', path=path)
            grouped[path] = (raw.decode('utf-8'), [])
        original, spans = grouped[path]
        start = original.find(old)
        if start < 0:
            raise EditRejected('text_not_found', path=path)
        # Look for overlapping matches as well, not just str.count().
        if original.find(old, start + 1) >= 0:
            raise EditRejected('text_not_unique', path=path)
        actual_line = original.count('\n', 0, start) + 1
        if actual_line != line:
            raise EditRejected('line_mismatch', path=path, actual_start_line=actual_line)
        end = start + len(old)
        if any(start < other_end and other_start < end for other_start, other_end, _ in spans):
            raise EditRejected('overlapping_edits', path=path)
        spans.append((start, end, new))
    changes = {}
    for path, (original, spans) in grouped.items():
        updated = original
        for start, end, new in sorted(spans, reverse=True):
            updated = updated[:start] + new + updated[end:]
        if len(updated.encode()) > 1024 * 1024:
            raise EditRejected('candidate_too_large', path=path)
        if updated != original:
            changes[path] = updated
    if not changes:
        raise EditRejected('empty_patch')
    return changes


def syntax_errors(workspace: Path, paths: list[str]) -> list[dict]:
    """Compile without executing or importing candidate code; no pyc is emitted."""
    errors = []
    for path in paths:
        try:
            compile((workspace / path).read_bytes(), path, 'exec', dont_inherit=True)
        except SyntaxError as exc:
            errors.append({'path': path, 'error_type': type(exc).__name__,
                           'line': exc.lineno, 'column': exc.offset,
                           'message': exc.msg[:300]})
        except (ValueError, RecursionError, OverflowError) as exc:
            errors.append({'path': path, 'error_type': type(exc).__name__,
                           'message': 'Python compiler rejected candidate'})
    return errors


def content_id(manifest):
    import json
    return hashlib.sha256(json.dumps(sorted(
        (f['path'], f['sha256']) for f in manifest['files'])).encode()).hexdigest()
