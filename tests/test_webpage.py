"""The notebook page: escaping, stable layout, and the shared UI script."""
import json
import os
import re
import unittest

import webpage
from tests import fixtures

PAGE = {"id": "2208.1.12.25", "strokes": 12, "svg": "pages/2208.1.12.25/page.svg",
        "thumb": "pages/2208.1.12.25/thumb.png",
        "bounds": [100.0, 200.0, 900.0, 1200.0]}
SESSION = {"id": "PRS-1a", "urls": ["audio/PRS-1a.0.aac"],
           "notebook": "0123456789abcdef", "pages": ["2208.1.12.25"]}


def page_with(index):
    fixtures.reset_sandbox()
    os.makedirs(os.path.dirname(webpage.INDEX), exist_ok=True)
    with open(webpage.INDEX, "w") as f:
        json.dump(index, f)
    webpage._last_good = None
    return webpage.build_index()


class LayoutTest(unittest.TestCase):
    def test_every_image_declares_its_size(self):
        html = page_with({"pages": [PAGE, dict(PAGE, id="1.2.3.4", bounds=None)],
                          "sessions": []})
        images = re.findall(r"<img [^>]*>", html)
        self.assertEqual(len(images), 2)
        for img in images:
            self.assertRegex(img, r'width="\d+" height="\d+"')

    def test_thumbnail_size_follows_the_ink_bounds(self):
        self.assertEqual(webpage.thumb_size(PAGE), (320, 400))

    def test_cards_stagger_but_only_for_the_first_few(self):
        pages = [dict(PAGE, id=f"1.2.3.{i}") for i in range(20)]
        html = page_with({"pages": pages, "sessions": []})
        delays = [int(d) for d in re.findall(r"--i:(\d+)", html)]
        self.assertEqual(max(delays), webpage.STAGGER_LIMIT)

    def test_busy_buttons_keep_their_size(self):
        css = webpage._read("style.css")
        self.assertIn(".btn.busy, .icon-btn.busy { color: transparent;", css)
        self.assertIn("aspect-ratio: 4 / 5", css)

    def test_animations_respect_reduced_motion(self):
        css = webpage._read("style.css")
        motion = css.index("@media (prefers-reduced-motion: no-preference)")
        self.assertNotIn("animation: rise", css[:motion])


class ContentTest(unittest.TestCase):
    def test_titles_and_text_are_escaped(self):
        evil = '<script>alert(1)</script>"'
        html = page_with({"pages": [dict(PAGE, title=evil, text=evil, ocr=True)],
                          "sessions": [dict(SESSION, title=evil,
                                            transcript=evil)]})
        self.assertNotIn("<script>alert", html.split("<script>\n", 1)[0])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;&quot;", html)

    def test_transcribed_pages_offer_download_instead_of_transcribe(self):
        html = page_with({"pages": [dict(PAGE, text="hello", ocr=True)],
                          "sessions": []})
        self.assertIn("Download .txt", html)
        self.assertNotIn('class="btn primary transcribe"', html)

    def test_empty_library_explains_what_to_do(self):
        html = page_with({"pages": [], "sessions": []})
        self.assertIn("No notes yet", html)
        self.assertIn("./run sync", html)

    def test_each_section_has_a_hidden_no_match_note(self):
        html = page_with({"pages": [PAGE], "sessions": [SESSION]})
        self.assertIn('<div class="empty nomatch" hidden>No matching pages</div>',
                      html)
        self.assertIn("No matching recordings", html)

    def test_counts_have_ids_the_script_updates(self):
        html = page_with({"pages": [PAGE], "sessions": [SESSION]})
        self.assertIn('id="n-pages">1<', html)
        self.assertIn('id="n-sessions">1<', html)


class ScriptTest(unittest.TestCase):
    def test_results_use_toasts_not_alerts(self):
        js = webpage._read("app.js")
        self.assertNotIn("alert(", js)
        self.assertIn("function toast(", js)

    def test_template_has_every_placeholder_filled(self):
        html = page_with({"pages": [PAGE], "sessions": [SESSION]})
        self.assertNotIn("$style", html)
        self.assertNotIn("$body", html)
        self.assertNotIn("$script", html)


if __name__ == "__main__":
    unittest.main()
