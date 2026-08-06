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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with VGSync.  If not, see <https://www.gnu.org/licenses/>.
#

import subprocess
import json
import shutil

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QDialogButtonBox,
    QLabel, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton, QMessageBox,
    QHBoxLayout, QButtonGroup, QRadioButton,
    QProgressDialog
)
from PySide6.QtCore import QSettings, Qt


# Helper function: quick test whether an FFmpeg encoder works

def can_encode_with(ffmpeg_enc_name, ffmpeg_path="ffmpeg", test_duration=0.5):
    """
    Tries to encode a short test video (test_duration seconds) with ffmpeg_enc_name.
    Writes to the temp directory (installer location Program Files is read-only).
    Returns True if ffmpeg exits successfully, otherwise False.
    """
    import tempfile, os, subprocess, shutil

    # Resolve ffmpeg as at runtime: first PATH, then the provided value
    if ffmpeg_path == "ffmpeg":
        ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"

    # Output file in temp directory (collision-safe)
    tmp_dir = tempfile.gettempdir()
    out_path = os.path.join(tmp_dir, "kvr_hwtest.mp4")

    # Cleanup in case a previous file was left behind
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except Exception:
        pass

    try:
        cmd = [
            ffmpeg_path, "-hide_banner", "-y",
            "-f", "lavfi",
            "-i", "color=black:r=24:size=320x240",
            "-t", str(test_duration),
            "-c:v", ffmpeg_enc_name,
            "-an",
            out_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False
    finally:
        # best effort cleanup
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass


# UI labels for codec / hardware (internal keys stay x264, x265, nvidia_hevc, …)
_CODEC_OPTIONS = [
    ("x264", "H.264 (AVC)"),
    ("x265", "HEVC (H.265)"),
]
_HW_DISPLAY = {
    "CPU": "CPU (software, libx264/libx265)",
    "nvidia_h264": "NVIDIA NVENC — H.264",
    "nvidia_hevc": "NVIDIA NVENC — HEVC",
    "amd_h264": "AMD AMF — H.264",
    "amd_hevc": "AMD AMF — HEVC",
    "intel_h264": "Intel Quick Sync — H.264",
    "intel_hevc": "Intel Quick Sync — HEVC",
}

_H264_HW_KEYS = {"CPU", "nvidia_h264", "amd_h264", "intel_h264"}
_HEVC_HW_KEYS = {"CPU", "nvidia_hevc", "amd_hevc", "intel_hevc"}


class EncoderSetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Encoder Setup")

        self.settings = QSettings("VGSync", "VGSync")
        
        # Stores the tested set of working HW encoders,
        # loaded from QSettings (or freshly detected).
        self._cached_detected_hw = None

        main_layout = QVBoxLayout(self)

        # Top action: match output settings to first source video
        self.btn_from_input = QPushButton("Match Source", self)
        top_row = QHBoxLayout()
        top_row.addWidget(self.btn_from_input)
        top_row.addStretch(1)
        main_layout.addLayout(top_row)

        form_layout = QFormLayout()
        main_layout.addLayout(form_layout)

        # (A) Resolution
        self.resolution_combo = QComboBox()
        self.resolution_options = [
            ((640, 360),  "640x360 (SD)"),
            ((854, 480),  "854x480 (nHD)"),
            ((1280, 720), "1280x720 (HD)"),
            ((1920,1080), "1920x1080 (Full HD)"),
            ((2560,1440), "2560x1440 (QHD 2K)"),
            ((3840,2160), "3840x2160 (4K UHD)")
        ]
        for wh, label in self.resolution_options:
            self.resolution_combo.addItem(label, userData=wh)
        form_layout.addRow("Resolution:", self.resolution_combo)

        # (B) Codec: H.264 vs HEVC (stored as x264 / x265 for ffmpeg lib names)
        self.container_combo = QComboBox()
        for codec_id, label in _CODEC_OPTIONS:
            self.container_combo.addItem(label, userData=codec_id)
        form_layout.addRow("Codec:", self.container_combo)

        # (C) Hardware
        self.hw_combo = QComboBox()
        self.btn_detect_hw = QPushButton("Detect", self)
        hw_row = QHBoxLayout()
        hw_row.addWidget(self.hw_combo)
        hw_row.addWidget(self.btn_detect_hw)
        form_layout.addRow("Hardware:", hw_row)

        # (D) CRF
        self.radio_crf = QRadioButton("Use CRF")
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(12, 50)
        form_layout.addRow(self.radio_crf, self.crf_spin)

        # (E) Preset
        self.radio_preset = QRadioButton("Use Preset")
        self.preset_combo = QComboBox()
        cpu_presets = ["ultrafast", "superfast", "veryfast", "faster",
                       "fast", "medium", "slow", "slower", "veryslow"]
        for p in cpu_presets:
            self.preset_combo.addItem(p)
        form_layout.addRow(self.radio_preset, self.preset_combo)

        # (H) Bitrate (Mbit/s)
        self.radio_bitrate = QRadioButton("Use Bitrate")
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(1, 200)
        form_layout.addRow(self.radio_bitrate, self.bitrate_spin)
        self.bitrate_mode_combo = QComboBox()
        self.bitrate_mode_combo.addItem("VBR", userData="vbr")
        self.bitrate_mode_combo.addItem("CBR", userData="cbr")
        form_layout.addRow("Bitrate Mode:", self.bitrate_mode_combo)

        # (F) FPS
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 120.0)
        self.fps_spin.setDecimals(3)
        self.fps_spin.setSingleStep(0.1)
        form_layout.addRow("FPS:", self.fps_spin)

        # (G) Xfade
        self.xfade_spin = QSpinBox()
        self.xfade_spin.setRange(0, 30)
        form_layout.addRow("X-Fade (s):", self.xfade_spin)

        # Buttons (OK/Cancel)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        main_layout.addWidget(btns, alignment=Qt.AlignRight)

        # Base signal connections
        btns.accepted.connect(self.on_ok_clicked)
        btns.rejected.connect(self.reject)
        self.btn_detect_hw.clicked.connect(self.on_detect_hw_clicked)
        self.btn_from_input.clicked.connect(self.on_from_input_clicked)
        self.container_combo.currentIndexChanged.connect(self.update_hw_options)
        self._rate_mode_group = QButtonGroup(self)
        self._rate_mode_group.setExclusive(True)
        self._rate_mode_group.addButton(self.radio_crf)
        self._rate_mode_group.addButton(self.radio_bitrate)
        self._rate_mode_group.addButton(self.radio_preset)
        self.radio_crf.toggled.connect(self._on_rate_mode_toggled)
        self.radio_bitrate.toggled.connect(self._on_rate_mode_toggled)
        self.radio_preset.toggled.connect(self._on_rate_mode_toggled)

        # Load initial values from QSettings
        self.load_from_settings()
        # Then refresh HW combo options
        self.update_hw_options()

        # IMPORTANT: connect signals that mutate widgets only at the very end.
        # This avoids slots firing before all widgets are initialized.
        self.resolution_combo.currentIndexChanged.connect(self.on_resolution_changed)
        self._on_rate_mode_toggled()


    # ---------------------------
    # Ordered helper functions
    # ---------------------------
    def _default_bitrate_for(self, res_wh: tuple[int, int]) -> int:
        """Returns the default bitrate (Mbit/s) for a given resolution."""
        mapping = {
            (640, 360): 2,
            (854, 480): 5,
            (1280, 720): 10,
            (1920, 1080): 20,
            (2560, 1440): 35,
            (3840, 2160): 50,
        }
        return mapping.get(res_wh, 20)

    def _current_rate_mode(self) -> str:
        if self.radio_bitrate.isChecked():
            return "bitrate"
        if self.radio_preset.isChecked():
            return "preset"
        return "crf"

    def _on_rate_mode_toggled(self):
        mode = self._current_rate_mode()
        self.crf_spin.setEnabled(mode == "crf")
        self.bitrate_spin.setEnabled(mode == "bitrate")
        self.bitrate_mode_combo.setEnabled(mode == "bitrate")
        self.preset_combo.setEnabled(mode == "preset")

    def on_resolution_changed(self, _idx: int):
        """
        Slot: called when the user changes resolution in the dialog.
        Sets bitrate to the default value for the selected resolution.
        """
        # Fallback: if the spinbox name differs across versions,
        # try alternative attributes (robust against version differences)
        spin = getattr(self, "bitrate_spin", None) or getattr(self, "bitrate_mbps_spin", None)
        if spin is None:
            # Defensive: spinbox not found -> do nothing (no crash)
            print("[WARN] Bitrate spinbox not found; skip auto-update.")
            return

        w, h = self.resolution_combo.currentData()
        default_bitrate = self._default_bitrate_for((w, h))

        # Temporarily block signals to avoid feedback loops.
        spin.blockSignals(True)
        spin.setValue(default_bitrate)
        spin.blockSignals(False)

    def _ensure_resolution_option(self, res_wh: tuple[int, int], label=None) -> int:
        """
        Ensures the given resolution exists in both `self.resolution_options`
        (source of truth) and `self.resolution_combo` (UI).
        """
        for i, (wh, _existing_label) in enumerate(self.resolution_options):
            if wh == res_wh:
                return i

        w, h = res_wh
        if label is None:
            label = f"{w}x{h} (Custom)"

        # Keep internal options and UI in sync.
        self.resolution_options.append((res_wh, label))
        self.resolution_combo.addItem(label, userData=res_wh)
        return len(self.resolution_options) - 1


    # ---------------------------
    # load / save
    # ---------------------------
    def load_from_settings(self):
        """Reads QSettings and updates dialog fields."""

        # 1) Resolution
        wdef = self.settings.value("encoder/res_w", 1920, type=int)
        hdef = self.settings.value("encoder/res_h", 1080, type=int)
        stored_res = (wdef, hdef)
        found_idx = None
        for i, (wh, _label) in enumerate(self.resolution_options):
            if wh == stored_res:
                found_idx = i
                break

        # If an auto-detected resolution was not part of the defaults,
        # add it back so the dialog reflects the stored values.
        if found_idx is None:
            found_idx = self._ensure_resolution_option(stored_res)
        # setCurrentIndex here: signal is NOT connected yet (connected at the end)
        self.resolution_combo.setCurrentIndex(found_idx)

        # 2) Codec (x264 / x265 stored in settings)
        container_val = self.settings.value("encoder/container", "x265", type=str)
        idx_c = self.container_combo.findData(container_val)
        if idx_c < 0:
            idx_c = 0
        self.container_combo.setCurrentIndex(idx_c)

        # 3) CRF
        crf_val = self.settings.value("encoder/crf", 20, type=int)
        self.crf_spin.setValue(crf_val)

        # 4) Preset
        preset_val = self.settings.value("encoder/preset", "fast", type=str)
        idx_p = self.preset_combo.findText(preset_val)
        if idx_p < 0:
            idx_p = 0
        self.preset_combo.setCurrentIndex(idx_p)

        # 5) FPS
        fps_val = self.settings.value("encoder/fps", 30.0, type=float)
        self.fps_spin.setValue(fps_val)

        # 6) Xfade
        xfade_val = self.settings.value("encoder/xfade", 2, type=int)
        self.xfade_spin.setValue(xfade_val)
        
        # 7) Bitrate (resolution-based defaults)
        bitrate_val = self.settings.value("encoder/bitrate_mbps", None)
        if bitrate_val is None:
            bitrate_val = self._default_bitrate_for(stored_res)
        self.bitrate_spin.setValue(int(bitrate_val))

        # 7b) Rate mode + bitrate mode
        rc_mode = self.settings.value("encoder/rate_control_mode", "crf", type=str).lower()
        if rc_mode == "bitrate":
            self.radio_bitrate.setChecked(True)
        elif rc_mode == "preset":
            self.radio_preset.setChecked(True)
        else:
            self.radio_crf.setChecked(True)

        br_mode = self.settings.value("encoder/bitrate_mode", "vbr", type=str).lower()
        idx_br_mode = self.bitrate_mode_combo.findData(br_mode)
        if idx_br_mode < 0:
            idx_br_mode = self.bitrate_mode_combo.findData("vbr")
        if idx_br_mode < 0:
            idx_br_mode = 0
        self.bitrate_mode_combo.setCurrentIndex(idx_br_mode)
        self._on_rate_mode_toggled()
        

        # 8) Load detected HW list (if present)
        hw_json = self.settings.value("encoder/detected_hw_list", "")
        if hw_json:
            try:
                # JSON -> Python set
                arr = json.loads(hw_json)
                self._cached_detected_hw = set(arr)
            except:
                self._cached_detected_hw = None


    # ---------------------------
    # Hardware detection / UI update
    # ---------------------------
    def update_hw_options(self):
        """
        Populates hw_combo based on selected codec (x264/x265) and:
        - If self._cached_detected_hw is not None -> use only those encoders
        - Always include CPU if not present
        """
        container = self.container_combo.currentData()  # "x264" / "x265"

        if self._cached_detected_hw is not None:
            # Already measured -> use only cached entries
            all_hw_encoders = self._cached_detected_hw
        else:
            # Not measured yet -> use theoretically available entries
            # e.g. from ffmpeg -encoders
            # Uses detect_available_hw_encoders() from core/hardware_detect.
            from core.hardware_detect import detect_available_hw_encoders
            all_hw_encoders = detect_available_hw_encoders()  # => z.B. {"CPU","nvidia_h264","amd_h264",...}

        # CPU should always be present; add it if missing
        if "CPU" not in all_hw_encoders:
            # detect_available_hw_encoders may or may not include "CPU"
            all_hw_encoders = set(all_hw_encoders)
            all_hw_encoders.add("CPU")

        # Filter by selected codec
        if container == "x264":
            allowed = {"CPU", "nvidia_h264", "amd_h264", "intel_h264"}
        else:
            allowed = {"CPU", "nvidia_hevc", "amd_hevc", "intel_hevc"}

        final_hw = all_hw_encoders.intersection(allowed)
        if not final_hw:
            final_hw = {"CPU"}

        self.hw_combo.clear()
        sorted_list = sorted(list(final_hw))
        for hw in sorted_list:
            display = _HW_DISPLAY.get(hw, hw)
            self.hw_combo.addItem(display, userData=hw)

        # Restore stored value from QSettings if available
        stored_hw = self.settings.value("encoder/hw", "CPU", type=str)
        # If "none" -> map to "CPU"
        if stored_hw == "none":
            stored_hw = "CPU"

        idx_hw = self.hw_combo.findData(stored_hw)
        if idx_hw < 0:
            idx_hw = 0
        self.hw_combo.setCurrentIndex(idx_hw)


    def on_detect_hw_clicked(self):
        """
        Shows a "please wait" dialog,
        tests common GPU encoders via can_encode_with(),
        stores the result in self._cached_detected_hw + QSettings,
        then calls update_hw_options().
        """
        # 1) Progress dialog
        progress = QProgressDialog("Detecting hardware, please wait...", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setWindowTitle("Please wait...")
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()

        # 2) Test list: ignore libx264 / libx265
        possible_hw_encs = {
            "nvidia_h264": "h264_nvenc",
            "nvidia_hevc": "hevc_nvenc",
            "amd_h264":    "h264_amf",
            "amd_hevc":    "hevc_amf",
            "intel_h264":  "h264_qsv",
            "intel_hevc":  "hevc_qsv",
        }

        working = {"CPU"}  # always include CPU
        
        ffmpeg_exe = shutil.which("ffmpeg") or "ffmpeg"
        for label, ffenc in possible_hw_encs.items():
            # If the user closes/cancels, abort detection loop
            if progress.wasCanceled():
                break
            if can_encode_with(ffenc, ffmpeg_path=ffmpeg_exe, test_duration=0.5):
                working.add(label)

        # 3) Close progress dialog
        progress.close()

        # 4) Save in self._cached_detected_hw
        self._cached_detected_hw = working

        # 5) Also store in QSettings => "encoder/detected_hw_list"
        arr_list = list(working)
        hw_json = json.dumps(arr_list)
        self.settings.setValue("encoder/detected_hw_list", hw_json)

        # 6) User info (including per-codec breakdown,
        # to clarify why hardware dropdown may show only CPU)
        h264_found = sorted(working.intersection(_H264_HW_KEYS))
        hevc_found = sorted(working.intersection(_HEVC_HW_KEYS))

        def _labels(keys):
            return ", ".join(_HW_DISPLAY.get(k, k) for k in keys) if keys else "none"

        current_codec = self.container_combo.currentData() or "x264"
        if current_codec == "x264":
            current_keys = h264_found
            other_keys = hevc_found
            current_name = "H.264 (AVC)"
            other_name = "HEVC (H.265)"
        else:
            current_keys = hevc_found
            other_keys = h264_found
            current_name = "HEVC (H.265)"
            other_name = "H.264 (AVC)"

        msg = (
            "Found working encoders:\n\n"
            f"{current_name}: {_labels(current_keys)}\n"
            f"{other_name}: {_labels(other_keys)}"
        )

        if set(current_keys) <= {"CPU"} and any(k != "CPU" for k in other_keys):
            msg += (
                "\n\nNote: Hardware list is filtered by the selected Codec. "
                f"Switch Codec to {other_name} to use the detected GPU encoder(s)."
            )

        QMessageBox.information(self, "Detect HW", msg)

        # 7) Refresh combo
        self.update_hw_options()

    def on_from_input_clicked(self):
        """Read resolution and fps from the first loaded video and populate fields."""
        # Try to get the first video from the parent MainWindow
        parent = self.parent()
        if not parent or not hasattr(parent, 'playlist') or not parent.playlist:
            QMessageBox.warning(self, "Match Source", "No videos loaded. Please load videos first.")
            return
        
        first_video = parent.playlist[0]
        
        try:
            # Use ffprobe to get video properties (JSON output is most reliable)
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate,bit_rate:format=bit_rate",
                "-of", "json",
                first_video
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            # Parse JSON output
            import json
            data = json.loads(result.stdout)
            
            if not data.get('streams'):
                QMessageBox.warning(self, "Match Source", "Could not find video stream.")
                return
            
            stream = data['streams'][0]
            width = stream.get('width')
            height = stream.get('height')
            r_frame_rate = stream.get('r_frame_rate', '')
            stream_bit_rate = stream.get('bit_rate', None)
            format_bit_rate = (data.get('format') or {}).get('bit_rate', None)
            
            fps = None
            if r_frame_rate and '/' in r_frame_rate:
                try:
                    num, den = r_frame_rate.split('/')
                    fps = float(num) / float(den)
                except (ValueError, ZeroDivisionError):
                    pass

            # Read the input's reported bitrate (bps) and convert to Mbps.
            # Note: ffprobe sometimes omits bit_rate; in that case we keep the default.
            input_bitrate_mbps = None
            bit_rate_raw = stream_bit_rate if stream_bit_rate is not None else format_bit_rate
            if bit_rate_raw is not None:
                try:
                    input_bps = float(bit_rate_raw)
                    input_bitrate_mbps = input_bps / 1_000_000.0
                except (TypeError, ValueError):
                    input_bitrate_mbps = None
            
            if width is None or height is None:
                QMessageBox.warning(self, "Match Source", "Could not read video properties.")
                return
            
            # Ensure the detected resolution exists in the dropdown.
            # If it wasn't part of the defaults, add it and select it.
            target_res = (int(width), int(height))
            found_idx = self._ensure_resolution_option(target_res)
            self.resolution_combo.setCurrentIndex(found_idx)

            # Set bitrate to match input (Mbps), if ffprobe provided it.
            if input_bitrate_mbps is not None:
                bitrate_int = int(round(input_bitrate_mbps))
                bitrate_int = min(max(bitrate_int, 1), 200)  # match QSpinBox range
                self.bitrate_spin.setValue(bitrate_int)
                # If we matched source bitrate, make that mode active as well.
                self.radio_bitrate.setChecked(True)
            
            # Set FPS if available
            if fps:
                fps_clamped = min(max(float(fps), 1.0), 120.0)
                self.fps_spin.setValue(fps_clamped)
            
            QMessageBox.information(
                self, "Match Source",
                "Loaded from source:\n"
                f"Resolution: {width}x{height}\n"
                + (f"Bitrate: {input_bitrate_mbps:.2f} Mbps\n" if input_bitrate_mbps is not None else "")
                + f"FPS: {fps if fps else 'N/A'}"
            )
            
        except Exception as e:
            QMessageBox.warning(self, "Match Source", f"Error reading video: {str(e)}")

    def on_ok_clicked(self):
        """Saves values to QSettings and closes the dialog."""
        # resolution
        w,h = self.resolution_combo.currentData()
        self.settings.setValue("encoder/res_w", w)
        self.settings.setValue("encoder/res_h", h)

        # codec (internal x264 / x265)
        container = self.container_combo.currentData()
        self.settings.setValue("encoder/container", container)

        # hardware => CPU => none
        hw_ui = self.hw_combo.currentData()
        hw_stored = "none" if (hw_ui == "CPU") else hw_ui
        self.settings.setValue("encoder/hw", hw_stored)

        # crf
        self.settings.setValue("encoder/crf", self.crf_spin.value())

        # preset
        preset = self.preset_combo.currentText()
        self.settings.setValue("encoder/preset", preset)
        self.settings.setValue("encoder/rate_control_mode", self._current_rate_mode())
        self.settings.setValue("encoder/bitrate_mode", self.bitrate_mode_combo.currentData())

        # fps
        self.settings.setValue("encoder/fps", self.fps_spin.value())

        xfade_val = self.xfade_spin.value()
        if xfade_val < 1:
            QMessageBox.warning(self, "Invalid X-Fade", "The X-Fade must be >= 1 second.")
            return
        self.settings.setValue("encoder/xfade", xfade_val)
        self.settings.setValue("encoder/bitrate_mbps", self.bitrate_spin.value())

        self.accept()
