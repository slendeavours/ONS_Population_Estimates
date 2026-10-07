import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blank_reader import CellResult, UnknownCellError, read_cell  # noqa: E402

M = {"..": "suppressed"}


class T(unittest.TestCase):
    def test_real_zero_float(self):
        self.assertEqual(read_cell(0.0, M), CellResult(Decimal(0), None))

    def test_real_zero_string(self):
        self.assertEqual(read_cell("0", M), CellResult(Decimal(0), None))

    def test_marker_gives_null_and_flag(self):
        self.assertEqual(read_cell("..", M), CellResult(None, "suppressed"))

    def test_number_with_commas_and_spaces(self):
        self.assertEqual(read_cell(" 1,234 ", M), CellResult(Decimal(1234), None))

    def test_float_integer(self):
        self.assertEqual(read_cell("12.0", M), CellResult(Decimal(12), None))

    def test_none_and_empty_need_declaring(self):
        for raw in (None, ""):
            with self.assertRaises(UnknownCellError):
                read_cell(raw, M)
            self.assertEqual(read_cell(raw, {"": "missing"}),
                             CellResult(None, "missing"))

    def test_unknown_marker_raises(self):
        with self.assertRaises(UnknownCellError):
            read_cell("n/a", M)

    def test_dash_only_where_declared(self):
        with self.assertRaises(UnknownCellError):
            read_cell("-", M)
        self.assertEqual(read_cell("-", {"-": "suppressed"}),
                         CellResult(None, "suppressed"))

    def test_bad_flag_name_rejected(self):
        with self.assertRaises(ValueError):
            read_cell("..", {"..": "zero"})

    def test_bool_nan_inf_rejected(self):
        for raw in (True, False, float("nan"), float("inf"), "NaN", "inf"):
            with self.assertRaises(UnknownCellError):
                read_cell(raw, M)

    def test_negative_decimal_and_types(self):
        self.assertEqual(read_cell("-1.5", M).value, Decimal("-1.5"))
        self.assertEqual(read_cell(7, M).value, Decimal(7))
        self.assertEqual(read_cell(Decimal("2.5"), M).value, Decimal("2.5"))
        self.assertIsInstance(read_cell(0.1, M).value, Decimal)


if __name__ == "__main__":
    unittest.main()
