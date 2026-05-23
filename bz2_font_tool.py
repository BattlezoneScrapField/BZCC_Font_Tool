#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import glob
import re
import shutil
import time
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QComboBox, 
                             QLineEdit, QFileDialog, QCheckBox, QSlider, 
                             QProgressBar, QMessageBox, QGroupBox, QScrollArea,
                             QSplitter, QGridLayout)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QPixmap, QImage

import freetype
from PIL import Image, ImageFilter, ImageOps, ImageEnhance

# =======================================================================
# CORE PROCESSING LOGIC
# =======================================================================

def sanitize_fontname(font_name: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', '_', font_name).strip().replace(' ', '_')
    return safe or "unknown_font"

def tint_grayscale(img, color):
    if color is None:
        return img
    r,g,b = color
    avg = (r+g+b)//3
    return ImageEnhance.Brightness(img).enhance(avg/255.0)

def render_font_file(face, pixel_size, use_aa, stroke, sharpen, contrast, brightness, font_color, stroke_color, size_offset, letter_spacing):
    final_size = max(1, pixel_size + size_offset)
    face.set_pixel_sizes(0, final_size)
    metrics = face.size
    asc = metrics.ascender >> 6
    desc = abs(metrics.descender >> 6)

    fh = min(255, max(1, asc + desc + (stroke*2)))
    fa = min(255, max(0, asc + stroke))
    fd = min(255, max(0, desc + stroke))

    chars = {}
    bump = {}

    load_flags = freetype.FT_LOAD_RENDER
    if not use_aa:
        load_flags |= freetype.FT_LOAD_TARGET_MONO
    else:
        load_flags |= freetype.FT_LOAD_TARGET_NORMAL | freetype.FT_LOAD_NO_BITMAP

    for code in range(1, 256):
        try:
            face.load_char(code, load_flags)
        except:
            continue

        bm = face.glyph.bitmap
        gm = face.glyph.metrics
        advance = gm.horiAdvance >> 6
        bear_x = gm.horiBearingX >> 6
        bear_y = gm.horiBearingY >> 6
        gw, gh = bm.width, bm.rows

        if advance <= 0 and gw == 0:
            continue

        raw = b""
        if gw > 0 and gh > 0:
            pitch = bm.pitch
            buf = bytes(bm.buffer)
            rows = []
            if bm.pixel_mode == 2:   
                for r in range(gh):
                    rows.append(buf[r*pitch : r*pitch+gw])
            else:                    
                for r in range(gh):
                    row = bytearray(gw)
                    for x in range(gw):
                        byte_idx = r*pitch + x//8
                        if byte_idx < len(buf) and ((buf[byte_idx] >> (7 - x%8)) & 1):
                            row[x] = 255
                    rows.append(bytes(row))
            raw = b"".join(rows)

        if raw:
            img = Image.frombytes("L", (gw,gh), raw)
            if brightness != 1.0:
                img = ImageEnhance.Brightness(img).enhance(brightness)
            if contrast != 1.0:
                img = ImageEnhance.Contrast(img).enhance(contrast)
            if sharpen:
                img = img.filter(ImageFilter.SHARPEN)
            img = tint_grayscale(img, font_color)

            if stroke > 0:
                orig = img.copy()
                img = ImageOps.expand(img, border=stroke, fill=0)
                mask = img.filter(ImageFilter.MaxFilter(size=stroke*2+1))
                mask = tint_grayscale(mask, stroke_color)
                img = mask
                img.paste(orig, (stroke,stroke))
                gw, gh = img.size
                bear_x -= stroke
                bear_y += stroke
                advance += stroke

            raw = img.tobytes()

        left_pad = 0
        ox = bear_x
        if ox < 0:
            left_pad = ox
            ox = 0
        oy = fa - bear_y
        if oy < 0:
            bump[code] = -oy
            oy = 0
        full_w = advance - left_pad if left_pad<0 else advance
        full_w += letter_spacing

        def clamp(v): return min(255, max(0, int(v)))

        chars[code] = {
            'fullWidth': clamp(full_w),
            'rectX0': clamp(ox),
            'rectY0': clamp(oy),
            'rectX1': clamp(ox+gw),
            'rectY1': clamp(oy+gh),
            'charWidth': clamp(gw),
            'charHeight': clamp(gh),
            'charData': raw,
            'leftOffset': max(-128, min(127, left_pad)),
            'kerning': [0]*256
        }

    if bump:
        maxb = max(bump.values())
        fa = min(255, fa+maxb)
        fh = min(255, fh+maxb)
        for code, ch in chars.items():
            b = bump.get(code, maxb)
            ch['rectY0'] = min(255, ch['rectY0']+b)
            ch['rectY1'] = min(255, ch['rectY1']+b)

    return fh, fa, fd, chars

def write_font_file(font_path: str, bm2_path: str, fh: int, fa: int, fd: int, chars: dict):
    items = sorted(chars.items())
    with open(font_path, "wb") as f:
        f.write(b"FONT")
        f.write(bytes([len(items), fh, fa, fd]))
        for cv, cd in items:
            f.write(bytes([cv, cd['fullWidth'], cd['rectX0'], cd['rectY0'], cd['rectX1'], cd['rectY1']]))
        for cv, cd in items:
            f.write(bytes([cd['charWidth'], cd['charHeight']]))
            f.write(cd['charData'])

    with open(bm2_path, "wb") as f:
        offs = bytearray(256)
        for cv, cd in items:
            offs[cv] = cd['leftOffset'] & 0xFF
        f.write(offs)
        kern = bytearray(256*256)
        for cv, cd in items:
            base = cv*256
            for j, k in enumerate(cd['kerning']):
                kern[base+j] = k & 0xFF
        f.write(kern)

def size_from_filename(path: str):
    stem = os.path.splitext(os.path.basename(path))[0]
    parts = stem.rsplit('_',1)
    if len(parts)==2:
        try:
            return int(parts[1])
        except:
            pass
    return None

# =======================================================================
# WORKER THREAD
# =======================================================================
class ProcessWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    finished = pyqtSignal(int, int) 

    def __init__(self, target_folder, backup_folder, font_path, settings):
        super().__init__()
        self.target_folder = target_folder
        self.backup_folder = backup_folder
        self.font_path = font_path
        self.settings = settings
        self.is_running = True

    def run(self):
        try:
            face = freetype.Face(self.font_path)
            family = face.family_name.decode(errors='replace')
            font_safe = sanitize_fontname(family)
        except Exception as e:
            self.log.emit(f"Error loading font: {e}")
            self.finished.emit(0, 1)
            return

        total_converted = 0
        total_errors = 0

        source_path = os.path.abspath(self.target_folder)
        base_name = os.path.basename(source_path)
        
        # 1. Handle Backup if provided
        if self.backup_folder and os.path.isdir(self.backup_folder):
            timestamp = int(time.time())
            backup_target = os.path.join(os.path.abspath(self.backup_folder), f"{base_name}_backup_{font_safe}_{timestamp}")
            try:
                shutil.copytree(source_path, backup_target)
                self.log.emit(f"Backup saved to: {backup_target}")
            except Exception as e:
                self.log.emit(f"BACKUP FAILED: {e}. Aborting.")
                self.finished.emit(0, 1)
                return
        else:
            self.log.emit("NO BACKUP FOLDER. Overwriting live files blindly!")

        # 2. In-place Processing
        self.log.emit(f"Processing in-place: {source_path}")
        
        font_files = []
        for root, _, files in os.walk(source_path):
            for f in files:
                low = f.lower()
                if low.endswith(('.bmf', '.fon')):
                    full = os.path.join(root, f)
                    font_files.append(full)

        valid = [(full, size_from_filename(full)) for full in font_files if size_from_filename(full) is not None]
        
        if not valid:
            self.log.emit(f"No valid .bmf/.fon files found in {source_path}")
            self.finished.emit(0, 0)
            return

        cache = {}
        for i, (full_path, psize) in enumerate(valid):
            if not self.is_running:
                break
                
            try:
                if psize not in cache:
                    cache[psize] = render_font_file(
                        face, psize, 
                        self.settings['use_aa'], 
                        self.settings['stroke'],
                        self.settings['sharpen'], 
                        self.settings['contrast'], 
                        self.settings['brightness'],
                        None, None, 
                        self.settings['size_offset'],
                        self.settings['letter_spacing']
                    )
                fh, fa, fd, chars = cache[psize]

                out_bm2 = os.path.splitext(full_path)[0] + ".bm2"

                write_font_file(full_path, out_bm2, fh, fa, fd, chars)
                total_converted += 1
            except Exception as e:
                self.log.emit(f"Error on {full_path}: {e}")
                total_errors += 1
            
            self.progress.emit(int(((i + 1) / len(valid)) * 100))

        self.finished.emit(total_converted, total_errors)

# =======================================================================
# GUI APPLICATION
# =======================================================================
class FontToolApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BZCC Font Dominator - Hardcore Edition")
        self.setMinimumSize(850, 750)
        self.resize(1000, 900) 
        
        self.font_files = {} 
        self.worker = None
        self.app_settings = QSettings("VAC_Dominator", "BZCC_Font_Tool_Hardcore")

        self.init_ui()
        self.refresh_fonts()
        self.load_settings()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        self.splitter = QSplitter(Qt.Vertical)
        
        # --- TOP SECTION (Controls) ---
        top_container = QWidget()
        top_layout = QVBoxLayout(top_container)
        top_layout.setContentsMargins(0, 0, 0, 0)

        # 1. Directories Section
        folder_group = QGroupBox("Target & Backup Directories")
        folder_layout = QVBoxLayout(folder_group)
        
        # Target Interface Row
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Live Target (Will Overwrite):"))
        self.input_folder = QLineEdit()
        self.input_folder.setReadOnly(True)
        self.input_folder.setPlaceholderText("Select the target 'interface' folder...")
        target_row.addWidget(self.input_folder)
        btn_browse_folder = QPushButton("Browse...")
        btn_browse_folder.clicked.connect(self.browse_folder)
        target_row.addWidget(btn_browse_folder)
        folder_layout.addLayout(target_row)

        # Backup Row
        backup_row = QHBoxLayout()
        backup_row.addWidget(QLabel("Backup Folder (Safety net):"))
        self.input_backup = QLineEdit()
        self.input_backup.setReadOnly(True)
        self.input_backup.setPlaceholderText("Leave empty if you are feeling lucky...")
        backup_row.addWidget(self.input_backup)
        btn_browse_backup = QPushButton("Browse...")
        btn_browse_backup.clicked.connect(self.browse_backup)
        backup_row.addWidget(btn_browse_backup)
        btn_clear_backup = QPushButton("Clear")
        btn_clear_backup.clicked.connect(lambda: self.input_backup.clear())
        backup_row.addWidget(btn_clear_backup)
        folder_layout.addLayout(backup_row)
        
        top_layout.addWidget(folder_group)

        # 2. Font Selection Settings
        font_group = QGroupBox("Font Setup")
        font_layout = QVBoxLayout(font_group)
        
        combo_layout = QHBoxLayout()
        self.combo_fonts = QComboBox()
        self.combo_fonts.currentIndexChanged.connect(self.update_preview)
        combo_layout.addWidget(self.combo_fonts, stretch=2)
        
        btn_refresh_fonts = QPushButton("Refresh")
        btn_refresh_fonts.clicked.connect(self.refresh_fonts)
        combo_layout.addWidget(btn_refresh_fonts)
        
        btn_browse_font = QPushButton("Custom TTF...")
        btn_browse_font.clicked.connect(self.browse_custom_font)
        combo_layout.addWidget(btn_browse_font)

        combo_layout.addWidget(QLabel("UI Scale:"))
        self.combo_scale = QComboBox()
        self.combo_scale.addItems(["x1.0", "x1.5", "x2.0", "x2.5", "x3.0", "x3.5", "x4.0", "x4.5", "x5.0"])
        self.combo_scale.currentIndexChanged.connect(self.update_preview)
        combo_layout.addWidget(self.combo_scale)
        
        font_layout.addLayout(combo_layout)

        text_layout = QHBoxLayout()
        text_layout.addWidget(QLabel("Preview Text:"))
        self.input_preview_text = QLineEdit()
        self.input_preview_text.setText("Battlezone: Combat Commander is a hybrid tank shooter, first-person shooter and real-time strategy video game.")
        self.input_preview_text.textChanged.connect(self.update_preview)
        text_layout.addWidget(self.input_preview_text)
        font_layout.addLayout(text_layout)
        
        top_layout.addWidget(font_group)

        # 3. Photoshop Settings Section
        settings_group = QGroupBox("Photoshop Engine (Rendering Settings)")
        settings_layout = QVBoxLayout(settings_group)
        
        check_layout = QHBoxLayout()
        self.chk_aa = QCheckBox("Enable Anti-Aliasing")
        self.chk_aa.setChecked(False)
        self.chk_aa.stateChanged.connect(self.update_preview)
        
        self.chk_sharpen = QCheckBox("Enable Sharpening")
        self.chk_sharpen.setChecked(True)
        self.chk_sharpen.stateChanged.connect(self.update_preview)
        check_layout.addWidget(self.chk_aa)
        check_layout.addWidget(self.chk_sharpen)
        settings_layout.addLayout(check_layout)

        grid_layout = QGridLayout()
        
        self.slider_stroke = self.create_grid_slider(grid_layout, 0, "Stroke Width", 0, 5, 0)
        self.slider_offset = self.create_grid_slider(grid_layout, 1, "Size Offset", -10, 10, 4)
        self.slider_spacing = self.create_grid_slider(grid_layout, 2, "Letter Spacing", -10, 20, 0)
        
        self.slider_contrast = self.create_grid_slider(grid_layout, 3, "Contrast (x0.1)", 1, 30, 20)
        self.slider_brightness = self.create_grid_slider(grid_layout, 4, "Brightness (x0.1)", 1, 30, 10)
        
        settings_layout.addLayout(grid_layout)
        top_layout.addWidget(settings_group)

        # 4. Action Section
        action_layout = QVBoxLayout()
        self.lbl_status = QLabel("Ready to deploy.")
        action_layout.addWidget(self.lbl_status)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        action_layout.addWidget(self.progress_bar)
        
        self.btn_process = QPushButton("EXECUTE OVERWRITE (PROCESS)")
        self.btn_process.setStyleSheet("background-color: #8b0000; color: white; font-weight: bold; font-size: 16px; padding: 15px;")
        self.btn_process.clicked.connect(self.start_processing)
        action_layout.addWidget(self.btn_process)
        
        top_layout.addLayout(action_layout)

        self.splitter.addWidget(top_container)

        # --- BOTTOM SECTION (Preview Area) ---
        bottom_container = QWidget()
        bottom_layout = QVBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 10, 0, 0)
        
        preview_label_title = QLabel("LIVE PREVIEW (FULL RENDER PIPELINE)")
        preview_label_title.setStyleSheet("font-weight: bold; color: #aaa;")
        bottom_layout.addWidget(preview_label_title)

        self.scroll_preview = QScrollArea()
        self.scroll_preview.setWidgetResizable(True)
        self.scroll_preview.setStyleSheet("background-color: #1a1a1a; border: 1px solid #444;")
        
        self.lbl_preview = QLabel("PREVIEW")
        self.lbl_preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lbl_preview.setStyleSheet("color: white; padding: 10px;")
        self.scroll_preview.setWidget(self.lbl_preview)
        
        bottom_layout.addWidget(self.scroll_preview)
        
        self.splitter.addWidget(bottom_container)
        self.splitter.setSizes([600, 300])
        
        main_layout.addWidget(self.splitter)

    def create_grid_slider(self, grid, row, label_text, min_val, max_val, default_val):
        lbl = QLabel(label_text)
        val_lbl = QLabel(str(default_val))
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(min_val)
        slider.setMaximum(max_val)
        slider.setValue(default_val)
        slider.valueChanged.connect(lambda v: val_lbl.setText(str(v)))
        slider.valueChanged.connect(self.update_preview)
        
        grid.addWidget(lbl, row, 0)
        grid.addWidget(slider, row, 1)
        grid.addWidget(val_lbl, row, 2)
        return slider

    def load_settings(self):
        def _get_int(val, default):
            try: return int(val)
            except: return default
            
        def _get_bool(val, default):
            if val is None: return default
            if isinstance(val, str): return val.lower() == 'true'
            return bool(val)

        self.input_folder.setText(self.app_settings.value("folder", ""))
        self.input_backup.setText(self.app_settings.value("backup_folder", ""))
        self.combo_scale.setCurrentIndex(_get_int(self.app_settings.value("scale_idx"), 0))
        
        saved_text = self.app_settings.value("preview_text")
        if saved_text:
            self.input_preview_text.setText(saved_text)
            
        self.chk_aa.setChecked(_get_bool(self.app_settings.value("use_aa"), False))
        self.chk_sharpen.setChecked(_get_bool(self.app_settings.value("sharpen"), True))
        
        self.slider_stroke.setValue(_get_int(self.app_settings.value("stroke"), 0))
        self.slider_offset.setValue(_get_int(self.app_settings.value("offset"), 4))
        self.slider_spacing.setValue(_get_int(self.app_settings.value("spacing"), 0))
        self.slider_contrast.setValue(_get_int(self.app_settings.value("contrast"), 20))
        self.slider_brightness.setValue(_get_int(self.app_settings.value("brightness"), 10))

    def closeEvent(self, event):
        self.app_settings.setValue("folder", self.input_folder.text())
        self.app_settings.setValue("backup_folder", self.input_backup.text())
        self.app_settings.setValue("scale_idx", self.combo_scale.currentIndex())
        self.app_settings.setValue("preview_text", self.input_preview_text.text())
        self.app_settings.setValue("use_aa", self.chk_aa.isChecked())
        self.app_settings.setValue("sharpen", self.chk_sharpen.isChecked())
        self.app_settings.setValue("stroke", self.slider_stroke.value())
        self.app_settings.setValue("offset", self.slider_offset.value())
        self.app_settings.setValue("spacing", self.slider_spacing.value())
        self.app_settings.setValue("contrast", self.slider_contrast.value())
        self.app_settings.setValue("brightness", self.slider_brightness.value())
        event.accept()

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select TARGET 'interface' Folder")
        if folder:
            self.input_folder.setText(folder)

    def browse_backup(self):
        folder = QFileDialog.getExistingDirectory(self, "Select BACKUP Directory")
        if folder:
            self.input_backup.setText(folder)

    def refresh_fonts(self):
        self.combo_fonts.blockSignals(True)
        self.combo_fonts.clear()
        self.font_files.clear()
        
        font_dirs = []
        if sys.platform == "win32":
            font_dirs.append(os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"))
            
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                font_dirs.append(os.path.join(local_appdata, "Microsoft", "Windows", "Fonts"))
        
        font_dirs.append(os.getcwd())

        valid_exts = ('.ttf', '.otf', '.ttc', '.dfont', '.pfb', '.pfa', '.fnt', '.fon')
        
        for d in font_dirs:
            if not os.path.exists(d): continue
            for f in os.listdir(d):
                if f.lower().endswith(valid_exts):
                    path = os.path.join(d, f)
                    try:
                        face = freetype.Face(path)
                        family = face.family_name.decode(errors='replace')
                        style = face.style_name.decode(errors='replace') if face.style_name else ""
                        
                        if style and style not in family:
                            display_name = f"{family} {style}".strip()
                        else:
                            display_name = family
                            
                        if display_name not in self.font_files:
                            self.font_files[display_name] = path
                            self.combo_fonts.addItem(display_name)
                    except:
                        pass
                        
        self.combo_fonts.model().sort(0, Qt.AscendingOrder)
        self.combo_fonts.blockSignals(False)
        self.update_preview()

    def browse_custom_font(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select Font File", "", "Fonts (*.ttf *.otf *.ttc *.fon *.fnt)")
        if file:
            try:
                face = freetype.Face(file)
                family = face.family_name.decode(errors='replace')
                style = face.style_name.decode(errors='replace') if face.style_name else ""
                
                if style and style not in family:
                    base_name = f"{family} {style}".strip()
                else:
                    base_name = family
                    
                name_with_custom = f"[Custom] {base_name}"
                self.font_files[name_with_custom] = file
                self.combo_fonts.addItem(name_with_custom)
                self.combo_fonts.setCurrentText(name_with_custom)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Bad font file:\n{e}")

    def update_preview(self):
        display_name = self.combo_fonts.currentText()
        if not display_name: return
        
        path = self.font_files.get(display_name)
        if not path: return
        
        scale_idx = self.combo_scale.currentIndex()
        if scale_idx < 0: return

        tiny_sizes = [6, 10, 14, 18, 22, 26, 30, 34, 38]
        system_sizes = [8, 12, 16, 20, 24, 28, 32, 36, 40]
        small_sizes = [8, 12, 16, 20, 24, 28, 32, 36, 40]
        medium_sizes = [10, 15, 20, 25, 30, 35, 40, 45, 50]
        large_sizes = [12, 18, 24, 30, 36, 42, 48, 54, 60]

        target_sizes = [
            ("TINY", tiny_sizes[scale_idx]),
            ("SYSTEM", system_sizes[scale_idx]),
            ("SMALL", small_sizes[scale_idx]),
            ("MEDIUM", medium_sizes[scale_idx]),
            ("LARGE", large_sizes[scale_idx])
        ]

        text = self.input_preview_text.text()
        if not text:
            text = "Enter some text above..."

        offset = self.slider_offset.value()
        stroke = self.slider_stroke.value()
        spacing = self.slider_spacing.value()
        contrast = self.slider_contrast.value() / 10.0
        brightness = self.slider_brightness.value() / 10.0
        
        use_aa = self.chk_aa.isChecked()
        sharpen = self.chk_sharpen.isChecked()
        load_flags = freetype.FT_LOAD_RENDER | (freetype.FT_LOAD_TARGET_NORMAL if use_aa else freetype.FT_LOAD_TARGET_MONO)

        try:
            face = freetype.Face(path)
            
            img_w, img_h = 4000, 4000 
            img = Image.new("L", (img_w, img_h), 26) 
            
            current_y = 20
            max_x_used = 0
            
            for label, base_size in target_sizes:
                render_size = max(1, base_size + offset)
                face.set_pixel_sizes(0, render_size)
                
                pen_x = 20
                pen_y_title = current_y + render_size
                title_str = f"{label} ({base_size}):"
                max_h_title = 0
                
                for char in title_str:
                    try:
                        face.load_char(char, load_flags)
                        bm = face.glyph.bitmap
                        gw, gh = bm.width, bm.rows
                        if gw > 0 and gh > 0:
                            pitch = bm.pitch
                            buf = bytes(bm.buffer)
                            rows = []
                            if bm.pixel_mode == 2:   
                                for r in range(gh): rows.append(buf[r*pitch : r*pitch+gw])
                            else:                    
                                for r in range(gh):
                                    row = bytearray(gw)
                                    for x in range(gw):
                                        byte_idx = r*pitch + x//8
                                        if byte_idx < len(buf) and ((buf[byte_idx] >> (7 - x%8)) & 1): row[x] = 255
                                    rows.append(bytes(row))
                            raw = b"".join(rows)
                            
                            char_mask = Image.frombytes("L", (gw, gh), raw)
                            title_fill = Image.new("L", (gw, gh), 180) 
                            img.paste(title_fill, (pen_x + face.glyph.bitmap_left, pen_y_title - face.glyph.bitmap_top), char_mask)
                            max_h_title = max(max_h_title, gh)
                        
                        pen_x += face.glyph.advance.x >> 6
                        max_x_used = max(max_x_used, pen_x)
                    except Exception:
                        continue 
                
                pen_x = 20
                pen_y_text = pen_y_title + max(render_size, max_h_title) + 5
                max_h_text = 0
                
                for char in text:
                    try:
                        face.load_char(char, load_flags)
                        bm = face.glyph.bitmap
                        gw, gh = bm.width, bm.rows
                        
                        adv_x = face.glyph.advance.x >> 6
                        bear_x = face.glyph.bitmap_left
                        bear_y = face.glyph.bitmap_top
                        
                        if gw > 0 and gh > 0:
                            pitch = bm.pitch
                            buf = bytes(bm.buffer)
                            rows = []
                            if bm.pixel_mode == 2:   
                                for r in range(gh): rows.append(buf[r*pitch : r*pitch+gw])
                            else:                    
                                for r in range(gh):
                                    row = bytearray(gw)
                                    for x in range(gw):
                                        byte_idx = r*pitch + x//8
                                        if byte_idx < len(buf) and ((buf[byte_idx] >> (7 - x%8)) & 1): row[x] = 255
                                    rows.append(bytes(row))
                            raw = b"".join(rows)
                            
                            char_mask = Image.frombytes("L", (gw, gh), raw)
                            
                            if brightness != 1.0:
                                char_mask = ImageEnhance.Brightness(char_mask).enhance(brightness)
                            if contrast != 1.0:
                                char_mask = ImageEnhance.Contrast(char_mask).enhance(contrast)
                            if sharpen:
                                char_mask = char_mask.filter(ImageFilter.SHARPEN)
                                
                            if stroke > 0:
                                orig = char_mask.copy()
                                char_mask = ImageOps.expand(char_mask, border=stroke, fill=0)
                                stroke_mask = char_mask.filter(ImageFilter.MaxFilter(size=stroke*2+1))
                                char_mask = stroke_mask
                                char_mask.paste(orig, (stroke, stroke))
                                
                                bear_x -= stroke
                                bear_y += stroke
                                adv_x += stroke

                            white_fill = Image.new("L", char_mask.size, 255)
                            img.paste(white_fill, (pen_x + bear_x, pen_y_text - bear_y), char_mask)
                            max_h_text = max(max_h_text, char_mask.height)
                        
                        pen_x += adv_x + spacing 
                        max_x_used = max(max_x_used, pen_x)
                    except Exception:
                        continue 
                
                current_y = pen_y_text + max(render_size, max_h_text) + 40

            img = img.crop((0, 0, min(max_x_used + 50, img_w), min(current_y, img_h)))
            
            img = img.convert("RGBA")
            data = img.tobytes("raw", "RGBA")
            qim = QImage(data, img.width, img.height, QImage.Format_RGBA8888)
            self.lbl_preview.setPixmap(QPixmap.fromImage(qim))
            
        except Exception as e:
            self.lbl_preview.setText(f"PREVIEW ERROR: {e}")

    def start_processing(self):
        folder = self.input_folder.text()
        if not folder or not os.path.exists(folder):
            QMessageBox.warning(self, "Hold up", "Select a valid target 'interface' folder.")
            return
            
        display_name = self.combo_fonts.currentText()
        if not display_name:
            return
            
        font_path = self.font_files[display_name]
        
        settings = {
            'use_aa': self.chk_aa.isChecked(),
            'sharpen': self.chk_sharpen.isChecked(),
            'stroke': self.slider_stroke.value(),
            'size_offset': self.slider_offset.value(),
            'letter_spacing': self.slider_spacing.value(),
            'contrast': self.slider_contrast.value() / 10.0, 
            'brightness': self.slider_brightness.value() / 10.0
        }

        self.btn_process.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_status.setText("Working...")

        self.worker = ProcessWorker(folder, self.input_backup.text(), font_path, settings)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log.connect(self.lbl_status.setText)
        self.worker.finished.connect(self.on_processing_finished)
        self.worker.start()

    def on_processing_finished(self, conv, errs):
        self.btn_process.setEnabled(True)
        self.progress_bar.setValue(100)
        if errs == 0:
            self.lbl_status.setText(f"Done flawlessly. {conv} files overwritten in place.")
        else:
            self.lbl_status.setText(f"Finished. {conv} converted, but {errs} errors happened.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FontToolApp()
    window.show()
    sys.exit(app.exec_())