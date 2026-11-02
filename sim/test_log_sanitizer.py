"""Regressions for locale-encoded tool output and partially masked paths."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'build'))
from log_sanitizer import public_log_issues, redact_bytes, sanitize_log


class LogTests(unittest.TestCase):
    def setUp(self):
        self.root = 'C:' + '/Users/sample/Documents/工程/源码/scan3d-zynq'
        self.output = 'C:' + '/Users/sample/Documents/工程/测试'
        self.prefixes = [(self.root, '$REPO'), (self.output, '$OUTPUT_TOUCH')]

    def test_utf8_source_path_and_warning_preserved(self):
        raw = ('WARNING: [VRFC 10-3645] unconnected [' + self.root + '/rtl/top.sv:12]\nPASS\n').encode('utf8')
        text, record = sanitize_log(raw, self.prefixes)
        self.assertEqual(text, b'WARNING: [VRFC 10-3645] unconnected [$REPO/rtl/top.sv:12]\nPASS\n')
        self.assertEqual(record['path_replacements'], {'$REPO': 1})

    def test_gbk_source_path_preserves_ascii_results(self):
        raw = ('File "' + self.root + '/sim/test.sv"\nPASS mismatches=0\n').encode('gbk')
        text, record = sanitize_log(raw, self.prefixes)
        self.assertEqual(text, b'File "$REPO/sim/test.sv"\nPASS mismatches=0\n')
        self.assertIn('gb18030', record['path_encodings_matched'])

    def test_mixed_path_encodings_in_one_output(self):
        raw = (self.root + '/one.sv\n').encode('utf8') + (self.output + '/run.log\n').encode('gbk')
        text, record = sanitize_log(raw, self.prefixes)
        self.assertEqual(text, b'$REPO/one.sv\n$OUTPUT_TOUCH/run.log\n')
        self.assertEqual(sum(record['path_replacements'].values()), 2)

    def test_backslashes_and_json_escaped_paths(self):
        raw = (self.root.replace('/', '\\').replace('\\', '\\\\') + '\\\\sim\\\\a.sv').encode('gbk')
        text, record = sanitize_log(raw, self.prefixes)
        self.assertEqual(text, b'$REPO\\\\sim\\\\a.sv')

    def test_utf8_message_is_not_decoded_as_gbk(self):
        raw = '校验完成，PASS\n'.encode('utf8')
        text, record = sanitize_log(raw, self.prefixes)
        self.assertEqual(text, raw)
        self.assertEqual(record['decode_after_path_redaction'], 'utf-8-sig')

    def test_gbk_message_retains_its_text(self):
        text, _ = sanitize_log('校验完成，PASS\n'.encode('gbk'), self.prefixes)
        self.assertEqual(text.decode('utf8'), '校验完成，PASS\n')

    def test_previous_lossy_output_is_rejected(self):
        with self.assertRaises(ValueError):
            sanitize_log(('lost ' + chr(0xfffd) + '\n').encode('utf8'), self.prefixes)

    def test_masked_home_tail_is_rejected_without_drive_letter(self):
        date_folder = '.'.join(['26', '9', '28'])
        raw = ('$USER' + '/Documents/work/' + date_folder + '/src/file.sv').encode('ascii')
        self.assertIn('unresolved_user_tail', public_log_issues(raw.decode()))
        with self.assertRaises(ValueError):
            sanitize_log(raw, self.prefixes)

    def test_unknown_encoding_is_rejected(self):
        with self.assertRaises(ValueError):
            sanitize_log(b'bad \xff\n', self.prefixes)

    def test_distinct_directory_prefix_is_not_silently_shortened(self):
        raw = (self.root + '_other/top.sv').encode('utf8')
        unchanged, counts, _ = redact_bytes(raw, self.prefixes)
        self.assertEqual(unchanged, raw)
        self.assertEqual(counts, {})
        with self.assertRaises(ValueError):
            sanitize_log(raw, self.prefixes)


if __name__ == '__main__':
    unittest.main(verbosity=2)
