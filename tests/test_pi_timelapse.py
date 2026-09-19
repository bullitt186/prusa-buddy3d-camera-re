"""GAP-TIMELAPSE-01: Pi storage-backed timelapse helpers."""
import ast
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
MAIN_PY = PI_DIR / 'main.py'
sys.path.insert(0, str(PI_DIR))

import timelapse  # noqa: E402
from state import CameraState  # noqa: E402


def _jpeg(width, height):
    """Minimal JPEG carrying an SOF0 marker so dimensions can be parsed."""
    sof = (
        b'\xff\xc0' + struct.pack('>H', 17) + b'\x08'
        + struct.pack('>HH', height, width)
        + b'\x03' + b'\x01\x11\x00' * 3
    )
    return b'\xff\xd8' + sof + b'\xff\xd9'


def _read_avi(path):
    """Parse back the RIFF/AVI structure the writer produced (no external tools)."""
    with open(path, 'rb') as f:
        data = f.read()
    assert data[0:4] == b'RIFF'
    assert struct.unpack('<I', data[4:8])[0] == len(data) - 8
    assert data[8:12] == b'AVI '

    pos = 12
    assert data[pos:pos + 4] == b'LIST'
    hdrl_size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
    assert data[pos + 8:pos + 12] == b'hdrl'
    hdrl_start = pos + 12

    assert data[hdrl_start:hdrl_start + 4] == b'avih'
    avih_size = struct.unpack('<I', data[hdrl_start + 4:hdrl_start + 8])[0]
    assert avih_size == 56
    avih = struct.unpack('<IIIIIIIIII4I', data[hdrl_start + 8:hdrl_start + 64])
    assert avih[3] & 0x10            # AVIF_HASINDEX
    assert avih[6] == 1              # one stream

    strl_pos = hdrl_start + 8 + avih_size
    assert data[strl_pos:strl_pos + 4] == b'LIST'
    strl_size = struct.unpack('<I', data[strl_pos + 4:strl_pos + 8])[0]
    assert data[strl_pos + 8:strl_pos + 12] == b'strl'
    strl_start = strl_pos + 12

    assert data[strl_start:strl_start + 4] == b'strh'
    strh_size = struct.unpack('<I', data[strl_start + 4:strl_start + 8])[0]
    assert strh_size == 56
    strh = struct.unpack(
        '<4s4sIHHIIIIIIIIhhhh', data[strl_start + 8:strl_start + 64]
    )
    assert strh[0] == b'vids'
    assert strh[1] == b'MJPG'

    strf_pos = strl_start + 8 + strh_size
    assert data[strf_pos:strf_pos + 4] == b'strf'
    strf = struct.unpack('<IiiHH4sIiiII', data[strf_pos + 8:strf_pos + 48])
    assert strf[0] == 40
    assert strf[4] == 24            # biBitCount
    assert strf[5] == b'MJPG'       # biCompression

    movi_pos = pos + 8 + hdrl_size
    assert data[movi_pos:movi_pos + 4] == b'LIST'
    movi_size = struct.unpack('<I', data[movi_pos + 4:movi_pos + 8])[0]
    assert data[movi_pos + 8:movi_pos + 12] == b'movi'
    movi_fourcc = movi_pos + 8
    movi_start = movi_pos + 12
    movi_end = movi_pos + 8 + movi_size

    frame_count = 0
    offsets = []
    sizes = []
    p = movi_start
    while p + 8 <= movi_end:
        assert data[p:p + 4] == b'00dc'
        size = struct.unpack('<I', data[p + 4:p + 8])[0]
        offsets.append(p - movi_fourcc)
        sizes.append(size)
        frame_count += 1
        p += 8 + size + (size & 1)
    assert p == movi_end

    assert data[movi_end:movi_end + 4] == b'idx1'
    idx_size = struct.unpack('<I', data[movi_end + 4:movi_end + 8])[0]
    assert idx_size == frame_count * 16
    idx_entries = idx_size // 16
    for i in range(frame_count):
        base = movi_end + 8 + i * 16
        assert data[base:base + 4] == b'00dc'
        assert struct.unpack('<I', data[base + 4:base + 8])[0] & 0x10  # keyframe
        assert struct.unpack('<I', data[base + 8:base + 12])[0] == offsets[i]
        assert struct.unpack('<I', data[base + 12:base + 16])[0] == sizes[i]

    return {
        'fcc_type': strh[0].decode(),
        'fcc_handler': strh[1].decode(),
        'width': avih[8],
        'height': avih[9],
        'frame_count': avih[4],
        'movi_chunks': frame_count,
        'idx_entries': idx_entries,
        'fps': strh[7],
        'scale': strh[6],
    }


