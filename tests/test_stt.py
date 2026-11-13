import unittest

import stt



class ChunksTest(unittest.TestCase):
    def test_short_audio_is_one_window(self):
        audio = list(range(stt.SAMPLE_RATE * 5))
        self.assertEqual(list(stt._chunks(audio)), [audio])


    def test_windows_cover_every_sample_once_plus_overlap(self):
        size = stt.SAMPLE_RATE * stt.CHUNK_S
        step = size - stt.SAMPLE_RATE * stt.STRIDE_S
        for total in (size + step, size + 1, size * 3 + 7):
            audio = list(range(total))
            windows = list(stt._chunks(audio))
            self.assertEqual(windows[-1][-1], total - 1, total)
            covered = set()
            for w in windows:
                covered.update(w)
            self.assertEqual(len(covered), total, total)
            self.assertEqual(sum(len(w) for w in windows),
                             total + (len(windows) - 1) *
                             stt.SAMPLE_RATE * stt.STRIDE_S, total)


class StitchTest(unittest.TestCase):
    def test_joins_chunk_texts(self):
        self.assertEqual(stt._stitch(["hello there", "general kenobi"]),
                         "hello there general kenobi")

    def test_overlap_at_a_boundary_is_dropped_once(self):
        self.assertEqual(stt._stitch(["we went to the", "to the store"]),
                         "we went to the store")

    def test_repeated_words_inside_a_chunk_are_kept(self):
        self.assertEqual(stt._stitch(["that that is", "is not"]),
                         "that that is not")


if __name__ == "__main__":
    unittest.main()
