import unittest

import tests  # noqa: F401

import stt


class ChunksTest(unittest.TestCase):
    def test_short_audio_is_one_window(self):
        audio = list(range(stt.SAMPLE_RATE * 5))
        self.assertEqual(list(stt._chunks(audio)), [audio])


class StitchTest(unittest.TestCase):
    def test_joins_chunk_texts(self):
        self.assertEqual(stt._stitch(["hello there", "general kenobi"]),
                         "hello there general kenobi")


if __name__ == "__main__":
    unittest.main()