class TimelapseValidationTests(unittest.TestCase):
    def test_valid_interval(self):
        self.assertEqual(timelapse.valid_interval('30'), 30)
        self.assertIsNone(timelapse.valid_interval(0))
        self.assertIsNone(timelapse.valid_interval(3601))
        self.assertIsNone(timelapse.valid_interval('x'))

    def test_valid_fps(self):
        self.assertEqual(timelapse.valid_fps(12), 12)
        self.assertIsNone(timelapse.valid_fps(0))
        self.assertIsNone(timelapse.valid_fps(31))

    def test_apply_enable(self):
        state = CameraState()
        self.assertFalse(state.timelapse_enabled)
        self.assertTrue(timelapse.apply_enable('timelapse_enable', state))
        self.assertTrue(state.timelapse_enabled)
        self.assertTrue(timelapse.apply_enable('timelapse_disable', state))
        self.assertFalse(state.timelapse_enabled)
        self.assertFalse(timelapse.apply_enable('unknown', state))


class TimelapseStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_frame_name_timestamp_format(self):
        now = 1700000000.123456
        tm = time.localtime(1700000000)
        expected = 'timelapse_%02d-%02d-%02d-123.jpg' % (
            tm.tm_hour, tm.tm_min, tm.tm_sec,
        )
        self.assertEqual(timelapse.frame_name(now), expected)
        self.assertRegex(
            timelapse.frame_name(now), r'^timelapse_\d{2}-\d{2}-\d{2}-\d{3}\.jpg$'
        )

    def test_save_frame_writes_timestamped_name(self):
        path = timelapse.save_frame(b'one', self.dir, now=1700000000.123456)
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertEqual(
            os.path.basename(path), timelapse.frame_name(1700000000.123456)
        )
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'one')
        self.assertEqual(timelapse.list_frames(self.dir), [os.path.basename(path)])

    def test_save_frame_collision_disambiguates(self):
        now = 1700000000.25
        first = timelapse.save_frame(b'A', self.dir, now=now)
        second = timelapse.save_frame(b'B', self.dir, now=now)
        self.assertNotEqual(first, second)
        first_name = os.path.basename(first)
        self.assertEqual(first_name, timelapse.frame_name(now))
        prefix, ms = first_name[:-len('.jpg')].rsplit('-', 1)
        self.assertEqual(
            os.path.basename(second), f'{prefix}-{int(ms) + 1:03d}.jpg'
        )
        self.assertEqual(len(timelapse.list_frames(self.dir)), 2)

    def test_list_frames_sorted_and_filtered(self):
        for now in (1700000002.0, 1700000000.0, 1700000001.0):
            timelapse.save_frame(b'x', self.dir, now=now)
        with open(os.path.join(self.dir, 'notes.txt'), 'w') as f:
            f.write('ignore me')
        with open(os.path.join(self.dir, 'other.avi'), 'wb') as f:
            f.write(b'ignore me')
        frames = timelapse.list_frames(self.dir)
        self.assertEqual(frames, sorted(frames))
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(n.startswith('timelapse_') and n.endswith('.jpg') for n in frames))

    def test_list_frames_missing_dir_is_empty(self):
        self.assertEqual(timelapse.list_frames(os.path.join(self.dir, 'nope')), [])

    def test_list_videos_sorted_and_filtered(self):
        timelapse.save_frame(_jpeg(640, 480), self.dir, now=1700000000.0)
        with open(os.path.join(self.dir, 'notes.txt'), 'w') as f:
            f.write('ignore me')
        for name in ('b.avi', 'a.avi'):
            with open(os.path.join(self.dir, name), 'wb') as f:
                f.write(b'x')
        self.assertEqual(timelapse.list_videos(self.dir), ['a.avi', 'b.avi'])

    def test_list_videos_missing_dir_is_empty(self):
        self.assertEqual(timelapse.list_videos(os.path.join(self.dir, 'nope')), [])


class TimelapseAviTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_avi_empty_returns_none(self):
        self.assertIsNone(timelapse.build_avi(self.dir))

    def test_build_avi_writes_valid_mjpeg_avi(self):
        timelapse.save_frame(_jpeg(1280, 720), self.dir, now=1700000000.0)
        timelapse.save_frame(_jpeg(1280, 720), self.dir, now=1700000000.5)
        path = timelapse.build_avi(self.dir, fps=10)
        self.assertTrue(path.endswith('.avi'))
        self.assertTrue(os.path.basename(path).startswith('timelapse_'))
        with open(path, 'rb') as f:
            head = f.read(12)
        self.assertEqual(head[:4], b'RIFF')
        self.assertEqual(head[8:12], b'AVI ')
        info = _read_avi(path)
        print(f'AVI read-back: {info}')
        self.assertEqual(info['fcc_type'], 'vids')
        self.assertEqual(info['fcc_handler'], 'MJPG')
        self.assertEqual(info['width'], 1280)
        self.assertEqual(info['height'], 720)
        self.assertEqual(info['frame_count'], 2)
        self.assertEqual(info['movi_chunks'], 2)
        self.assertEqual(info['idx_entries'], 2)
        self.assertEqual(info['fps'], 10)
        self.assertEqual(info['scale'], 1)

    def test_build_avi_falls_back_to_caller_dimensions(self):
        timelapse.save_frame(b'not-a-jpeg', self.dir, now=1700000000.0)
        path = timelapse.build_avi(self.dir, width=640, height=480)
        info = _read_avi(path)
        self.assertEqual(info['width'], 640)
        self.assertEqual(info['height'], 480)
        self.assertEqual(info['frame_count'], 1)

    def test_build_avi_appends_status_rows(self):
        timelapse.save_frame(_jpeg(640, 480), self.dir, now=1700000000.0)
        first = timelapse.build_avi(self.dir, fps=10)
        second = timelapse.build_avi(self.dir, fps=10)
        with open(os.path.join(self.dir, timelapse.CSV_NAME), encoding='utf-8') as f:
            lines = f.read().strip().split('\n')
        self.assertEqual(lines, [
            f'{os.path.basename(first)}:{timelapse.VIDEO_STATUS_DONE}',
            f'{os.path.basename(second)}:{timelapse.VIDEO_STATUS_DONE}',
        ])

    def test_read_video_index_and_file_list_entries(self):
        os.makedirs(self.dir, exist_ok=True)
        for name in ('b-second.avi', 'a-first.avi'):
            with open(os.path.join(self.dir, name), 'wb') as f:
                f.write(b'x')
        with open(os.path.join(self.dir, timelapse.CSV_NAME), 'w', encoding='utf-8') as f:
            f.write('a-first.avi:D\n')
        self.assertEqual(timelapse.read_video_index(self.dir), {'a-first.avi': 'D'})
        # FUN_000ad7ec: <name>;<status>\n per .avi, index lookup, 'U' default.
        self.assertEqual(
            timelapse.file_list_entries(self.dir),
            'a-first.avi;D\nb-second.avi;U\n',
        )

    def test_file_list_entries_empty_without_videos(self):
        self.assertEqual(timelapse.file_list_entries(self.dir), '')


class TimelapseFileListTests(unittest.TestCase):
    def test_format_file_list_fragment(self):
        self.assertEqual(
            timelapse.format_file_list_fragment(1, 1, 'a.avi;b.avi'),
            '1;1\na.avi;b.avi',
        )

    def test_empty_listing_has_no_fragments(self):
        self.assertEqual(timelapse.file_list_fragments(''), [])

    def test_small_listing_is_one_fragment(self):
        listing = 'a.avi;b.avi'
        self.assertEqual(timelapse.file_list_fragments(listing), [(1, 1, listing)])

    def test_1024_bytes_is_still_one_fragment(self):
        listing = 'x' * 1024
        self.assertEqual(timelapse.file_list_fragments(listing), [(1, 1, listing)])

    def test_large_listing_fragments_with_total(self):
        listing = 'x' * 0x401
        fragments = timelapse.file_list_fragments(listing)
        self.assertEqual(len(fragments), 2)
        self.assertEqual(fragments[0][0], 1)
        self.assertEqual(fragments[0][1], 2)
        self.assertEqual(fragments[0][2], 'x' * 1024)
        self.assertEqual(fragments[1][0], 2)
        self.assertEqual(fragments[1][1], 2)
        self.assertEqual(fragments[1][2], 'x')
        self.assertEqual(
            ''.join(chunk for _, _, chunk in fragments), listing
        )

    def test_exact_multiple_of_1024_keeps_trailing_empty_chunk(self):
        # Firmware rule total=(size>>10)+1 means an exact 2048-byte listing
        # yields three fragments, the last empty; replicated deliberately.
        listing = 'x' * 2048
        fragments = timelapse.file_list_fragments(listing)
        self.assertEqual([f[0] for f in fragments], [1, 2, 3])
        self.assertEqual([f[1] for f in fragments], [3, 3, 3])
        self.assertEqual(fragments[2][2], '')


