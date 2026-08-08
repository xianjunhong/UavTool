import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    from ui.pages import PlotCropPage
except Exception:
    QApplication = None
    PlotCropPage = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class PlotColumnWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = PlotCropPage()
        self.viewer = self.page.viewer
        self.viewer.full_w = 1000
        self.viewer.full_h = 1000
        self.viewer.pixel_to_geo = lambda x, y: (float(x), float(y))
        self.viewer.geo_to_pixel = lambda x, y: (float(x), float(y))

    def _start_new_column(self, start_end="b"):
        self.page._column_edit_context = {
            "column_id": "column_1",
            "original_plots": [],
            "insert_index": 0,
            "prefix": "A-",
            "start": 1,
            "padding": 2,
            "start_end": start_end,
            "is_new": True,
        }
        dividers = [
            ((100, 100), (300, 100)),
            ((100, 200), (300, 200)),
            ((100, 300), (300, 300)),
        ]
        self.viewer.start_column_edit(dividers, start_end)
        self.page._replace_active_column_items(dividers, start_end, refresh_ui=True)
        self.page._set_column_editing(True)

    def test_reverse_direction_reassigns_first_number_to_other_end(self):
        self._start_new_column("b")
        first_before = next(item for item in self.page.plot_polygons if item["name"] == "A-01")
        self.assertEqual(first_before["column_position"], 1)
        self.assertEqual(
            [item.text for item in self.viewer.column_name_items],
            ["A-02", "A-01"],
        )
        self.assertEqual(
            [item.text for item in self.viewer.column_endpoint_labels],
            ["A端", "B端 · 起点"],
        )

        self.viewer.reverse_column_direction()

        first_after = next(item for item in self.page.plot_polygons if item["name"] == "A-01")
        self.assertEqual(first_after["column_position"], 0)
        self.assertEqual(
            [item.text for item in self.viewer.column_name_items],
            ["A-01", "A-02"],
        )
        self.assertEqual(
            [item.text for item in self.viewer.column_endpoint_labels],
            ["A端 · 起点", "B端"],
        )

    def test_all_column_plot_names_use_bold_high_contrast_badges(self):
        self._start_new_column("a")

        self.assertEqual(len(self.viewer.column_name_items), 2)
        self.assertTrue(all(item.text for item in self.viewer.column_name_items))
        self.assertTrue(
            all(item.font.bold() for item in self.viewer.column_name_items)
        )
        self.assertGreaterEqual(
            self.viewer.column_endpoint_labels[0].font.pointSize(),
            16,
        )

    def test_add_delete_and_cancel_restore_original_data(self):
        self._start_new_column("a")
        self.assertEqual(len(self.page.plot_polygons), 2)

        self.viewer.set_selected_column_plot(0)
        self.viewer.add_column_divider()
        self.assertEqual(len(self.page.plot_polygons), 3)

        self.assertTrue(self.viewer.delete_selected_column_divider())
        self.assertEqual(len(self.page.plot_polygons), 2)

        self.page.cancel_column_edit()
        self.assertEqual(self.page.plot_polygons, [])
        self.assertFalse(self.viewer.column_edit_active)

    def test_finish_keeps_live_saved_column(self):
        self._start_new_column("a")
        self.page.finish_column_edit()

        self.assertEqual(len(self.page.plot_polygons), 2)
        self.assertIsNone(self.page._column_edit_context)
        self.assertFalse(self.viewer.column_edit_active)

    def test_finished_column_can_be_reopened_from_metadata(self):
        self._start_new_column("a")
        self.page.finish_column_edit()
        self.page.plot_list.setCurrentRow(0)

        self.page.edit_selected_column()

        self.assertTrue(self.viewer.column_edit_active)
        self.assertEqual(len(self.viewer.column_dividers), 3)
        self.assertEqual(self.page._column_edit_context["column_id"], "column_1")

    def test_moving_outer_corner_recalculates_internal_rail(self):
        self._start_new_column("a")
        origin = self.viewer.get_column_dividers()

        self.viewer._move_outer_column_corner(
            origin,
            divider_index=0,
            side=0,
            moved=(50.0, 50.0),
        )

        self.assertEqual(self.viewer.column_dividers[0][0], (50.0, 50.0))
        self.assertEqual(self.viewer.column_dividers[-1][0], origin[-1][0])
        self.assertNotEqual(self.viewer.column_dividers[1][0], origin[1][0])
        self.assertEqual(self.viewer.column_dividers[1][1], origin[1][1])

    def test_selected_overlay_hides_other_labels_and_map_hit_returns_global_index(self):
        self.viewer.set_saved_polygons(
            [
                {
                    "index": 4,
                    "name": "A-01",
                    "pixels": [(0, 0), (10, 0), (10, 10), (0, 10)],
                },
                {
                    "index": 7,
                    "name": "A-02",
                    "pixels": [(20, 0), (30, 0), (30, 10), (20, 10)],
                },
            ]
        )
        self.viewer.set_selected_saved_polygon(4)

        self.assertTrue(self.viewer.saved_label_items[0].isVisible())
        self.assertFalse(self.viewer.saved_label_items[1].isVisible())
        self.assertEqual(
            self.viewer._saved_polygon_index_at(QPointF(25, 5)),
            7,
        )

    def test_map_click_selects_without_entering_edit_or_recentering(self):
        self.page.plot_polygons = [
            {
                "name": "A-01",
                "geo_points": [(10, 10), (20, 10), (20, 20), (10, 20)],
            },
            {
                "name": "A-02",
                "geo_points": [(30, 10), (40, 10), (40, 20), (30, 20)],
            },
        ]
        self.page._refresh_plot_list()
        center_calls = []
        self.viewer.centerOn = lambda *args: center_calls.append(args)

        self.page.on_map_plot_clicked(1)

        self.assertEqual(self.page.plot_list.currentRow(), 1)
        self.assertEqual(self.viewer.selected_saved_polygon_index, 1)
        self.assertEqual(self.page._editing_plot_index, -1)
        self.assertEqual(self.viewer.get_polygon_pixels(), [])
        self.assertEqual(center_calls, [])

    def test_active_column_can_be_renamed_and_metadata_updates(self):
        self._start_new_column("b")

        names = self.page._apply_active_column_naming("田块-A-", 12, 3)

        self.assertEqual(names, ["田块-A-012", "田块-A-013"])
        column_items = sorted(
            self.page.plot_polygons,
            key=lambda item: item["column_position"],
        )
        self.assertEqual(
            [item["name"] for item in column_items],
            ["田块-A-013", "田块-A-012"],
        )
        self.assertTrue(
            all(item["column_prefix"] == "田块-A-" for item in column_items)
        )
        self.assertTrue(all(item["column_start"] == 12 for item in column_items))
        self.assertTrue(all(item["column_padding"] == 3 for item in column_items))
        self.assertEqual(
            [item.text for item in self.viewer.column_name_items],
            ["田块-A-013", "田块-A-012"],
        )

    def test_active_column_rename_rejects_names_used_by_other_plots(self):
        self._start_new_column("a")
        self.page.plot_polygons.append(
            {
                "name": "B-01",
                "geo_points": [(400, 100), (500, 100), (500, 200), (400, 200)],
            }
        )

        with self.assertRaisesRegex(ValueError, "重复"):
            self.page._apply_active_column_naming("B-", 1, 2)

        column_names = {
            item["name"]
            for item in self.page.plot_polygons
            if item.get("column_id") == "column_1"
        }
        self.assertEqual(column_names, {"A-01", "A-02"})

    def test_crossed_shared_boundaries_are_rejected(self):
        self.assertFalse(
            self.viewer._column_geometry_is_valid(
                [
                    ((0, 0), (10, 0)),
                    ((10, 10), (0, 10)),
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
