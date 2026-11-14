import unittest

from offline_cache.frame import (
    HEADER_SIZE, MAGIC, FRAME_RECORD, FRAME_END, FrameError,
    build_frame, iter_frames,
)


class FrameTests(unittest.TestCase):
    def test_record_frame_roundtrip(self):
        payload = b'\x00\x01\x02\xff' * 10
        raw = build_frame(FRAME_RECORD, sequence=7, store_index=2, payload=payload)
        frames = list(iter_frames(raw))
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame.kind, FRAME_RECORD)
        self.assertEqual(frame.sequence, 7)
        self.assertEqual(frame.store_index, 2)
        self.assertEqual(frame.payload, payload)
        self.assertEqual(len(raw), HEADER_SIZE + len(payload))

    def test_end_frame_has_empty_payload(self):
        raw = build_frame(FRAME_END, sequence=3, store_index=0, payload=b'')
        frame = next(iter(iter_frames(raw)))
        self.assertEqual(frame.kind, FRAME_END)
        self.assertEqual(frame.payload, b'')

    def test_multiple_frames_in_one_chunk(self):
        chunk = b''.join([
            build_frame(FRAME_RECORD, 0, 0, b'a'),
            build_frame(FRAME_RECORD, 1, 0, b'bb'),
            build_frame(FRAME_END, 2, 0, b''),
        ])
        frames = list(iter_frames(chunk))
        self.assertEqual([f.sequence for f in frames], [0, 1, 2])
        self.assertEqual([f.payload for f in frames], [b'a', b'bb', b''])

    def test_bad_magic_rejected(self):
        raw = bytearray(build_frame(FRAME_RECORD, 0, 0, b'x'))
        raw[0] = ord('X')
        with self.assertRaises(FrameError):
            list(iter_frames(bytes(raw)))

    def test_tampered_payload_detected(self):
        raw = bytearray(build_frame(FRAME_RECORD, 0, 0, b'original'))
        raw[-1] ^= 0xFF
        with self.assertRaises(FrameError):
            list(iter_frames(bytes(raw)))

    def test_truncated_header_and_body_rejected(self):
        raw = build_frame(FRAME_RECORD, 0, 0, b'body')
        with self.assertRaises(FrameError):
            list(iter_frames(raw[:HEADER_SIZE - 1]))
        with self.assertRaises(FrameError):
            list(iter_frames(raw[:-1]))

    def test_unknown_kind_rejected(self):
        raw = build_frame(99, 0, 0, b'')
        with self.assertRaises(FrameError):
            list(iter_frames(raw))

    def test_magic_constant(self):
        self.assertEqual(MAGIC, b'TBEF')


if __name__ == '__main__':
    unittest.main()