class MainTimelapseWiringTests(unittest.TestCase):
    """AST checks for main.py's file-list sender.

    main.py imports aiohttp/socketio, so parse its AST instead of importing it.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN_PY.read_text())

    def _function(self, name):
        return next(
            node for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )

    def test_file_list_sender_guards_empty_and_calls_send_file_list(self):
        fn = self._function('_send_timelapse_file_list')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertTrue(any(
            isinstance(n.func, ast.Attribute) and n.func.attr == 'file_list_entries'
            for n in calls
        ))
        self.assertTrue(any(
            isinstance(n.func, ast.Attribute) and n.func.attr == 'send_file_list'
            for n in calls
        ))
        guards = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.UnaryOp)
            and isinstance(n.test.op, ast.Not)
            and isinstance(n.test.operand, ast.Name)
            and n.test.operand.id == 'listing'
            and any(isinstance(b, ast.Return) for b in n.body)
        ]
        self.assertEqual(len(guards), 1)

    def test_direct_event_request_id_prefers_trigger_tag_11(self):
        fn = self._function('_request_id_from_event')
        # Prefer the recovered trigger request-id field (tag 11) over the
        # best-effort "first non-empty string" fallback.
        self.assertTrue(any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'get'
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == 11
            for n in ast.walk(fn)
        ))


class StorageStatusTests(unittest.TestCase):
    """GAP-TIMELAPSE-01: emulated-SD telemetry for extended_status.4."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_sd_present_true_for_writable_dir(self):
        self.assertTrue(timelapse.sd_present(self.dir))

    def test_sd_present_false_for_missing_path(self):
        self.assertFalse(timelapse.sd_present(os.path.join(self.dir, 'nope')))

    def test_sd_present_true_for_read_only_dir(self):
        # FUN_00071bc0 uses access(path, R_OK); write access is the separate mode
        # string, so a readable-but-not-writable mountpoint is still "present".
        with patch('timelapse.os.access', side_effect=lambda path, mode: mode == os.R_OK):
            self.assertTrue(timelapse.sd_present(self.dir))
            self.assertEqual(timelapse.sd_mode(self.dir), 'RO')

    def test_sd_space_returns_consistent_megabytes(self):
        total, free, used = timelapse.sd_space(self.dir)
        for value in (total, free, used):
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
        # FUN_000745e0 floors each MB value independently, so `used` computed
        # from (f_blocks - f_bfree) can differ from (total - free) by at most
        # 1 MB. Assert the firmware relationship without that rounding artifact.
        self.assertLessEqual(abs(used - (total - free)), 1)

    def test_storage_status_present(self):
        present, total, free, used, mode = timelapse.storage_status(self.dir)
        self.assertEqual(present, 1)
        self.assertEqual(mode, 'RW')
        self.assertEqual(total, timelapse.sd_space(self.dir)[0])
        self.assertLessEqual(abs(used - (total - free)), 1)

    def test_storage_status_absent(self):
        self.assertEqual(
            timelapse.storage_status(os.path.join(self.dir, 'nope')),
            (2, 0, 0, 0, 'UNKNOWN'),
        )

    def test_sd_mode_read_only(self):
        # Patch sd_present directly so the RO branch is reached without relying
        # on os.access side effects inside sd_present.
        with patch('timelapse.sd_present', return_value=True), \
                patch('timelapse.os.access', return_value=False):
            self.assertEqual(timelapse.sd_mode(self.dir), 'RO')


if __name__ == '__main__':
    unittest.main()
