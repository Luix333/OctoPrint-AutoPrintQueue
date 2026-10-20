import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "octoprint_autoprintqueue"))
from firstlayer import parse_first_layer  # noqa: E402

CURA = """;FLAVOR:RepRap
G90
M83
G1 X5 Y5 Z0.3 E3 ; purge in start gcode, must be ignored
;LAYER_COUNT:2
;LAYER:0
G0 F18000 X10 Y10 Z0.2
;TYPE:SKIRT
G1 F1200 X20 Y10 E0.5
G1 X20 Y20 E0.5
G0 X50 Y50
G1 X60 Y50 E0.4
;LAYER:1
G1 Z0.4
G1 X70 Y70 E1
"""

KEYS = {"z", "tools", "bounds", "segments", "method", "truncated", "lines_read"}


def parse(text, **kw):
    return parse_first_layer(text.splitlines(), **kw)


def points(polyline):
    return list(zip(polyline[0::2], polyline[1::2]))


class FirstLayerTests(unittest.TestCase):
    def test_cura_marker_method(self):
        r = parse(CURA)
        self.assertEqual(set(r), KEYS)
        self.assertEqual(r["method"], "marker")
        self.assertEqual(r["z"], 0.2)
        self.assertEqual(r["tools"], {"0": [[10, 10, 20, 10, 20, 20], [50, 50, 60, 50]]})
        self.assertEqual(r["bounds"], [10, 10, 60, 50])
        self.assertEqual(r["segments"], 3)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["lines_read"], 13)  # stopped at the second marker

    def test_prusa_absolute_e(self):
        text = "\n".join([
            "M82", "G92 E0", ";LAYER_CHANGE", ";Z:0.2", "G1 Z0.2", "G1 X0 Y0",
            "G1 X10 Y0 E1.0", "G1 X10 Y10 E2.0", "G1 E1.2", "G1 X30 Y30", "G1 E2.0",
            "G1 X40 Y30 E3.0", ";LAYER_CHANGE", "G1 X99 Y99 E9",
        ])
        r = parse(text)
        self.assertEqual(r["method"], "marker")
        self.assertEqual(r["tools"], {"0": [[0, 0, 10, 0, 10, 10], [30, 30, 40, 30]]})
        self.assertEqual(r["segments"], 3)

    def test_z_method_without_markers(self):
        text = "\n".join([
            "G1 Z0.3", "G1 X0 Y0", "M83", "G1 X10 Y0 E1", "G1 Z1.3", "G1 X20 Y0", "G1 Z0.3",
            "G1 X30 Y0 E1", "G1 Z0.6", "G1 X40 Y0 E1",
        ])
        r = parse(text)
        self.assertEqual(r["method"], "z")
        self.assertEqual(r["z"], 0.3)
        self.assertEqual(r["tools"], {"0": [[0, 0, 10, 0], [20, 0, 30, 0]]})
        self.assertEqual(r["segments"], 2)

    def test_tool_change_starts_new_polyline(self):
        text = "\n".join([
            "M83", ";LAYER:0", "T0", "G1 X0 Y0 Z0.2", "G1 X10 Y0 E1", "G1 X10 Y10 E1",
            "T3", "G1 X20 Y10 E1", ";LAYER:1",
        ])
        r = parse(text)
        self.assertEqual(set(r["tools"]), {"0", "3"})
        self.assertEqual(r["tools"]["0"], [[0, 0, 10, 0, 10, 10]])
        self.assertEqual(r["tools"]["3"], [[10, 10, 20, 10]])

    def test_relative_positioning(self):
        text = "\n".join([";LAYER:0", "G1 X0 Y0 Z0.2", "G91", "G1 X10 E1", "G1 X10 E1", ";LAYER:1"])
        r = parse(text)
        self.assertEqual(r["tools"], {"0": [[0, 0, 10, 0, 20, 0]]})

    def test_simplification_keeps_final_point(self):
        lines = [";LAYER:0", "M83", "G1 X0 Y0 Z0.2"]
        lines += ["G1 X%.2f Y0 E0.01" % (i / 100.0) for i in range(1, 101)]
        lines.append(";LAYER:1")
        r = parse("\n".join(lines), min_step=0.195)  # not 0.2: 0.6 - 0.4 is 0.1999... in floats
        poly = r["tools"]["0"]
        self.assertEqual(len(poly), 1)
        self.assertEqual([p[0] for p in points(poly[0])], [0, 0.2, 0.4, 0.6, 0.8, 1.0])
        self.assertTrue(all(p[1] == 0 for p in points(poly[0])))
        self.assertEqual(r["segments"], 100)

    def assert_quarter_arc(self, poly, start, end):
        pts = points(poly)
        self.assertEqual(pts[0], start)
        self.assertEqual(pts[-1], end)
        self.assertGreaterEqual(len(pts), 4)
        for px, py in pts:
            self.assertAlmostEqual(math.hypot(px, py), 10, delta=0.02)
            self.assertGreaterEqual(px, -0.01)
            self.assertGreaterEqual(py, -0.01)

    def test_arcs(self):
        r = parse("\n".join([";LAYER:0", "M83", "G1 X10 Y0 Z0.2", "G3 X0 Y10 I-10 J0 E1", ";LAYER:1"]), min_step=0.05)
        self.assertEqual(r["segments"], 1)
        self.assert_quarter_arc(r["tools"]["0"][0], (10, 0), (0, 10))
        r = parse("\n".join([";LAYER:0", "M83", "G1 X0 Y10 Z0.2", "G2 X10 Y0 I0 J-10 E1", ";LAYER:1"]), min_step=0.05)
        self.assert_quarter_arc(r["tools"]["0"][0], (0, 10), (10, 0))

    def test_truncation(self):
        r = parse(CURA, max_lines=8)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["lines_read"], 8)

    def test_nothing_printed(self):
        r = parse("G28\nM104 S200")
        self.assertEqual(r["method"], "none")
        self.assertIsNone(r["z"])
        self.assertEqual(r["tools"], {})
        self.assertIsNone(r["bounds"])
        self.assertEqual(r["segments"], 0)

    def test_inline_comment_and_lower_case(self):
        r = parse(";LAYER:0\nm83\ng1 x0 y0 z0.2\ng1 x10 y0 e1 ; comment\n;LAYER:1")
        self.assertEqual(r["tools"], {"0": [[0, 0, 10, 0]]})

    def test_no_space_parameters(self):
        r = parse(";LAYER:0\nM83\nG1X0Y0Z0.2\nG1X10Y0E1\n;LAYER:1")
        self.assertEqual(r["tools"], {"0": [[0, 0, 10, 0]]})

    def test_negative_raft_layer_is_first(self):
        r = parse(";LAYER:-2\nM83\nG1 X0 Y0 Z0.3\nG1 X5 Y0 E1\n;LAYER:-1\nG1 X9 Y9 E1")
        self.assertEqual(r["tools"], {"0": [[0, 0, 5, 0]]})
        self.assertEqual(r["z"], 0.3)

    def test_e_only_and_travel_are_not_extrusion(self):
        r = parse(";LAYER:0\nM83\nG1 X0 Y0 Z0.2\nG1 E5\nG1 E-1\nG0 X10 Y0\nG1 X10 Y0 E1\n;LAYER:1")
        self.assertEqual(r["tools"], {})
        self.assertEqual(r["segments"], 0)


if __name__ == "__main__":
    unittest.main()
