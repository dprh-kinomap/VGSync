# -*- coding: utf-8 -*-
#
# This file is part of VGSync.
#
# Copyright (C) 2025 by Bernd Eller
#
# VGSync is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# VGSync is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with VGSync. If not, see <https://www.gnu.org/licenses/>.
#

from PySide6.QtWidgets import QWidget

from PySide6.QtCore import Qt, QPoint, Signal, QPointF, QRect
from datetime import timedelta
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QWheelEvent, QPolygonF, QFont
)
                           

ELE_EPS = 0.05
class ChartWidget(QWidget):
    markerClicked = Signal(int)
    raiseTrackRequested = Signal(float)
    elevationPointEdited = Signal(int, float)  # index, new_elevation

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._gpx_data = []
        self._speed_cap = 70.0

        # Ausschnitt/Zoom
        self._zoom_factor = 1.0
        self._min_zoom = 1.0
        self._max_zoom = 50.0
        self._horizontal_offset = 0.0

        self._marker_index = 0

        # Dragging
        self._dragging_scroll = False
        self._drag_start_x = 0.0
        self._offset_start = 0.0

        # Chart-Layouts
        self._chart_height_top = 0.6
        self._chart_height_bottom = 0.3
        self._scroll_speed_px = 40

        # Neuer Schwellenwert (z.B. 1 km/h)
        self._zero_speed_threshold = 1.0
        
        # ausgrauen
        self._sync_idx_start = None
        self._sync_idx_end   = None
        
        self._usl_indices = []        # alle Indizes mit ele < 0
        self._usl_idx_cursor = -1     # Cursor fürs Durchklicken
        self._usl_badge_rect = None   # Klickfläche des Warn-Badges
        
         # ---------------------------
        # **NEU**: Schwellenwert für Stops
        self._stop_threshold = 1.0   # z.B. Default 1 Sekunde

        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)
        
        self._usl_badge_rect = None   # clickable area of the badge
        self._usl_idx_cursor = -1     # cycling cursor for USL points
        # Elevation edit mode
        self._elevation_edit_mode = False
        self._ele_control_indices = []
        self._dragging_ele_idx = None
        self._drag_prev_anchor = None
        self._drag_next_anchor = None
        self._drag_seg_start = None
        self._drag_seg_end = None
        self._drag_orig_elevations = None
        self._ele_control_radius = 6
        self._ele_control_hit_radius = 14
        self._preferred_ele_control_indices = set()
        self._ele_min_anchor_gap = 6
        self._ele_control_hit_x_radius = 18
        self._ele_control_hit_y_radius = 20
        
    def _prevideo_cut_idx(self) -> int:
        """
        Index-Grenze für das Vorlauf-Grau bei negativem Video-Shift.
        Alles < return-Wert gilt als 'grau' (ausgeschlossen).
        """
        try:
            from core.gpx_parser import get_gpx_video_shift
            shift = get_gpx_video_shift()
        except Exception:
            shift = 0
        if not (self._gpx_data and shift is not None and shift < 0 and self._gpx_data[0].get("time")):
            return 0
        from datetime import timedelta
        positive_time = self._gpx_data[0]["time"] + timedelta(seconds=abs(shift))
        cut_idx = 0
        for i, pt in enumerate(self._gpx_data):
            ti = pt.get("time")
            if ti and ti >= positive_time:
                cut_idx = i
                break
            else:
                cut_idx = i + 1
        return max(0, min(cut_idx, len(self._gpx_data)))
    
    def _effective_usl_indices(self):
        """
        Liefert die Liste aller 'unter Meer' Indizes, ABER
        ausgeschlossen werden:
          - B..E (self._sync_idx_start.._end), falls gesetzt
          - Vorlaufbereich 0..pre_cut_idx-1 bei negativem Shift
        """
        base = [i for i, pt in enumerate(self._gpx_data)
                if (pt.get("ele") is not None and pt["ele"] < -ELE_EPS)]
        if not base:
            return []
        # Vorlauf (pre-video) grau ausschließen
        cut0 = self._prevideo_cut_idx()
        base = [i for i in base if i >= cut0]

        # Markierter B..E-Bereich ausschließen (falls gesetzt)
        if getattr(self, "_sync_idx_start", None) is not None and getattr(self, "_sync_idx_end", None) is not None:
            i0 = int(min(self._sync_idx_start, self._sync_idx_end))
            i1 = int(max(self._sync_idx_start, self._sync_idx_end))
            base = [i for i in base if not (i0 <= i <= i1)]

        return base

    
        
    def set_stop_threshold(self, value: float):
        self._stop_threshold = value
        self.update()  # Damit das Diagramm neu gezeichnet wird    
        

    # -----------------------------------------------------
    # Getter/Setter für die neue Zero-Speed-Einstellung
    # -----------------------------------------------------
    def set_zero_speed_threshold(self, threshold: float):
        """
        Legt den Geschwindigkeits-Schwellenwert fest.
        Werte darunter werden im Diagramm rot markiert.
        """
        self._zero_speed_threshold = threshold
        self.update()

    def zero_speed_threshold(self) -> float:
        return self._zero_speed_threshold

    # -----------------------------------------------------
    # Andere bekannte Setter
    # -----------------------------------------------------
    def set_speed_cap(self, new_limit: float):
        """Setzt das max. Speed-Limit und refresht."""
        self._speed_cap = new_limit
        self.update()

    def set_gpx_data(self, data):
        self._gpx_data = data if data else []
        self._marker_index = 0
        self._zoom_factor = 1.0
        self._horizontal_offset = 0.0
        self._usl_indices = [i for i, pt in enumerate(self._gpx_data) if pt.get("ele", 0.0) < 0.0]
        self._usl_idx_cursor = -1
        self._usl_badge_rect = None
        self._preferred_ele_control_indices = set()
        self._rebuild_elevation_control_indices()
        self.update()

    def refresh_after_elevation_edit(self, preferred_idx: int | None = None):
        """Refresh draggable elevation controls without resetting zoom/scroll."""
        self._usl_indices = [i for i, pt in enumerate(self._gpx_data) if pt.get("ele", 0.0) < 0.0]
        if preferred_idx is not None and 0 <= preferred_idx < len(self._gpx_data):
            self._preferred_ele_control_indices.add(int(preferred_idx))
        self._rebuild_elevation_control_indices()
        self.update()

    def _find_effective_drag_anchors(self, anchor_pos: int, dragged_idx: int):
        """Return previous/next anchors with enough spacing for a visible drag effect."""
        prev_idx = None
        next_idx = None

        for pos in range(anchor_pos - 1, -1, -1):
            candidate = self._ele_control_indices[pos]
            if dragged_idx - candidate >= self._ele_min_anchor_gap:
                prev_idx = candidate
                break

        for pos in range(anchor_pos + 1, len(self._ele_control_indices)):
            candidate = self._ele_control_indices[pos]
            if candidate - dragged_idx >= self._ele_min_anchor_gap:
                next_idx = candidate
                break

        # Fallbacks: if nearby controls are dense, still use the outermost control
        # on each side so the dragged point can influence a larger section.
        if prev_idx is None and anchor_pos > 0:
            prev_idx = self._ele_control_indices[0]
            if prev_idx == dragged_idx:
                prev_idx = None
        if next_idx is None and anchor_pos < len(self._ele_control_indices) - 1:
            next_idx = self._ele_control_indices[-1]
            if next_idx == dragged_idx:
                next_idx = None

        return prev_idx, next_idx

    def _elevation_plot_range(self, elevations):
        """Return the elevation range used for drawing and hit testing."""
        min_ele = min(elevations)
        max_ele = max(elevations)
        plot_min_ele = min_ele
        plot_max_ele = max_ele

        # Add 20% headroom on each side for better scaling
        ele_range = max_ele - min_ele
        if ele_range > 0:
            headroom = ele_range * 0.2
            plot_min_ele = min_ele - headroom
            plot_max_ele = max_ele + headroom

        if abs(plot_max_ele - plot_min_ele) < 0.1:
            plot_max_ele += 0.1
            plot_min_ele -= 0.1

        return plot_min_ele, plot_max_ele

    def _pick_elevation_control_point(self, click_x: float, click_y: float):
        """Find the best draggable elevation control near the cursor."""
        if not (self._elevation_edit_mode and self._ele_control_indices):
            return None

        count = len(self._gpx_data)
        if count < 2:
            return None

        w = self.width()
        h = self.height()
        chart_width = w * self._zoom_factor
        ele_vals = [pt.get("ele", 0.0) for pt in self._gpx_data]
        plot_min_ele, plot_max_ele = self._elevation_plot_range(ele_vals)
        top_height = int(self._chart_height_top * h)

        def x_for_index(i: int) -> float:
            ratio = i / (count - 1)
            return ratio * chart_width - self._horizontal_offset

        def y_for_ele(e: float) -> float:
            frac = (e - plot_min_ele) / (plot_max_ele - plot_min_ele)
            return top_height - (frac * (top_height - 20))

        best_idx = None
        best_score = None

        for idx in self._ele_control_indices:
            if not (0 <= idx < count):
                continue
            xx = x_for_index(idx)
            yy = y_for_ele(ele_vals[idx])
            dx = abs(xx - click_x)
            dy = abs(yy - click_y)

            in_rect = (
                dx <= self._ele_control_hit_x_radius
                and dy <= self._ele_control_hit_y_radius
            )
            in_circle = (dx * dx + dy * dy) <= float(self._ele_control_hit_radius ** 2)
            if not (in_rect or in_circle):
                continue

            score = (dx, dy, dx * dx + dy * dy)
            if best_score is None or score < best_score:
                best_score = score
                best_idx = idx

        return best_idx

    def _rebuild_elevation_control_indices(self):
        """Rebuild the list of control indices used in elevation edit mode.

        Anchors are placed at inflection-like positions of the elevation curve:
        where the discrete slope changes sign (local minima/maxima), plus start/end.
        """
        self._ele_control_indices = []
        count = len(self._gpx_data)
        if count == 0:
            return

        elevations = [float(pt.get("ele", 0.0)) for pt in self._gpx_data]
        noise_eps = 1e-3

        def sign_of_delta(delta: float) -> int:
            if abs(delta) < noise_eps:
                return 0
            return 1 if delta > 0.0 else -1

        # Always include first and last point. Strength is used when we need
        # to cap the number of controls later.
        anchor_strengths = {
            0: float("inf"),
            count - 1: float("inf"),
        }

        def add_anchor(idx: int, strength: float = 1.0):
            idx = max(0, min(idx, count - 1))
            prev = anchor_strengths.get(idx, 0.0)
            anchor_strengths[idx] = max(prev, float(strength))

        if count >= 3:
            i = 1
            while i < count - 1:
                run_start = i
                run_end = i

                # Collapse flat plateaus so flat-bottom valleys and flat-top peaks
                # receive a single anchor in their middle instead of being missed.
                while (
                    run_end < count - 1
                    and abs(elevations[run_end + 1] - elevations[run_end]) < noise_eps
                ):
                    run_end += 1

                left_delta = elevations[run_start] - elevations[run_start - 1]
                right_delta = (
                    elevations[run_end + 1] - elevations[run_end]
                    if run_end < count - 1
                    else 0.0
                )
                left_sign = sign_of_delta(left_delta)
                right_sign = sign_of_delta(right_delta)
                turn_strength = abs(left_delta) + abs(right_delta)

                if left_sign != 0 and right_sign != 0 and left_sign != right_sign:
                    add_anchor((run_start + run_end) // 2, turn_strength)
                elif run_end > run_start:
                    if left_sign != 0:
                        add_anchor(run_start, abs(left_delta))
                    if right_sign != 0:
                        add_anchor(run_end, abs(right_delta))
                else:
                    if left_sign == 0 and right_sign != 0:
                        add_anchor(run_start, abs(right_delta))
                    elif left_sign != 0 and right_sign == 0:
                        add_anchor(run_start, abs(left_delta))

                i = run_end + 1

        # Fill very large gaps so broad smooth valleys/ridges still expose
        # a few draggable handles after rebuilding.
        max_anchor_gap = 24
        sorted_anchor_indices = sorted(anchor_strengths)
        for left_idx, right_idx in zip(sorted_anchor_indices, sorted_anchor_indices[1:]):
            gap = right_idx - left_idx
            if gap > max_anchor_gap:
                steps = gap // max_anchor_gap
                for step_idx in range(1, steps + 1):
                    interp_idx = left_idx + round(gap * step_idx / (steps + 1))
                    if 0 < interp_idx < count - 1:
                        add_anchor(interp_idx, 0.25)

        # Keep previously edited controls draggable even if the local shape was
        # smoothed enough that they are no longer strict extrema.
        for idx in self._preferred_ele_control_indices:
            if 0 < idx < count - 1:
                add_anchor(idx, float("inf"))

        # Limit the number of anchors to keep UI responsive, but preserve the
        # strongest extrema first so valleys are less likely to disappear.
        max_controls = 120
        if len(anchor_strengths) > max_controls:
            protected = {0, count - 1}
            ranked = sorted(
                (idx for idx in anchor_strengths if idx not in protected),
                key=lambda idx: (-anchor_strengths[idx], idx),
            )
            keep = set(ranked[: max(0, max_controls - len(protected))])
            keep.update(protected)
            self._ele_control_indices = sorted(keep)
        else:
            self._ele_control_indices = sorted(anchor_strengths)

    def set_elevation_edit_mode(self, enabled: bool):
        """Enable or disable elevation edit mode (editing directly on the chart)."""
        self._elevation_edit_mode = bool(enabled)
        self._dragging_ele_idx = None
        self.update()

    def elevation_edit_mode(self) -> bool:
        return self._elevation_edit_mode

    def highlight_gpx_index(self, index: int):
        """Springt im Chart zum GPX-Punkt `index`."""
        if not self._gpx_data:
            return
        if index < 0:
            index = 0
        if index >= len(self._gpx_data):
            index = len(self._gpx_data) - 1
        self._marker_index = index
        self._keep_marker_visible()
        self.update()

    def _keep_marker_visible(self):
        """
        Verhindert, dass der Marker aus dem sichtbaren Bereich rutscht.
        Schiebt ggf. self._horizontal_offset so, dass der Marker x-Koordinate
        in ~80% des sichtbaren Bereichs bleibt.
        """
        count = len(self._gpx_data)
        if count <= 0:
            return

        w = self.width()
        chart_width = w * self._zoom_factor

        if count > 1:
            ratio = self._marker_index / (count - 1)
        else:
            ratio = 0.0

        marker_x = ratio * chart_width

        if marker_x < self._horizontal_offset:
            self._horizontal_offset = marker_x
        elif marker_x > self._horizontal_offset + (0.8 * w):
            self._horizontal_offset = marker_x - (0.8 * w)

        if self._horizontal_offset < 0:
            self._horizontal_offset = 0
        max_offset = chart_width - w
        if max_offset < 0:
            max_offset = 0
        if self._horizontal_offset > max_offset:
            self._horizontal_offset = max_offset

    # -----------------------------------------------------
    # Mouse/Scroll/Zoom
    # -----------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self._index_for_x(event.pos().x())
            self._marker_index = idx
            self.update()
            self.markerClicked.emit(idx)
            event.accept()
        elif event.button() == Qt.RightButton:
            self._dragging_scroll = True
            self._drag_start_x = event.pos().x()
            self._offset_start = self._horizontal_offset
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging_scroll:
            delta_x = event.pos().x() - self._drag_start_x
            new_offset = self._offset_start - delta_x
            if new_offset < 0:
                new_offset = 0
            self._horizontal_offset = new_offset
            self.update()
            event.accept()
        elif self._elevation_edit_mode and self._dragging_ele_idx is not None:
            # Live update elevation: interpolate between fixed neighbour anchors.
            # Previous and next anchors stay at their original elevation; only
            # interior points (including the dragged one) move along a smooth line.
            idx = self._dragging_ele_idx
            if (
                0 <= idx < len(self._gpx_data)
                and self._drag_seg_start is not None
                and self._drag_seg_end is not None
                and self._drag_orig_elevations is not None
            ):
                count = len(self._gpx_data)
                if count >= 2:
                    w = self.width()
                    h = self.height()

                    # Use current global elevation range for y <-> ele mapping
                    ele_vals_all = [pt.get("ele", 0.0) for pt in self._gpx_data]
                    plot_min_ele, plot_max_ele = self._elevation_plot_range(ele_vals_all)
                    top_height = int(self._chart_height_top * h)

                    # invert y_for_ele for current mouse y
                    y = float(event.pos().y())
                    y_clamped = max(20.0, min(float(top_height), y))
                    frac = (top_height - y_clamped) / max(1e-6, (top_height - 20.0))
                    new_ele = plot_min_ele + frac * (plot_max_ele - plot_min_ele)

                    seg_start = self._drag_seg_start
                    seg_end = self._drag_seg_end
                    orig = self._drag_orig_elevations
                    if not orig:
                        event.accept()
                        return

                    prev_idx = self._drag_prev_anchor
                    next_idx = self._drag_next_anchor

                    # Clamp anchors to segment and bounds
                    if prev_idx is not None and (prev_idx < seg_start or prev_idx > seg_end):
                        prev_idx = None
                    if next_idx is not None and (next_idx < seg_start or next_idx > seg_end):
                        next_idx = None

                    # Start by restoring original elevations for the whole segment
                    for j, i in enumerate(range(seg_start, seg_end + 1)):
                        self._gpx_data[i]["ele"] = orig[j]

                    # Helper to fetch original elevation from cached segment
                    def orig_ele_at(i: int) -> float:
                        return orig[i - seg_start]

                    if prev_idx is None and next_idx is not None:
                        # First anchor is the dragged point; interpolate dragged->next
                        seg_a = idx
                        seg_b = next_idx
                        ele_a = new_ele
                        ele_b = orig_ele_at(seg_b)
                        length = max(1, seg_b - seg_a)
                        for i in range(seg_a, seg_b + 1):
                            t = (i - seg_a) / float(length)
                            s = t * t * (3.0 - 2.0 * t)  # smoothstep spline
                            self._gpx_data[i]["ele"] = ele_a + s * (ele_b - ele_a)
                    elif prev_idx is not None and next_idx is None:
                        # Last anchor is the dragged point; interpolate prev->dragged
                        seg_a = prev_idx
                        seg_b = idx
                        ele_a = orig_ele_at(seg_a)
                        ele_b = new_ele
                        length = max(1, seg_b - seg_a)
                        for i in range(seg_a, seg_b + 1):
                            t = (i - seg_a) / float(length)
                            s = t * t * (3.0 - 2.0 * t)  # smoothstep spline
                            self._gpx_data[i]["ele"] = ele_a + s * (ele_b - ele_a)
                    elif prev_idx is not None and next_idx is not None:
                        # Middle control: piecewise interpolation prev->dragged and dragged->next,
                        # keeping prev and next fixed at their original values.
                        ele_prev = orig_ele_at(prev_idx)
                        ele_next = orig_ele_at(next_idx)

                        # prev_idx .. idx
                        if idx > prev_idx:
                            seg_a = prev_idx
                            seg_b = idx
                            length1 = max(1, seg_b - seg_a)
                            for i in range(seg_a, seg_b + 1):
                                t = (i - seg_a) / float(length1)
                                s = t * t * (3.0 - 2.0 * t)  # smoothstep spline
                                self._gpx_data[i]["ele"] = ele_prev + s * (new_ele - ele_prev)

                        # idx .. next_idx
                        if next_idx > idx:
                            seg_a = idx
                            seg_b = next_idx
                            length2 = max(1, seg_b - seg_a)
                            for i in range(seg_a, seg_b + 1):
                                t = (i - seg_a) / float(length2)
                                s = t * t * (3.0 - 2.0 * t)  # smoothstep spline
                                self._gpx_data[i]["ele"] = new_ele + s * (ele_next - new_ele)
                    else:
                        # No neighbours known: move only this point
                        self._gpx_data[idx]["ele"] = new_ele

                    self.update()
            event.accept()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._dragging_scroll:
            self._dragging_scroll = False
            event.accept()
        elif event.button() == Qt.LeftButton and self._elevation_edit_mode and self._dragging_ele_idx is not None:
            # Finish elevation edit and notify listeners
            idx = self._dragging_ele_idx
            self._dragging_ele_idx = None
            self._drag_prev_anchor = None
            self._drag_next_anchor = None
            self._drag_seg_start = None
            self._drag_seg_end = None
            self._drag_orig_elevations = None
            if 0 <= idx < len(self._gpx_data):
                new_ele = float(self._gpx_data[idx].get("ele", 0.0))
                self.elevationPointEdited.emit(idx, new_ele)
            event.accept()
        else:
            event.ignore()

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return

        mods = event.modifiers()
        if (mods & Qt.ShiftModifier):
            # horizontal scroll with Shift+wheel
            if delta > 0:
                self._horizontal_offset = max(0, self._horizontal_offset - self._scroll_speed_px)
            else:
                self._horizontal_offset += self._scroll_speed_px
            self.update()
            event.accept()
            return

        # Default: zoom with mouse wheel (no modifier needed)
        factor = 1.1 if delta > 0 else (1.0 / 1.1)
        new_zoom = self._zoom_factor * factor
        if new_zoom < self._min_zoom:
            new_zoom = self._min_zoom
        if new_zoom > self._max_zoom:
            new_zoom = self._max_zoom

        old_zoom = self._zoom_factor
        self._zoom_factor = new_zoom
        self._center_marker(0.3)
        self.update()
        event.accept()

    def _center_marker(self, widget_ratio: float):
        count = len(self._gpx_data)
        if count < 2:
            return
        w = self.width()
        chart_width = w * self._zoom_factor
        ratio = self._marker_index / (count - 1)
        marker_x_abs = ratio * chart_width
        desired_x_in_widget = widget_ratio * w
        self._horizontal_offset = marker_x_abs - desired_x_in_widget
        if self._horizontal_offset < 0:
            self._horizontal_offset = 0

    # -----------------------------------------------------
    # Painting
    # -----------------------------------------------------
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
    
        rect_ = self.rect()
        w = rect_.width()
        h = rect_.height()
        painter.fillRect(rect_, QColor("#222222"))
        
        
        

        # ------------------------------------------------------
        # LEGENDE (oben links)
        # ------------------------------------------------------
        legend_x = 10
        legend_y = 20

        # SPEED
        painter.setPen(QPen(Qt.white, 1))
        painter.drawText(legend_x, legend_y, "Speed:")

        fm = painter.fontMetrics()
        speed_text_width = fm.horizontalAdvance("Speed:")
        line_start_x = legend_x + speed_text_width + 5
        line_start_y = legend_y - 5
        line_end_x = line_start_x + 30
        line_end_y = line_start_y

        # Speed-Legendenlinie (cyan, 3px)
        painter.setPen(QPen(QColor("cyan"), 3))
        painter.drawLine(line_start_x, line_start_y, line_end_x, line_end_y)

        # HEIGHT
        next_block_x = line_end_x + 20
        painter.setPen(QPen(Qt.white, 1))
        painter.drawText(next_block_x, legend_y, "Height:")
    
        height_text_width = fm.horizontalAdvance("Height:")
        height_line_start_x = next_block_x + height_text_width + 5
        height_line_start_y = legend_y - 5
        height_line_end_x = height_line_start_x + 30
        height_line_end_y = height_line_start_y
    
        # Height-Legendenlinie (yellow, 3px)
        painter.setPen(QPen(QColor("yellow"), 3))
        painter.drawLine(
            height_line_start_x,
            height_line_start_y,
            height_line_end_x,
            height_line_end_y
        )
    
        # ------------------------------------------------------
        # GPX-Daten prüfen
        # ------------------------------------------------------
        count = len(self._gpx_data)
        if count < 2:
            painter.setPen(QColor("white"))
            painter.drawText(10, 60, "No GPX data for chart.")
            return

        chart_width = w * self._zoom_factor
    
        # ------------------------------------------------------
        # ELE / SPEED ermitteln und skalieren
        # ------------------------------------------------------
        ele_vals = [pt.get("ele", 0.0) for pt in self._gpx_data]
    
        speed_vals = []
        for pt in self._gpx_data:
            raw_spd = pt.get("speed_kmh", 0.0)
            capped_spd = min(raw_spd, self._speed_cap)
            speed_vals.append(capped_spd)
    
        min_ele, max_ele = min(ele_vals), max(ele_vals)
        min_spd, max_spd = min(speed_vals), max(speed_vals)

        # Make a plotting range that is robust and includes 0m when it
        # makes sense. If all elevations are above 0, include 0 as
        # lower bound so the 0m-line is placed correctly below the
        # lowest data point (instead of coinciding with it).
        plot_min_ele, plot_max_ele = self._elevation_plot_range(ele_vals)

        if abs(max_spd - min_spd) < 0.1:
            max_spd += 0.1
            min_spd -= 0.1
    
        top_height = int(self._chart_height_top * h)
        bottom_height = int(self._chart_height_bottom * h)
    
        def x_for_index(i: int) -> float:
            ratio = i / (count - 1)
            return ratio * chart_width - self._horizontal_offset
    
        def y_for_ele(e: float) -> float:
            frac = (e - plot_min_ele) / (plot_max_ele - plot_min_ele)
            return top_height - (frac * (top_height - 20))
    
        def y_for_speed(s: float) -> float:
            frac = (s - min_spd) / (max_spd - min_spd)
            speed_range = bottom_height - 20
            y0 = top_height + 10
            return y0 + (bottom_height - 20) - (frac * speed_range)
    
        # Pfade für Elevation/Speed
        path_ele = []
        path_spd = []
        for i in range(count):
            x_ = x_for_index(i)
            path_ele.append((x_, y_for_ele(ele_vals[i])))
            path_spd.append((x_, y_for_speed(speed_vals[i])))
    
        # ------------------------------------------------------
        # Linien zeichnen (Elevation = gelb, Speed = cyan)
        # ------------------------------------------------------
        def draw_polyline(painter, pts, color, thickness=2):
            painter.setPen(QPen(color, thickness))
            for idx in range(len(pts) - 1):
                x1, y1 = pts[idx]
                x2, y2 = pts[idx + 1]
                if (x1 < -50 and x2 < -50):
                    continue
                if (x1 > w + 50 and x2 > w + 50):
                    continue
                painter.drawLine(x1, y1, x2, y2)

        # 1) Elevation-Linie (gelb, 2px)
        draw_polyline(painter, path_ele, QColor(255, 255, 0), thickness=2)

        # Optional: elevation edit skeleton (thicker overlay with control points)
        if self._elevation_edit_mode and self._ele_control_indices:
            # overlay line
            draw_polyline(painter, path_ele, QColor(255, 215, 0, 220), thickness=3)
            # control points as larger circles
            painter.setBrush(QBrush(QColor(255, 165, 0)))
            painter.setPen(QPen(QColor(0, 0, 0, 150), 1))
            control_radius = self._ele_control_radius
            for idx in self._ele_control_indices:
                if 0 <= idx < len(path_ele):
                    xx, yy = path_ele[idx]
                    if -20 < xx < w + 20:
                        painter.drawEllipse(QPointF(xx, yy), control_radius, control_radius)

        # --- NEU: 0-Meter-Linie im Höhenbereich --------------------------------
        # Fälle:
        #  - min_ele <= 0 <= max_ele  -> Linie exakt bei 0 m (deutlich, gestrichelt)
        #  - min_ele > 0              -> dezente Linie am unteren Rand des Höhenplots
        #  - max_ele < 0              -> dezente Linie am oberen Rand des Höhenplots
        
        
        # --- 0-Meter-Linie im Höhenbereich (robust) -------------------------------
        # Compute zero-line position using the plotting range. If 0m is
        # within the plotted range, draw the 0m line at the correct
        # vertical position. Otherwise draw a subtle guide at the edge
        # of the elevation plot.
        eps = 1e-6
        has_only_above = (min_ele > 0.0 + eps)
        has_only_below = (max_ele < 0.0 - eps)

        if plot_min_ele <= 0.0 <= plot_max_ele:
            zero_y = y_for_ele(0.0)
            pen = QPen(QColor(255, 80, 80), 1, Qt.DashLine)
            label = "0 m"
        elif has_only_above:
            # all data above zero -> subtle line at bottom of elevation plot
            zero_y = int(top_height) - 1
            pen = QPen(QColor(140, 140, 140), 1, Qt.DashLine)
            label = f"{int(min_ele)} m"
        else:
            # all data below zero -> subtle line at top of elevation plot
            zero_y = 20
            pen = QPen(QColor(140, 140, 140), 1, Qt.DashLine)
            label = f"{int(max_ele)} m"

        painter.setPen(pen)
        painter.drawLine(0, zero_y, w, zero_y)

        # Label the line with the correct elevation
        try:
            lab_font = QFont(self.font().family(), max(4, int(h * 0.025)))
            painter.setFont(lab_font)
        except Exception:
            pass
        painter.drawText(4, int(zero_y) - 2, label)
        # -------------------------------------------------------------------------

        # ------------------------------------------------------------------------

        # 2) Speed-Linie (cyan, 1px)
        draw_polyline(painter, path_spd, QColor(0, 255, 255), thickness=1)
    
        
        # ------------------------------------------------------
        # Null-Linie (0 km/h) dünn weiß
        # ------------------------------------------------------
        
        painter.setPen(QPen(QColor("white"), 1))
        zero_speed_y = y_for_speed(0.0)
        painter.drawLine(0, zero_speed_y, w, zero_speed_y)
    
        # ------------------------------------------------------
        # Bereich für Geschwindigkeiten < zero_speed_threshold rot markieren
        # ------------------------------------------------------
        zst = self._zero_speed_threshold  # z.B. 1 km/h
        y_axis_speed = y_for_speed(0)     # x-Achse für Speed
    
        # Wir nutzen ein "Füll-Polygon" für jede zusammenhängende Unterschreitung.
        painter.setBrush(QColor(255, 0, 0, 100))  # halbtransparentes Rot
        painter.setPen(Qt.NoPen)
    
        sub_threshold_polygon = []
        in_segment = False
    
        for i in range(count):
            if i == 0:
                continue  # Ersten Punkt überspringen
            x_, y_ = path_spd[i]
            spd_ = speed_vals[i]
    
            # Unterhalb Schwelle?
            if spd_ < zst:
                # Segment anfangen, falls wir noch nicht "drin" sind
                if not in_segment:
                    sub_threshold_polygon.append((x_, y_axis_speed))
                    in_segment = True
                sub_threshold_polygon.append((x_, y_))
            else:
                # Falls wir gerade ein "rotes" Segment hatten, jetzt schließen
                if in_segment:
                    sub_threshold_polygon.append((x_, y_axis_speed))
                    # Zeichnen der Polygonfläche
                    poly = QPolygonF()
                    for (px, py) in sub_threshold_polygon:
                        poly.append(QPointF(px, py))
                    painter.drawPolygon(poly)
                    sub_threshold_polygon.clear()
                    in_segment = False
    
        # Falls Segment bis zum letzten Punkt offen
        if in_segment and len(sub_threshold_polygon) > 0:
            sub_threshold_polygon.append((path_spd[-1][0], y_axis_speed))
            poly = QPolygonF()
            for (px, py) in sub_threshold_polygon:
                poly.append(QPointF(px, py))
            painter.drawPolygon(poly)
    
        # ------------------------------------------------------
        # Rote Marker an der x-Achse für alle Punkte < zst
        # ------------------------------------------------------
        painter.setPen(QPen(QColor(255, 0, 0), 4))
        for i in range(count):
            if i == 0:
                continue
            x_, y_ = path_spd[i]
            spd_ = speed_vals[i]
            if spd_ < zst:
                # Kleiner senkrechter Strich nach unten (5px)
                painter.drawLine(x_, y_axis_speed, x_, y_axis_speed + 15)
    
        # ------------------------------------------------------
        # **NEU**: Blaue Marker für "Stops"
        # wenn Zeitdifferenz > self._stop_threshold
        # ------------------------------------------------------
        
        painter.setPen(QPen(QColor(255, 215, 0), 3))  # Time Gaps: GELB (Gold)
        #painter.setPen(QPen(QColor(0, 153, 255), 3))  # Time Gaps: BLAU
        #painter.setPen(QPen(QColor(0, 255, 255), 4))  # Cyan
        #painter.setPen(QPen(QColor(173, 255, 47), 4)) # Lime
        #painter.setPen(QPen(QColor(255, 105, 180), 4))# Pink
        #painter.setPen(QPen(QColor(148, 0, 211), 4))  # Violett
        
        #painter.setPen(QPen(QColor(255, 255, 255), 4))# Weiß
        
        for i in range(1, count):
            # Zeitdifferenz zwischen Punkt i-1 und i:
            dt = (self._gpx_data[i]["time"] - self._gpx_data[i-1]["time"]).total_seconds() if self._gpx_data[i]["time"] else 0
            if dt > self._stop_threshold:
                # x_-Koordinate des Punktes i (bereits in path_spd gespeichert)
                x_ = path_spd[i][0]
                # Hier zeichnen wir einen Strich nach oben (15px) vom zero_speed_y:
                painter.drawLine(x_, zero_speed_y, x_, zero_speed_y + 15)
        
        # === Sync-Overlay (aus der GPX-Liste) ==============================
        if (self._sync_idx_start is not None and
            self._sync_idx_end   is not None and
            self._gpx_data and
            path_spd and len(path_spd) == len(self._gpx_data)):

            i0 = int(max(0, min(self._sync_idx_start, len(self._gpx_data) - 1)))
            i1 = int(max(0, min(self._sync_idx_end,   len(self._gpx_data) - 1)))
            if i0 > i1:
                i0, i1 = i1, i0

            # x-Koordinaten direkt aus path_spd (kein x_for_index nötig)
            x0 = path_spd[i0][0]
            x1 = path_spd[i1][0]

            left  = max(0, min(w, min(x0, x1)))
            right = max(0, min(w, max(x0, x1)))

            if right - left > 1:
                painter.save()
                try:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(0, 0, 0, 110))  # halbtransparentes Grau
                    painter.drawRect(left, 0, right - left, h)
                finally:
                    painter.restore()
        # ===================================================================

        
        # ------------------------------------------------------
        # Kreise auf den Datenpunkten (Elevation = gelb, Speed = cyan)
        # ------------------------------------------------------
        painter.setPen(Qt.NoPen)
    
        # Elevation-Kreise
        painter.setBrush(QBrush(QColor(255, 255, 0)))
        ele_radius = 1
        for (xx, yy) in path_ele:
            if -10 < xx < w + 10:
                painter.drawEllipse(QPointF(xx, yy), ele_radius, ele_radius)
    
        # Speed-Kreise
        painter.setBrush(QBrush(QColor(0, 255, 255)))
        speed_radius = 0.7
        for (xx, yy) in path_spd:
            if -10 < xx < w + 10:
                painter.drawEllipse(QPointF(xx, yy), speed_radius, speed_radius)
        
        # --- Label links an der Speed-Basislinie: "0 km/h" ---
        try:
            lab_font = QFont(self.font().family(), max(8, int(h * 0.025)))
            painter.setFont(lab_font)
        except Exception:
            pass
        # y-Position der 0-km/h-Linie: nutze die gleiche Skalierung wie für die Speed-Kurve
        y0_spd = int(y_for_speed(0.0))
        # Falls 0 außerhalb des sichtbaren Speed-Bereichs liegen sollte, am unteren Rand einklemmen:
        y0_spd = max(top_height, min(h - 1, y0_spd))
        painter.setPen(QColor(180, 180, 180))
        painter.drawText(6, y0_spd - 2, "0 km/h")
        
        # --- X-Axis Time Labels ---
        if self._gpx_data and self._gpx_data[0].get("time"):
            try:
                time_label_font = QFont(self.font().family(), max(7, int(h * 0.02)))
                painter.setFont(time_label_font)
            except Exception:
                pass
            
            painter.setPen(QColor(150, 150, 150))
            
            # Calculate time span
            start_time = self._gpx_data[0]["time"]
            end_time = self._gpx_data[-1]["time"]
            total_secs = (end_time - start_time).total_seconds()
            
            # Determine a good interval for time labels (every 60s, 300s, 600s, 1800s, etc.)
            if total_secs <= 300:
                interval_secs = 60
            elif total_secs <= 600:
                interval_secs = 120
            elif total_secs <= 1800:
                interval_secs = 300
            elif total_secs <= 3600:
                interval_secs = 600
            else:
                interval_secs = 1800
            
            # Draw time labels at intervals
            for offset in range(0, int(total_secs) + 1, interval_secs):
                current_time = start_time + timedelta(seconds=offset)
                
                # Find index closest to this time
                idx = 0
                for i, pt in enumerate(self._gpx_data):
                    if pt.get("time") and pt["time"] <= current_time:
                        idx = i
                
                x = x_for_index(idx)
                if -10 < x < w + 10:
                    # Format time as MM:SS
                    minutes = offset // 60
                    seconds = offset % 60
                    time_str = f"{minutes}:{seconds:02d}"
                    # Draw label higher up (at bottom of chart area, with padding)
                    painter.drawText(int(x) - 20, h - 35, 40, 15, Qt.AlignCenter, time_str)
                    
                    # Draw a small tick mark
                    painter.drawLine(int(x), h - 22, int(x), h - 27)
        
        try:
            from core.gpx_parser import get_gpx_video_shift
            shift = get_gpx_video_shift()
        except Exception:
            shift = 0

        if shift is not None and shift < 0 and self._gpx_data and self._gpx_data[0].get("time"):
            positive_time = self._gpx_data[0]["time"] + timedelta(seconds=abs(shift))

            # Finde den ersten Index, dessen Zeit >= positive_time ist
            cut_idx = 0
            for i, pt in enumerate(self._gpx_data):
                ti = pt.get("time")
                if ti and ti >= positive_time:
                    cut_idx = i
                    break
                else:
                    cut_idx = i + 1  # falls alle < positive_time

            if cut_idx > 0:
                # x-Position des letzten "grauen" Punktes
                x_right = path_spd[min(cut_idx - 1, len(path_spd)-1)][0]
                # sichtbare Breite begrenzen (Scroll/Zoom beachten)
                fill_to = x_right
                if fill_to > 0:
                    if fill_to > w:
                        fill_to = w
                    # halbtransparent grau über den gesamten Chart-Bereich
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(128, 128, 128, 90))
                    painter.drawRect(0, 0, fill_to, h)
        # --------------------------------------------------------------------------
        
        # ------------------------------------------------------
        # Marker-Linie und Info-Texte
        # ------------------------------------------------------
        m_x = x_for_index(self._marker_index)
        # ... dein bestehender Marker-Zeichencode ...

        # === NEU: Warn-Badge "unter 0 m" einblenden, wenn min_ele < 0 ===
        usl_list = self._effective_usl_indices()
        
        if usl_list:  # nur wenn es (gefilterte) USL-Punkte gibt
            # Text & Style
            badge_text = "⚠ under sea level ⚠ (0m)"
            try:
                lab_font = QFont(self.font().family(), max(8, int(h * 0.025)))
                painter.setFont(lab_font)
            except Exception:
                pass
            fm = painter.fontMetrics()

            # Position: links oben UNTER der Legende (3 Zeilen * Schrift-Höhe)
            margin = 8
            legend_lines = 3  # "Speed:", "Height:", + Werteblock
            y_top = margin + legend_lines * fm.height() + 6
            x_left = margin

            pad_x = 8
            pad_y = 4
            tw = fm.horizontalAdvance(badge_text)
            th = fm.height()
            rect_w = tw + 2 * pad_x
            rect_h = th + 2 * pad_y
            
            self._usl_badge_rect = QRect(x_left, y_top, rect_w, rect_h)
            
            # Kapsel (halbtransparent), rote Kontur, gut lesbarer Text
            painter.setBrush(QColor(60, 20, 20, 200))
            painter.setPen(QPen(QColor(255, 80, 80), 1))
            painter.drawRoundedRect(x_left, y_top, rect_w, rect_h, 6, 6)

            painter.setPen(QColor(255, 160, 160))
            painter.drawText(x_left + pad_x, y_top + pad_y + fm.ascent(), badge_text)

        
        
        
        # ------------------------------------------------------
        # Marker-Linie und Info-Texte
        # ------------------------------------------------------
        m_x = x_for_index(self._marker_index)
        if -50 < m_x < w + 50:
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawLine(m_x, 0, m_x, h)
            pt_ = self._gpx_data[self._marker_index]
    
            ele_val = pt_['ele']
            spd_val = speed_vals[self._marker_index]  # gecappter Wert
            grad_val = pt_.get("gradient", 0.0)
    
            #line1 = f"{ele_val:.1f}".replace(".", ",") + "m"
            e_show = ele_val if abs(ele_val) >= ELE_EPS else 0.0
            line1  = f"{e_show:.1f}".replace(".", ",") + "m"
            line2 = f"{spd_val:.1f}".replace(".", ",") + "km/h"
            line3 = f"{grad_val:.1f}".replace(".", ",") + "%"
    
            y_start = 40
            y_step = 15
    
            painter.setPen(QPen(QColor("white"), 1))
            painter.drawText(m_x + 5, y_start, line1)
            painter.drawText(m_x + 5, y_start + y_step, line2)
            painter.drawText(m_x + 5, y_start + 2 * y_step, line3)
    
                
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 1) Klick auf den USL-Badge => Dialog
            if self._usl_badge_rect and self._usl_badge_rect.contains(event.pos()):
                usl = self._effective_usl_indices()
                if not usl:
                    event.accept()
                    return
                # tiefster Punkt suchen
                min_idx = min(usl, key=lambda i: (self._gpx_data[i].get("ele", 0.0)))
                min_ele = self._gpx_data[min_idx].get("ele", 0.0)

                # Dialog (englisch)
                from PySide6.QtWidgets import QMessageBox
                msg = QMessageBox(self)
                msg.setWindowTitle("Under sea level")
                msg.setText(f"Deepest point: {min_ele:.2f} m\nWhat would you like to do?")
                show_btn = msg.addButton("Show", QMessageBox.AcceptRole)
                move_btn = msg.addButton("Raise track above sea level", QMessageBox.ActionRole)
                cancel_btn = msg.addButton("Cancel", QMessageBox.RejectRole)
                msg.exec()

                btn = msg.clickedButton()
                if btn is show_btn:
                    # marker + notify world
                    self.highlight_gpx_index(min_idx)
                    self.markerClicked.emit(min_idx)
                elif btn is move_btn:
                    # delta = -min_ele + buffer (aktuell 0)
                    buffer_m = 0.0
                    delta = max(0.0, -min_ele + buffer_m)
                    if delta > 0:
                        self.raiseTrackRequested.emit(delta)
                event.accept()
                return

            # 2) Elevation edit mode: start dragging nearest control point (if close)
            if self._elevation_edit_mode and self._ele_control_indices:
                click_x = event.pos().x()
                click_y = event.pos().y()
                count = len(self._gpx_data)
                if count >= 2:
                    nearest_idx = self._pick_elevation_control_point(click_x, click_y)
                    if nearest_idx is not None:
                        # Keep chart selection, list selection, and drag target aligned.
                        self._marker_index = nearest_idx
                        self.update()
                        self.markerClicked.emit(nearest_idx)
                        self._dragging_ele_idx = nearest_idx
                        # Precompute segment anchors for this drag (previous/next control)
                        try:
                            pos = self._ele_control_indices.index(nearest_idx)
                        except ValueError:
                            self._drag_prev_anchor = None
                            self._drag_next_anchor = None
                            self._drag_seg_start = None
                            self._drag_seg_end = None
                            self._drag_orig_elevations = None
                        else:
                            self._drag_prev_anchor, self._drag_next_anchor = (
                                self._find_effective_drag_anchors(pos, nearest_idx)
                            )
                            # Determine affected segment [seg_start, seg_end] and cache original elevations
                            count = len(self._gpx_data)
                            prev_idx = self._drag_prev_anchor
                            next_idx = self._drag_next_anchor
                            if prev_idx is None and next_idx is not None:
                                seg_start = nearest_idx
                                seg_end = next_idx
                            elif prev_idx is not None and next_idx is None:
                                seg_start = prev_idx
                                seg_end = nearest_idx
                            elif prev_idx is not None and next_idx is not None:
                                seg_start = prev_idx
                                seg_end = next_idx
                            else:
                                seg_start = nearest_idx
                                seg_end = nearest_idx

                            seg_start = max(0, min(seg_start, count - 1))
                            seg_end = max(0, min(seg_end, count - 1))
                            if seg_end < seg_start:
                                seg_start, seg_end = seg_end, seg_start

                            self._drag_seg_start = seg_start
                            self._drag_seg_end = seg_end
                            self._drag_orig_elevations = [
                                float(self._gpx_data[i].get("ele", 0.0))
                                for i in range(seg_start, seg_end + 1)
                            ]
                        event.accept()
                        return

            # 3) normaler Left-Click ins Chart -> Marker dahin
            idx = self._index_for_x(event.pos().x())
            self._marker_index = idx
            self.update()
            self.markerClicked.emit(idx)
            event.accept()

        elif event.button() == Qt.RightButton:
            # 1) Rechtsklick auf Badge -> USL zyklisch durchgehen
            if self._usl_badge_rect and self._usl_badge_rect.contains(event.pos()):
                usl = self._effective_usl_indices()
                if usl:
                    self._usl_idx_cursor = (self._usl_idx_cursor + 1) % len(usl)
                    idx = usl[self._usl_idx_cursor]
                    self.highlight_gpx_index(idx)
                    self.markerClicked.emit(idx)
                event.accept()
                return
            # 2) sonst: Scroll-Drag wie gehabt
            self._dragging_scroll = True
            self._drag_start_x = event.pos().x()
            self._offset_start = self._horizontal_offset
            event.accept()
        else:
            super().mousePressEvent(event)

 

    # -----------------------------------------------------
    # Hilfsfunktion: x->Index
    # -----------------------------------------------------
    def _index_for_x(self, x_screen: float) -> int:
        count = len(self._gpx_data)
        if count < 2:
            return 0
        w = self.width()
        chart_width = w * self._zoom_factor
        abs_x = x_screen + self._horizontal_offset
        ratio = abs_x / chart_width
        ratio = max(0, min(ratio, 1))
        idx_ = int(round(ratio * (count - 1)))
        return max(0, min(idx_, count - 1))
        
    def set_sync_range(self, idx_start: int, idx_end: int):
        if idx_start is None or idx_end is None:
            self._sync_idx_start = None
            self._sync_idx_end = None
        else:
            if idx_start > idx_end:
                idx_start, idx_end = idx_end, idx_start
            self._sync_idx_start = max(0, idx_start)
            self._sync_idx_end   = max(0, idx_end)
        self.update()

    def clear_sync_range(self):
        self._sync_idx_start = None
        self._sync_idx_end   = None
        self.update()
    
