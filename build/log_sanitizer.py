"""Redact known path prefixes from raw bytes, then decode without data loss."""
import re


def public_log_issues(text):
    """Detect both absolute home paths and incompletely masked path tails."""
    patterns = {
        'absolute_user_path': r'[A-Za-z]:[\\/]+Users[\\/]+',
        'unresolved_user_tail': r'\$(?:USER|HOME)[\\/]+\S',
        'workspace_tail': r'(?:Documents|Desktop)[\\/]+[^\r\n\"\']*[\\/]\d{2}\.\d{1,2}\.\d{1,2}[\\/]',
        'replacement_character': '\ufffd',
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]


def redact_bytes(raw, prefixes):
    """Prefixes are (path, token) pairs, never inferred from corrupted text."""
    rules = {}
    for path, token in prefixes:
        value = str(path)
        variants = {value, value.replace('\\', '/'), value.replace('/', '\\')}
        variants.update({item.replace('\\', '\\\\') for item in list(variants)})
        for variant in variants:
            for codec in ('utf-8', 'gb18030'):
                try:
                    encoded = variant.encode(codec)
                except UnicodeEncodeError:
                    continue
                rules.setdefault((encoded, token), set()).add(codec)
    counts, encodings = {}, set()
    boundary = rb'(?=[\\/\s\"\'\]\):]|$)'
    for (prefix, token), codecs in sorted(rules.items(), key=lambda item: len(item[0][0]), reverse=True):
        pattern = re.compile(re.escape(prefix) + boundary, re.I)
        raw, count = pattern.subn(lambda match: token.encode('ascii'), raw)
        if count:
            counts[token] = counts.get(token, 0) + count
            encodings.update(codecs)
    return raw, counts, sorted(encodings)


def decode_strict(raw):
    if raw.isascii():
        return raw.decode('ascii'), 'ascii'
    for codec in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(codec, errors='strict')
        except UnicodeDecodeError:
            continue
        if '\ufffd' in text:
            raise ValueError('Input contains replacement characters; obtain the original byte output')
        return text, codec
    raise ValueError('Unknown log encoding; recover the original output instead of replacing bytes')


def sanitize_log(raw, prefixes):
    redacted, counts, encodings = redact_bytes(raw, prefixes)
    text, codec = decode_strict(redacted)
    issues = public_log_issues(text)
    if issues:
        raise ValueError('Log has unresolved path/encoding issues: ' + ', '.join(issues))
    return text.encode('utf8'), dict(
        path_replacements=counts, path_encodings_matched=encodings,
        decode_after_path_redaction=codec, lossy_decode_used=False)
