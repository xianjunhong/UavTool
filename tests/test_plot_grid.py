import unittest

from logic.plot_grid import (
    column_dividers_from_quadrilateral,
    plots_from_dividers,
    redistribute_column_dividers,
    split_column_quadrilateral,
)


class SplitColumnQuadrilateralTests(unittest.TestCase):
    def test_splits_long_rectangle_into_equal_adjacent_plots(self):
        plots = split_column_quadrilateral(
            [(0, 0), (10, 0), (10, 100), (0, 100)],
            4,
        )

        self.assertEqual(len(plots), 4)
        self.assertEqual(plots[0], [(0.0, 0.0), (10.0, 0.0), (10.0, 25.0), (0.0, 25.0)])
        self.assertEqual(plots[-1], [(0.0, 75.0), (10.0, 75.0), (10.0, 100.0), (0.0, 100.0)])
        self.assertEqual(plots[0][2:], [plots[1][1], plots[1][0]])

    def test_accepts_unordered_corners(self):
        plots = split_column_quadrilateral(
            [(10, 100), (0, 0), (0, 100), (10, 0)],
            2,
        )

        self.assertEqual(len(plots), 2)
        self.assertEqual(plots[0][2:], [plots[1][1], plots[1][0]])

    def test_can_split_on_perpendicular_axis(self):
        plots = split_column_quadrilateral(
            [(0, 0), (10, 0), (10, 100), (0, 100)],
            2,
            axis="short",
        )

        self.assertEqual(plots[0], [(0.0, 0.0), (0.0, 100.0), (5.0, 100.0), (5.0, 0.0)])

    def test_rejects_non_quadrilateral(self):
        with self.assertRaisesRegex(ValueError, "4个顶点"):
            split_column_quadrilateral([(0, 0), (1, 0), (1, 1)], 2)

    def test_rejects_concave_quadrilateral(self):
        with self.assertRaisesRegex(ValueError, "凸四边形"):
            split_column_quadrilateral([(0, 0), (10, 0), (4, 4), (0, 10)], 2)

    def test_dividers_are_shared_by_adjacent_plots(self):
        dividers = column_dividers_from_quadrilateral(
            [(0, 0), (20, 0), (30, 100), (0, 100)],
            5,
        )
        plots = plots_from_dividers(dividers)

        self.assertEqual(len(dividers), 6)
        self.assertEqual(len(plots), 5)
        for index in range(len(plots) - 1):
            self.assertEqual(plots[index][3], plots[index + 1][0])
            self.assertEqual(plots[index][2], plots[index + 1][1])

    def test_redistribute_preserves_outer_frame(self):
        dividers = [
            ((0, 0), (10, 0)),
            ((1, 20), (12, 30)),
            ((0, 100), (20, 100)),
        ]
        redistributed = redistribute_column_dividers(dividers)

        self.assertEqual(redistributed[0], dividers[0])
        self.assertEqual(redistributed[-1], dividers[-1])
        self.assertEqual(redistributed[1], ((0.0, 50.0), (15.0, 50.0)))


if __name__ == "__main__":
    unittest.main()
