#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import re
import shutil
import time
import copy
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QComboBox,
                             QLineEdit, QFileDialog, QCheckBox, QSlider,
                             QProgressBar, QMessageBox, QGroupBox, QScrollArea,
                             QGridLayout, QDockWidget, QStyle, QStyleOptionSlider)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer
from PyQt5.QtGui import QFont, QPixmap, QImage, QPainter, QColor
import freetype
from PIL import Image, ImageFilter, ImageOps, ImageEnhance

# =======================================================================
# NATIVE BMF PARSER
# =======================================================================
class DummyMetrics: pass
class DummyBitmap: pass
class DummyGlyph: pass
class DummySize: pass

class BmfFace:
    def __init__(self, path):
        self.path = path
        self.is_scalable = True
        self.family_name = os.path.basename(path).encode('utf-8')
        self.style_name = b""
        self.size = DummySize()
        self.glyph = DummyGlyph()
        self.glyph.bitmap = DummyBitmap()
        self.glyph.metrics = DummyMetrics()
        self.glyph.advance = DummyMetrics()
        
        with open(path, 'rb') as f:
            data = f.read()
        
        offset = 0
        self.fontIdent = data[offset:offset+4]
        offset += 4
        self.numChar = data[offset]
        offset += 1
        
        self.base_fontHeight = data[offset]
        offset += 1
        self.base_fontAscent = data[offset]
        offset += 1
        self.base_fontDescent = data[offset]
        offset += 1
        
        self.chars = {}
        for i in range(self.numChar):
            code = data[offset]
            fullWidth = data[offset+1]
            rectX0 = data[offset+2]
            rectY0 = data[offset+3]
            rectX1 = data[offset+4]
            rectY1 = data[offset+5]
            offset += 6
            self.chars[code] = {
                'fullWidth': fullWidth,
                'rectX0': rectX0, 'rectY0': rectY0,
                'rectX1': rectX1, 'rectY1': rectY1
            }
            
        for i in range(self.numChar):
            cw = data[offset]
            ch = data[offset+1]
            offset += 2
            pixels = data[offset:offset + cw * ch]
            offset += cw * ch
            code = list(self.chars.keys())[i]
            self.chars[code]['charWidth'] = cw
            self.chars[code]['charHeight'] = ch
            self.chars[code]['charData'] = pixels
            
        match = re.search(r'_(\d+)\.bmf$', os.path.basename(path), re.IGNORECASE)
        if match:
            self.base_pixel_size = int(match.group(1))
        else:
            self.base_pixel_size = 16
            
        self.current_scale = 1.0

    def set_pixel_sizes(self, w, h):
        self.current_scale = float(h) / float(self.base_pixel_size)
        self.size.ascender = int(self.base_fontAscent * self.current_scale) << 6
        self.size.descender = int(-self.base_fontDescent * self.current_scale) << 6
        
    def load_char(self, code, flags):
        if code not in self.chars:
            raise Exception("Char not found")
            
        ch = self.chars[code]
        cw = int(ch['charWidth'] * self.current_scale)
        ch_h = int(ch['charHeight'] * self.current_scale)
        
        if cw > 0 and ch_h > 0 and ch['charWidth'] > 0 and ch['charHeight'] > 0:
            img = Image.frombytes('L', (ch['charWidth'], ch['charHeight']), ch['charData'])
            img_scaled = img.resize((cw, ch_h), Image.Resampling.LANCZOS)
            self.glyph.bitmap.buffer = img_scaled.tobytes()
        else:
            self.glyph.bitmap.buffer = b""
            
        self.glyph.bitmap.width = cw
        self.glyph.bitmap.rows = ch_h
        self.glyph.bitmap.pitch = cw
        self.glyph.bitmap.pixel_mode = 2
        
        adv = int(ch['fullWidth'] * self.current_scale)
        self.glyph.metrics.horiAdvance = adv << 6
        self.glyph.advance.x = adv << 6
        
        bx = int(ch['rectX0'] * self.current_scale)
        self.glyph.metrics.horiBearingX = bx << 6
        self.glyph.bitmap_left = bx
        
        scaled_ascent = int(self.base_fontAscent * self.current_scale)
        by = scaled_ascent - int(ch['rectY0'] * self.current_scale)
        self.glyph.metrics.horiBearingY = by << 6
        self.glyph.bitmap_top = by

# =======================================================================
# ADVANCED CUSTOM WIDGETS
# =======================================================================
class JumpSlider(QSlider):
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            val = self.pixelPosToRangeValue(event.pos())
            self.setValue(val)

    def pixelPosToRangeValue(self, pos):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        gr = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        sr = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        if self.orientation() == Qt.Horizontal:
            sliderLength = sr.width()
            sliderMin = gr.x()
            sliderMax = gr.right() - sliderLength + 1
            p = pos.x() - sliderLength / 2
        else:
            sliderLength = sr.height()
            sliderMin = gr.y()
            sliderMax = gr.bottom() - sliderLength + 1
            p = pos.y() - sliderLength / 2
        if sliderMax <= sliderMin:
            return self.minimum()
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(p), sliderMax - sliderMin, opt.upsideDown)

# =======================================================================
# CORE PROCESSING LOGIC
# =======================================================================
def sanitize_fontname(font_name: str) -> str:
    safe = re.sub(r'[\\/*?:"<>|]', '_', font_name).strip().replace(' ', '_')
    return safe or "unknown_font"

def tint_grayscale(img, color):
    if color is None:
        return img
    try:
        r, g, b = color
        avg = (r + g + b) // 3
        return ImageEnhance.Brightness(img).enhance(avg / 255.0)
    except (ValueError, TypeError):
        return img

def set_safe_pixel_size(face, target_size):
    target_size = max(1, int(target_size))
    if hasattr(face, 'is_scalable') and face.is_scalable:
        try:
            face.set_pixel_sizes(0, target_size)
            return
        except Exception:
            pass
    if hasattr(face, 'available_sizes') and face.available_sizes:
        closest_idx = 0
        closest_diff = float('inf')
        for idx, sz in enumerate(face.available_sizes):
            h = getattr(sz, 'height', target_size)
            diff = abs(h - target_size)
            if diff < closest_diff:
                closest_diff = diff
                closest_idx = idx
        try:
            face.select_size(closest_idx)
            return
        except Exception:
            pass
    try:
        face.set_pixel_sizes(0, target_size)
    except Exception:
        pass

def load_face(path):
    if path.lower().endswith('.bmf'):
        return BmfFace(path)
    else:
        return freetype.Face(path)

def render_font_file(face, pixel_size, config):
    use_aa = config.get('use_aa', False)
    stroke = config.get('stroke', 0)
    sharpen = config.get('sharpen', True)
    contrast = config.get('contrast', 1.0)
    brightness = config.get('brightness', 1.0)
    size_offset = config.get('size_offset', 0)
    letter_spacing = config.get('letter_spacing', 0)
    font_color = config.get('font_color', None)
    stroke_color = config.get('stroke_color', None)
    
    use_shadow = config.get('shadow', False)
    shadow_x = config.get('shadow_x', 2)
    shadow_y = config.get('shadow_y', 2)
    
    final_size = max(1, pixel_size + size_offset + 10)
    set_safe_pixel_size(face, final_size)
    
    metrics = face.size
    asc = metrics.ascender >> 6
    desc = abs(metrics.descender >> 6)
    fh = min(255, max(1, asc + desc + (stroke * 2) + (abs(shadow_y) if use_shadow else 0)))
    fa = min(255, max(0, asc + stroke + (max(0, -shadow_y) if use_shadow else 0)))
    fd = min(255, max(0, desc + stroke + (max(0, shadow_y) if use_shadow else 0)))
    
    chars = {}
    bump = {}
    load_flags = freetype.FT_LOAD_RENDER
    if not use_aa and not isinstance(face, BmfFace):
        load_flags |= freetype.FT_LOAD_TARGET_MONO
    elif not isinstance(face, BmfFace):
        load_flags |= freetype.FT_LOAD_TARGET_NORMAL | freetype.FT_LOAD_NO_BITMAP
        
    for code in range(1, 256):
        try:
            face.load_char(code, load_flags)
        except Exception:
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
                    rows.append(buf[r * pitch : r * pitch + gw])
            else:
                for r in range(gh):
                    row = bytearray(gw)
                    for x in range(gw):
                        byte_idx = r * pitch + x // 8
                        if byte_idx < len(buf) and ((buf[byte_idx] >> (7 - x % 8)) & 1):
                            row[x] = 255
                    rows.append(bytes(row))
            raw = b"".join(rows)
            
            if raw:
                img = Image.frombytes("L", (gw, gh), raw)
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
                    mask = img.filter(ImageFilter.MaxFilter(size=stroke * 2 + 1))
                    mask = tint_grayscale(mask, stroke_color)
                    img = mask
                    img.paste(orig, (stroke, stroke))
                    orig.close()
                    gw, gh = img.size
                    bear_x -= stroke
                    bear_y += stroke
                    advance += stroke
                    
                if use_shadow:
                    orig = img.copy()
                    sx = max(0, shadow_x)
                    sy = max(0, shadow_y)
                    neg_sx = max(0, -shadow_x)
                    neg_sy = max(0, -shadow_y)
                    new_w = gw + abs(shadow_x)
                    new_h = gh + abs(shadow_y)
                    img = Image.new("L", (new_w, new_h), 0)
                    
                    # Shadow layer (dimmed)
                    shadow_layer = orig.point(lambda p: p * 0.4)
                    img.paste(shadow_layer, (sx, sy))
                    
                    # Front layer
                    img.paste(orig, (neg_sx, neg_sy), orig)
                    orig.close()
                    gw, gh = img.size
                    bear_x -= neg_sx
                    bear_y += neg_sy
                    advance += max(0, shadow_x)

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
                    
                full_w = advance - left_pad if left_pad < 0 else advance
                full_w += letter_spacing
                
                def clamp(v): return min(255, max(0, int(v)))
                chars[code] = {
                    'fullWidth': clamp(full_w),
                    'rectX0': clamp(ox),
                    'rectY0': clamp(oy),
                    'rectX1': clamp(ox + gw),
                    'rectY1': clamp(oy + gh),
                    'charWidth': clamp(gw),
                    'charHeight': clamp(gh),
                    'charData': raw,
                    'leftOffset': max(-128, min(127, left_pad)),
                    'kerning': [0] * 256
                }
                
    if bump:
        maxb = max(bump.values())
        fa = min(255, fa + maxb)
        fh = min(255, fh + maxb)
        for code, ch in chars.items():
            b = bump.get(code, maxb)
            ch['rectY0'] = min(255, ch['rectY0'] + b)
            ch['rectY1'] = min(255, ch['rectY1'] + b)
            
    return fh, fa, fd, chars

def write_font_file(font_path: str, bm2_path: str, fh: int, fa: int, fd: int, chars: dict):
    items = sorted(chars.items())
    try:
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
            kern = bytearray(256 * 256)
            for cv, cd in items:
                base = cv * 256
                for j, k in enumerate(cd['kerning']):
                    kern[base + j] = k & 0xFF
            f.write(kern)
    except Exception as e:
        raise IOError(f"Failed to write to {font_path} or {bm2_path}: {e}")

def size_from_filename(path: str):
    stem = os.path.splitext(os.path.basename(path))[0]
    parts = stem.rsplit('_', 1)
    if len(parts) == 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return None

# =======================================================================
# WORKER THREAD
# =======================================================================
class ProcessWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    finished = pyqtSignal(int, int)
    
    def __init__(self, target_folder, backup_folder, font_path, scale_idx, profiles):
        super().__init__()
        self.target_folder = target_folder
        self.backup_folder = backup_folder
        self.font_path = font_path
        self.scale_idx = scale_idx
        self.profiles = profiles
        self.is_running = True
        
    def run(self):
        try:
            face = load_face(self.font_path)
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
        
        if self.backup_folder and os.path.isdir(self.backup_folder):
            timestamp = int(time.time())
            backup_target = os.path.join(os.path.abspath(self.backup_folder), f"{base_name}_backup_{font_safe}_{timestamp}")
            try:
                shutil.copytree(source_path, backup_target)
                self.log.emit(f"Backup created: {backup_target}")
            except Exception as e:
                self.log.emit(f"CRITICAL BACKUP FAILURE: {e}. Execution Aborted.")
                self.finished.emit(0, 1)
                return
        else:
            self.log.emit("No backup folder set. Applying live mutations.")
            self.log.emit(f"Processing in-place: {source_path}")
            
        font_files = []
        for root, _, files in os.walk(source_path):
            for f in files:
                if f.lower().endswith(('.bmf', '.fon')):
                    font_files.append(os.path.join(root, f))
                    
        valid = [(full, size_from_filename(full)) for full in font_files if size_from_filename(full) is not None]
        if not valid:
            self.log.emit(f"No matching fonts found in {source_path}")
            self.finished.emit(0, 0)
            return
            
        tiny_sizes = [6, 10, 14, 18, 22, 26, 30, 34, 38]
        system_sizes = [8, 12, 16, 20, 24, 28, 32, 36, 40]
        small_sizes = [8, 12, 16, 20, 24, 28, 32, 36, 40]
        medium_sizes = [10, 15, 20, 25, 30, 35, 40, 45, 50]
        large_sizes = [12, 18, 24, 30, 36, 42, 48, 54, 60]
        
        scale_idx = max(0, min(self.scale_idx, len(tiny_sizes) - 1))
        t_sz = tiny_sizes[scale_idx]
        sys_sz = system_sizes[scale_idx]
        sm_sz = small_sizes[scale_idx]
        med_sz = medium_sizes[scale_idx]
        lg_sz = large_sizes[scale_idx]
        
        for i, (full_path, psize) in enumerate(valid):
            if not self.is_running:
                break
            try:
                if psize == t_sz: profile_name = 'Tiny'
                elif psize == sys_sz: profile_name = 'System'
                elif psize == sm_sz: profile_name = 'Small'
                elif psize == med_sz: profile_name = 'Medium'
                elif psize == lg_sz: profile_name = 'Large'
                else:
                    diffs = {
                        'Tiny': abs(psize - t_sz),
                        'System': abs(psize - sys_sz),
                        'Small': abs(psize - sm_sz),
                        'Medium': abs(psize - med_sz),
                        'Large': abs(psize - lg_sz)
                    }
                    profile_name = min(diffs, key=diffs.get)
                    
                conf = self.profiles.get(profile_name, self.profiles.get('System', {}))
                fh, fa, fd, chars = render_font_file(face, psize, conf)
                out_bm2 = os.path.splitext(full_path)[0] + ".bm2"
                write_font_file(full_path, out_bm2, fh, fa, fd, chars)
                total_converted += 1
            except Exception as e:
                self.log.emit(f"Error processing {os.path.basename(full_path)}: {e}")
                total_errors += 1
            self.progress.emit(int(((i + 1) / len(valid)) * 100))
            
        self.finished.emit(total_converted, total_errors)

# =======================================================================
# GUI APPLICATION INTERFACE
# =======================================================================
class FontToolApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BZ2 Font Tool Pro V4")
        self.setMinimumSize(700, 450)
        self.resize(900, 500)
        self.setAcceptDrops(True)
        self.font_files = {}
        self.worker = None
        self.app_settings = QSettings("VAC_Dominator", "BZCC_Font_Tool_v4")
        self._updating_ui = False
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.do_update_preview)
        
        self.default_profiles = {
            'Tiny':   {'size_offset': 0, 'stroke': 0, 'letter_spacing': 0, 'contrast': 2.0, 'brightness': 1.0, 'use_aa': False, 'sharpen': True, 'shadow': False, 'shadow_x': 2, 'shadow_y': 2},
            'System': {'size_offset': 0, 'stroke': 0, 'letter_spacing': 0, 'contrast': 2.0, 'brightness': 1.0, 'use_aa': False, 'sharpen': True, 'shadow': False, 'shadow_x': 2, 'shadow_y': 2},
            'Small':  {'size_offset': 0, 'stroke': 0, 'letter_spacing': 0, 'contrast': 2.0, 'brightness': 1.0, 'use_aa': False, 'sharpen': True, 'shadow': False, 'shadow_x': 2, 'shadow_y': 2},
            'Medium': {'size_offset': 0, 'stroke': 0, 'letter_spacing': 0, 'contrast': 2.0, 'brightness': 1.0, 'use_aa': False, 'sharpen': True, 'shadow': False, 'shadow_x': 2, 'shadow_y': 2},
            'Large':  {'size_offset': 0, 'stroke': 0, 'letter_spacing': 0, 'contrast': 2.0, 'brightness': 1.0, 'use_aa': False, 'sharpen': True, 'shadow': False, 'shadow_x': 2, 'shadow_y': 2}
        }
        self.profiles = copy.deepcopy(self.default_profiles)
        self.current_profile = "Global"
        self.modified_profiles = set()
        
        self.apply_modern_theme()
        self.init_menu_bar()
        self.init_ui()
        self.refresh_fonts()
        self.load_settings()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if os.path.isdir(path):
            self.input_folder.setText(path)
        elif os.path.isfile(path) and path.lower().endswith(('.ttf', '.otf', '.ttc', '.fon', '.fnt', '.pfb', '.pfa', '.dfont', '.bmf')):
            self.load_custom_font_from_path(path)

    def init_menu_bar(self):
        menubar = self.menuBar()
        self.view_menu = menubar.addMenu('View')

    def apply_modern_theme(self):
        self.setStyleSheet("""
        QMainWindow, QMenuBar, QMenu { background-color: #121212; color: #e0e0e0; }
        QWidget { color: #e0e0e0; font-family: 'Segoe UI', Arial, sans-serif; font-size: 11px; }
        QGroupBox {
            font-weight: 600; border: 1px solid #333; border-radius: 4px;
            margin-top: 6px; padding-top: 10px; padding-bottom: 4px;
            background-color: #1a1a1a;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #aaaaaa; }
        QLineEdit, QComboBox {
            background-color: #222; border: 1px solid #3a3a3a; border-radius: 3px;
            padding: 3px 6px; color: #fff; min-height: 22px;
        }
        QLineEdit:focus, QComboBox:focus { border: 1px solid #e63946; background-color: #2a2a2a; }
        QComboBox::drop-down { border: none; width: 18px; }
        QComboBox::down-arrow { image: none; border-left: 3px solid transparent; border-right: 3px solid transparent; border-top: 5px solid #aaa; width: 0; height: 0; margin-right: 4px; }
        QComboBox QAbstractItemView {
            background-color: #1a1a1a; color: #fff; selection-background-color: #e63946;
            selection-color: #fff; border: 1px solid #333;
        }
        QPushButton {
            background-color: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 3px;
            padding: 4px 10px; font-weight: 600; color: #e0e0e0; min-height: 22px;
        }
        QPushButton:hover { background-color: #383838; border: 1px solid #4a4a4a; }
        QPushButton:pressed { background-color: #1a1a1a; }
        QLabel { margin: 0px; padding: 0px; }
        QSlider { min-height: 18px; margin: 0px; }
        QSlider::groove:horizontal { height: 4px; background: #333; border-radius: 2px; }
        QSlider::handle:horizontal {
            background: #e63946; width: 12px; height: 12px;
            margin-top: -4px; margin-bottom: -4px; border-radius: 6px;
        }
        QSlider::handle:horizontal:hover { background: #ff4d5a; }
        QSlider:disabled { opacity: 0.5; }
        QCheckBox { spacing: 4px; }
        QCheckBox::indicator { width: 12px; height: 12px; border: 1px solid #444; border-radius: 2px; background: #222; }
        QCheckBox::indicator:checked { background: #e63946; border: 1px solid #ff4d5a; }
        QProgressBar { background-color: #222; border: 1px solid #333; border-radius: 3px; text-align: center; font-weight: bold; min-height: 16px; font-size: 10px; }
        QProgressBar::chunk { background-color: #e63946; border-radius: 2px; }
        QDockWidget { titlebar-close-button-position: right; color: #888; font-weight: bold; font-size: 10px; }
        QDockWidget::title { background-color: #1a1a1a; text-align: left; padding-left: 6px; padding-top: 3px; border: 1px solid #333;}
        """)

    def init_ui(self):
        self.scroll_preview = QScrollArea()
        self.scroll_preview.setWidgetResizable(True)
        self.scroll_preview.setStyleSheet("background-color: #111111; border: none;")
        self.lbl_preview = QLabel()
        self.lbl_preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lbl_preview.setStyleSheet("padding: 12px; color: #888;")
        self.scroll_preview.setWidget(self.lbl_preview)
        self.setCentralWidget(self.scroll_preview)
        
        self.controls_dock = QDockWidget("Control Panel V4 [Detach/Dock]", self)
        self.controls_dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.controls_dock.setFeatures(QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetMovable)
        self.view_menu.addAction(self.controls_dock.toggleViewAction())
        
        controls_widget = QWidget()
        layout = QVBoxLayout(controls_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        
        dir_group = QGroupBox("Directories")
        dir_grid = QGridLayout(dir_group)
        dir_grid.setContentsMargins(6, 10, 6, 4)
        dir_grid.setSpacing(4)
        
        self.input_folder = QLineEdit()
        self.input_folder.setPlaceholderText("Game interface folder (drop here)...")
        self.input_backup = QLineEdit()
        self.input_backup.setPlaceholderText("Backup folder (optional)...")
        self.btn_f = QPushButton("Browse")
        self.btn_f.clicked.connect(self.browse_folder)
        self.btn_b = QPushButton("Browse")
        self.btn_b.clicked.connect(self.browse_backup)
        
        dir_grid.addWidget(QLabel("Target:"), 0, 0)
        dir_grid.addWidget(self.input_folder, 0, 1)
        dir_grid.addWidget(self.btn_f, 0, 2)
        dir_grid.addWidget(QLabel("Backup:"), 0, 3)
        dir_grid.addWidget(self.input_backup, 0, 4)
        dir_grid.addWidget(self.btn_b, 0, 5)
        dir_grid.setColumnStretch(1, 1)
        dir_grid.setColumnStretch(4, 1)
        layout.addWidget(dir_group)
        
        config_group = QGroupBox("Font Source & Profiles")
        config_layout = QVBoxLayout(config_group)
        config_layout.setContentsMargins(6, 10, 6, 4)
        config_layout.setSpacing(4)
        
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self.combo_fonts = QComboBox()
        self.combo_fonts.currentIndexChanged.connect(self.schedule_preview_update)
        row1.addWidget(self.combo_fonts, stretch=3)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.refresh_fonts)
        row1.addWidget(self.btn_refresh)
        self.btn_load_font = QPushButton("Load TTF/BMF")
        self.btn_load_font.clicked.connect(self.browse_custom_font)
        row1.addWidget(self.btn_load_font)
        row1.addWidget(QLabel(" Scale:"))
        self.combo_scale = QComboBox()
        self.combo_scale.addItems(["x1.0", "x1.5", "x2.0", "x2.5", "x3.0", "x3.5", "x4.0", "x4.5", "x5.0"])
        self.combo_scale.currentIndexChanged.connect(self.schedule_preview_update)
        row1.addWidget(self.combo_scale)
        config_layout.addLayout(row1)
        
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        row2.addWidget(QLabel("Profiles: "))
        self.profile_buttons = {}
        for prof in ["Global", "Tiny", "System", "Small", "Medium", "Large"]:
            btn = QPushButton(prof)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, p=prof: self.select_profile(p))
            self.profile_buttons[prof] = btn
            row2.addWidget(btn, stretch=1)
        self.lbl_active_info = QLabel("Global View")
        self.lbl_active_info.setStyleSheet("color: #888; font-style: italic; margin-left: 10px;")
        row2.addWidget(self.lbl_active_info)
        row2.addStretch()
        self.btn_propagate = QPushButton("Apply to All")
        self.btn_propagate.clicked.connect(self.copy_profile_to_all)
        row2.addWidget(self.btn_propagate)
        self.btn_reset_profile = QPushButton("↺ Reset")
        self.btn_reset_profile.clicked.connect(self.reset_current_profile)
        row2.addWidget(self.btn_reset_profile)
        config_layout.addLayout(row2)
        
        row3 = QHBoxLayout()
        row3.setSpacing(4)
        row3.addWidget(QLabel("Preview Text:"))
        self.input_preview_text = QLineEdit()
        self.input_preview_text.setText("BATTLEZONE: COMBAT COMMANDER - HARDCORE RENDER ENGINE")
        self.input_preview_text.textChanged.connect(self.schedule_preview_update)
        row3.addWidget(self.input_preview_text, stretch=1)
        self.btn_toggle_dock = QPushButton("Toggle Control Panel")
        self.btn_toggle_dock.clicked.connect(self.toggle_dock_visibility)
        row3.addWidget(self.btn_toggle_dock)
        config_layout.addLayout(row3)
        layout.addWidget(config_group)
        
        self.settings_group = QGroupBox("Rendering Controls")
        settings_grid = QGridLayout(self.settings_group)
        settings_grid.setContentsMargins(6, 12, 6, 4)
        settings_grid.setSpacing(6)
        
        chk_layout = QHBoxLayout()
        chk_layout.setSpacing(6)
        
        def create_reset_btn(callback):
            b = QPushButton("↺")
            b.setFixedSize(18, 18)
            b.setStyleSheet("QPushButton { border: none; background: transparent; color: #888; font-size: 14px; padding: 0px; margin: 0px; min-height: 18px; } QPushButton:hover { color: #e63946; }")
            b.clicked.connect(callback)
            return b

        self.chk_aa = QCheckBox("Anti-Aliasing")
        self.chk_aa.stateChanged.connect(self.on_control_value_changed)
        chk_layout.addWidget(self.chk_aa)
        chk_layout.addWidget(create_reset_btn(lambda: self.chk_aa.setChecked(False)))
        
        chk_layout.addSpacing(10)
        self.chk_sharpen = QCheckBox("Sharpen")
        self.chk_sharpen.stateChanged.connect(self.on_control_value_changed)
        chk_layout.addWidget(self.chk_sharpen)
        chk_layout.addWidget(create_reset_btn(lambda: self.chk_sharpen.setChecked(True)))

        chk_layout.addSpacing(10)
        self.chk_shadow = QCheckBox("Drop Shadow")
        self.chk_shadow.stateChanged.connect(self.on_control_value_changed)
        chk_layout.addWidget(self.chk_shadow)
        chk_layout.addWidget(create_reset_btn(lambda: self.chk_shadow.setChecked(False)))
        
        chk_layout.addStretch()
        settings_grid.addLayout(chk_layout, 0, 0, 1, 8)
        
        self.slider_stroke, self.lbl_val_stroke = self.create_grid_slider(settings_grid, 1, 0, "Stroke", 0, 5, 0)
        self.slider_contrast, self.lbl_val_contrast = self.create_grid_slider(settings_grid, 1, 4, "Contrast", 1, 30, 20)
        
        self.slider_offset, self.lbl_val_offset = self.create_grid_slider(settings_grid, 2, 0, "Size Offset", -20, 20, 0)
        self.slider_brightness, self.lbl_val_brightness = self.create_grid_slider(settings_grid, 2, 4, "Brightness", 1, 30, 10)
        
        self.slider_spacing, self.lbl_val_spacing = self.create_grid_slider(settings_grid, 3, 0, "Spacing", -10, 20, 0)
        self.slider_shadow_x, self.lbl_val_shadow_x = self.create_grid_slider(settings_grid, 3, 4, "Shadow X", -10, 10, 2)
        
        self.slider_shadow_y, self.lbl_val_shadow_y = self.create_grid_slider(settings_grid, 4, 4, "Shadow Y", -10, 10, 2)
        
        layout.addWidget(self.settings_group)
        
        bot_layout = QHBoxLayout()
        bot_layout.setSpacing(6)
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setStyleSheet("color: #aaa;")
        bot_layout.addWidget(self.lbl_status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        bot_layout.addWidget(self.progress_bar)
        
        self.btn_process = QPushButton("Convert & Save")
        self.btn_process.setStyleSheet("""
        QPushButton { background-color: #8b0000; color: #fff; font-size: 13px; padding: 6px 20px; border: 1px solid #b30000; border-radius: 3px; font-weight: bold; }
        QPushButton:hover { background-color: #b30000; border: 1px solid #ff4d5a; }
        QPushButton:disabled { background-color: #333; color: #666; border: 1px solid #222; }
        """)
        self.btn_process.clicked.connect(self.start_processing)
        bot_layout.addWidget(self.btn_process)
        layout.addLayout(bot_layout)
        
        self.controls_dock.setWidget(controls_widget)
        self.addDockWidget(Qt.TopDockWidgetArea, self.controls_dock)

    def toggle_dock_visibility(self):
        if hasattr(self, 'controls_dock'):
            self.controls_dock.setVisible(not self.controls_dock.isVisible())
            if self.controls_dock.isVisible():
                self.controls_dock.raise_()

    def create_grid_slider(self, grid, row, col_start, label_text, min_val, max_val, default_val):
        lbl = QLabel(label_text)
        val_lbl = QLabel(str(default_val))
        val_lbl.setStyleSheet("font-weight: bold; color: #e63946; min-width: 24px; text-align: right;")
        slider = JumpSlider(Qt.Horizontal)
        slider.setMinimum(min_val)
        slider.setMaximum(max_val)
        slider.setValue(default_val)
        if "Contrast" in label_text or "Brightness" in label_text:
            slider.valueChanged.connect(lambda v: val_lbl.setText(f"{v / 10.0:.1f}"))
        else:
            slider.valueChanged.connect(lambda v: val_lbl.setText(str(v)))
        slider.valueChanged.connect(self.on_control_value_changed)
        
        btn_reset = QPushButton("↺")
        btn_reset.setToolTip(f"Reset {label_text}")
        btn_reset.setFixedSize(18, 18)
        btn_reset.setStyleSheet("QPushButton { border: none; background: transparent; color: #888; font-size: 14px; padding: 0px; margin: 0px; min-height: 18px; } QPushButton:hover { color: #e63946; }")
        btn_reset.clicked.connect(lambda: slider.setValue(default_val))
        
        grid.addWidget(lbl, row, col_start)
        grid.addWidget(slider, row, col_start + 1)
        grid.addWidget(val_lbl, row, col_start + 2)
        grid.addWidget(btn_reset, row, col_start + 3)
        return slider, val_lbl

    def select_profile(self, profile_name):
        self.current_profile = profile_name
        for p, btn in self.profile_buttons.items():
            btn.setChecked(p == profile_name)
            
        if profile_name == "Global":
            self.lbl_active_info.setText("GLOBAL: Changes apply to ALL")
            self.settings_group.setEnabled(True)
            self.settings_group.setTitle("Rendering Controls [GLOBAL MODE - All Profiles]")
            self.btn_propagate.setEnabled(False)
            self.btn_reset_profile.setEnabled(True)
            if 'System' in self.profiles:
                self.load_profile_ui('System')
        else:
            self.lbl_active_info.setText(f"Editing: {profile_name}")
            self.settings_group.setEnabled(True)
            self.settings_group.setTitle(f"Rendering Controls [{profile_name.upper()}]")
            self.btn_propagate.setEnabled(True)
            self.btn_reset_profile.setEnabled(True)
            self.load_profile_ui(profile_name)
            
        self.update_profile_indicators()
        self.schedule_preview_update()

    def load_profile_ui(self, profile_name):
        if profile_name not in self.profiles: return
        conf = self.profiles[profile_name]
        self._updating_ui = True
        self.slider_stroke.setValue(conf.get('stroke', 0))
        self.slider_offset.setValue(conf.get('size_offset', 0))
        self.slider_spacing.setValue(conf.get('letter_spacing', 0))
        self.slider_contrast.setValue(int(conf.get('contrast', 1.0) * 10))
        self.slider_brightness.setValue(int(conf.get('brightness', 1.0) * 10))
        self.slider_shadow_x.setValue(conf.get('shadow_x', 2))
        self.slider_shadow_y.setValue(conf.get('shadow_y', 2))
        self.chk_aa.setChecked(conf.get('use_aa', False))
        self.chk_sharpen.setChecked(conf.get('sharpen', False))
        self.chk_shadow.setChecked(conf.get('shadow', False))
        
        self.lbl_val_stroke.setText(str(conf.get('stroke', 0)))
        self.lbl_val_offset.setText(str(conf.get('size_offset', 0)))
        self.lbl_val_spacing.setText(str(conf.get('letter_spacing', 0)))
        self.lbl_val_shadow_x.setText(str(conf.get('shadow_x', 2)))
        self.lbl_val_shadow_y.setText(str(conf.get('shadow_y', 2)))
        self.lbl_val_contrast.setText(f"{conf.get('contrast', 1.0):.1f}")
        self.lbl_val_brightness.setText(f"{conf.get('brightness', 1.0):.1f}")
        self._updating_ui = False

    def check_if_modified(self, profile_name):
        if profile_name not in self.default_profiles or profile_name not in self.profiles:
            return False
        return self.default_profiles[profile_name] != self.profiles[profile_name]

    def update_profile_indicators(self):
        for prof, btn in self.profile_buttons.items():
            if prof == "Global": continue
            is_mod = self.check_if_modified(prof)
            if is_mod:
                self.modified_profiles.add(prof)
                btn.setText(f"{prof} *")
                btn.setStyleSheet("QPushButton { background-color: #4a1a1a; border: 1px solid #6a2a2a; color: #ff9999; font-weight: bold; } QPushButton:hover { background-color: #5a2a2a; } QPushButton:checked { background-color: #4a1a1a; color: #ffcccc; border: 1px solid #9b0000; }")
            else:
                self.modified_profiles.discard(prof)
                btn.setText(prof)
                btn.setStyleSheet("")

    def on_control_value_changed(self):
        if self._updating_ui: return
        profile_name = self.current_profile
        new_values = {
            'stroke': self.slider_stroke.value(),
            'size_offset': self.slider_offset.value(),
            'letter_spacing': self.slider_spacing.value(),
            'shadow_x': self.slider_shadow_x.value(),
            'shadow_y': self.slider_shadow_y.value(),
            'contrast': self.slider_contrast.value() / 10.0,
            'brightness': self.slider_brightness.value() / 10.0,
            'use_aa': self.chk_aa.isChecked(),
            'sharpen': self.chk_sharpen.isChecked(),
            'shadow': self.chk_shadow.isChecked()
        }
        if profile_name == "Global":
            for p in self.profiles:
                self.profiles[p].update(new_values)
        elif profile_name in self.profiles:
            self.profiles[profile_name].update(new_values)
            
        self.update_profile_indicators()
        self.schedule_preview_update()

    def copy_profile_to_all(self):
        src_profile = self.current_profile
        if src_profile == "Global" or src_profile not in self.profiles:
            return
        src_conf = self.profiles[src_profile]
        for name in self.profiles:
            if name != src_profile:
                self.profiles[name] = src_conf.copy()
        self.update_profile_indicators()
        QMessageBox.information(self, "Synced", f"Applied '{src_profile}' settings to all profiles.")

    def reset_current_profile(self):
        prof = self.current_profile
        if prof == "Global":
            for p in self.default_profiles:
                self.profiles[p] = self.default_profiles[p].copy()
            self.load_profile_ui('System')
        elif prof in self.default_profiles:
            self.profiles[prof] = self.default_profiles[prof].copy()
            self.load_profile_ui(prof)
        self.update_profile_indicators()
        self.schedule_preview_update()

    def render_text_line(self, face, text, conf, base_size):
        use_aa = conf.get('use_aa', False)
        sharpen = conf.get('sharpen', True)
        stroke = conf.get('stroke', 0)
        offset = conf.get('size_offset', 0)
        spacing = conf.get('letter_spacing', 0)
        contrast = conf.get('contrast', 1.0)
        brightness = conf.get('brightness', 1.0)
        use_shadow = conf.get('shadow', False)
        shadow_x = conf.get('shadow_x', 2)
        shadow_y = conf.get('shadow_y', 2)
        
        render_size = max(1, base_size + offset + 10)
        set_safe_pixel_size(face, render_size)
        metrics = face.size
        asc = metrics.ascender >> 6
        fa = min(255, max(0, asc + stroke + (max(0, -shadow_y) if use_shadow else 0)))
        
        load_flags = freetype.FT_LOAD_RENDER
        if not use_aa and not isinstance(face, BmfFace):
            load_flags |= freetype.FT_LOAD_TARGET_MONO
        elif not isinstance(face, BmfFace):
            load_flags |= freetype.FT_LOAD_TARGET_NORMAL | freetype.FT_LOAD_NO_BITMAP
            
        img_w, img_h = max(500, len(text) * render_size + 100), max(50, render_size * 2 + 50)
        img = Image.new("L", (img_w, img_h), 18)
        pen_x = 20
        pen_y_text = fa + 20
        max_x_used = 0
        max_h_text = 0
        for char in text:
            try:
                face.load_char(char if isinstance(char, int) else ord(char), load_flags)
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
                        for r in range(gh): rows.append(buf[r * pitch : r * pitch + gw])
                    else:
                        for r in range(gh):
                            row = bytearray(gw)
                            for x in range(gw):
                                byte_idx = r * pitch + x // 8
                                if byte_idx < len(buf) and ((buf[byte_idx] >> (7 - x % 8)) & 1): row[x] = 255
                            rows.append(bytes(row))
                    raw = b"".join(rows)
                    char_mask = Image.frombytes("L", (gw, gh), raw)
                    if brightness != 1.0: char_mask = ImageEnhance.Brightness(char_mask).enhance(brightness)
                    if contrast != 1.0: char_mask = ImageEnhance.Contrast(char_mask).enhance(contrast)
                    if sharpen: char_mask = char_mask.filter(ImageFilter.SHARPEN)
                    
                    if stroke > 0:
                        orig = char_mask.copy()
                        char_mask = ImageOps.expand(char_mask, border=stroke, fill=0)
                        char_mask = char_mask.filter(ImageFilter.MaxFilter(size=stroke * 2 + 1))
                        char_mask.paste(orig, (stroke, stroke))
                        orig.close()
                        bear_x -= stroke
                        bear_y += stroke
                        adv_x += stroke
                        
                    if use_shadow:
                        orig = char_mask.copy()
                        sx = max(0, shadow_x)
                        sy = max(0, shadow_y)
                        neg_sx = max(0, -shadow_x)
                        neg_sy = max(0, -shadow_y)
                        new_w = gw + abs(shadow_x)
                        new_h = gh + abs(shadow_y)
                        char_mask = Image.new("L", (new_w, new_h), 0)
                        shadow_layer = orig.point(lambda p: p * 0.4)
                        char_mask.paste(shadow_layer, (sx, sy))
                        char_mask.paste(orig, (neg_sx, neg_sy), orig)
                        orig.close()
                        bear_x -= neg_sx
                        bear_y += neg_sy
                        adv_x += max(0, shadow_x)
                        
                    white_fill = Image.new("L", char_mask.size, 255)
                    paste_x = pen_x + bear_x
                    paste_y = pen_y_text - bear_y
                    if 0 <= paste_x < img_w and 0 <= paste_y < img_h:
                        img.paste(white_fill, (paste_x, paste_y), char_mask)
                    max_h_text = max(max_h_text, char_mask.height)
                    char_mask.close()
                    white_fill.close()
                    pen_x += adv_x + spacing
                    max_x_used = max(max_x_used, pen_x)
            except Exception:
                continue
        crop_w = max(1, min(max_x_used + 50, img_w))
        crop_h = max(1, min(pen_y_text + max_h_text + 30, img_h))
        cropped = img.crop((0, 0, crop_w, crop_h))
        img.close()
        res = cropped.convert("RGBA")
        cropped.close()
        return res

    def schedule_preview_update(self):
        self.preview_timer.start(100)

    def do_update_preview(self):
        display_name = self.combo_fonts.currentText()
        if not display_name: return
        path = self.font_files.get(display_name)
        if not path or not os.path.exists(path): return
        scale_idx = self.combo_scale.currentIndex()
        if scale_idx < 0: return
        active_profile = self.current_profile
        text_base = self.input_preview_text.text() or "RENDER MATRIX"
        try:
            face = load_face(path)
            targets = ['Tiny', 'System', 'Small', 'Medium', 'Large'] if active_profile == "Global" else [active_profile]
            rendered_strips = []
            for prof in targets:
                conf = self.profiles.get(prof, {})
                if prof == 'Tiny': base_size = [6, 10, 14, 18, 22, 26, 30, 34, 38][scale_idx]
                elif prof in ['System', 'Small']: base_size = [8, 12, 16, 20, 24, 28, 32, 36, 40][scale_idx]
                elif prof == 'Medium': base_size = [10, 15, 20, 25, 30, 35, 40, 45, 50][scale_idx]
                else: base_size = [12, 18, 24, 30, 36, 42, 48, 54, 60][scale_idx]
                line_text = f"[{prof.upper()}]  {text_base}"
                try:
                    strip = self.render_text_line(face, line_text, conf, base_size)
                    rendered_strips.append(strip)
                except Exception as e:
                    pass
            if not rendered_strips: return
            if len(rendered_strips) == 1:
                final_img = rendered_strips[0]
            else:
                w = max((img.width for img in rendered_strips), default=1)
                h = sum(img.height for img in rendered_strips) + (10 * max(0, len(rendered_strips) - 1))
                final_img = Image.new("RGBA", (w, h), (18, 18, 18, 255))
                curr_y = 0
                for strip in rendered_strips:
                    row_bg = Image.new("RGBA", (w, strip.height), (24, 24, 24, 255))
                    row_bg.paste(strip, (0, 0), strip)
                    final_img.paste(row_bg, (0, curr_y))
                    curr_y += strip.height + 10
                    strip.close()
                    row_bg.close()
            data = final_img.tobytes("raw", "RGBA")
            qim = QImage(data, final_img.width, final_img.height, QImage.Format_RGBA8888)
            self.lbl_preview.setPixmap(QPixmap.fromImage(qim))
            final_img.close()
        except Exception as e:
            self.lbl_preview.setText(f"Preview Error:\n{e}")

    def load_settings(self):
        def _get_int(val, default):
            try: return int(val)
            except (ValueError, TypeError): return default
        def _get_bool(val, default):
            if val is None: return default
            if isinstance(val, str): return val.lower() == 'true'
            return bool(val)
        self._updating_ui = True
        self.input_folder.setText(str(self.app_settings.value("folder", "")))
        self.input_backup.setText(str(self.app_settings.value("backup_folder", "")))
        s_idx = _get_int(self.app_settings.value("scale_idx"), 0)
        if 0 <= s_idx < self.combo_scale.count():
            self.combo_scale.setCurrentIndex(s_idx)
        saved_text = self.app_settings.value("preview_text")
        if saved_text: self.input_preview_text.setText(str(saved_text))
        for prof_name in self.profiles:
            p_stroke = self.app_settings.value(f"p_{prof_name}_stroke")
            p_offset = self.app_settings.value(f"p_{prof_name}_offset")
            p_spacing = self.app_settings.value(f"p_{prof_name}_spacing")
            p_contrast = self.app_settings.value(f"p_{prof_name}_contrast")
            p_brightness = self.app_settings.value(f"p_{prof_name}_brightness")
            p_use_aa = self.app_settings.value(f"p_{prof_name}_use_aa")
            p_sharpen = self.app_settings.value(f"p_{prof_name}_sharpen")
            p_shadow = self.app_settings.value(f"p_{prof_name}_shadow")
            p_shadow_x = self.app_settings.value(f"p_{prof_name}_shadow_x")
            p_shadow_y = self.app_settings.value(f"p_{prof_name}_shadow_y")
            
            if p_stroke is not None: self.profiles[prof_name]['stroke'] = _get_int(p_stroke, 0)
            if p_offset is not None: self.profiles[prof_name]['size_offset'] = _get_int(p_offset, 0)
            if p_spacing is not None: self.profiles[prof_name]['letter_spacing'] = _get_int(p_spacing, 0)
            if p_shadow_x is not None: self.profiles[prof_name]['shadow_x'] = _get_int(p_shadow_x, 2)
            if p_shadow_y is not None: self.profiles[prof_name]['shadow_y'] = _get_int(p_shadow_y, 2)
            if p_contrast is not None:
                try: self.profiles[prof_name]['contrast'] = float(p_contrast)
                except ValueError: pass
            if p_brightness is not None:
                try: self.profiles[prof_name]['brightness'] = float(p_brightness)
                except ValueError: pass
            if p_use_aa is not None: self.profiles[prof_name]['use_aa'] = _get_bool(p_use_aa, False)
            if p_sharpen is not None: self.profiles[prof_name]['sharpen'] = _get_bool(p_sharpen, True)
            if p_shadow is not None: self.profiles[prof_name]['shadow'] = _get_bool(p_shadow, False)
            
        saved_prof = self.app_settings.value("active_profile", "Global")
        names = ["Global", "Tiny", "System", "Small", "Medium", "Large"]
        if saved_prof not in names: saved_prof = "Global"
        self.select_profile(saved_prof)
        self._updating_ui = False

    def closeEvent(self, event):
        self.app_settings.setValue("folder", self.input_folder.text())
        self.app_settings.setValue("backup_folder", self.input_backup.text())
        self.app_settings.setValue("scale_idx", self.combo_scale.currentIndex())
        self.app_settings.setValue("preview_text", self.input_preview_text.text())
        self.app_settings.setValue("active_profile", self.current_profile)
        for prof_name, conf in self.profiles.items():
            self.app_settings.setValue(f"p_{prof_name}_stroke", conf.get('stroke', 0))
            self.app_settings.setValue(f"p_{prof_name}_offset", conf.get('size_offset', 0))
            self.app_settings.setValue(f"p_{prof_name}_spacing", conf.get('letter_spacing', 0))
            self.app_settings.setValue(f"p_{prof_name}_contrast", conf.get('contrast', 1.0))
            self.app_settings.setValue(f"p_{prof_name}_brightness", conf.get('brightness', 1.0))
            self.app_settings.setValue(f"p_{prof_name}_use_aa", conf.get('use_aa', False))
            self.app_settings.setValue(f"p_{prof_name}_sharpen", conf.get('sharpen', False))
            self.app_settings.setValue(f"p_{prof_name}_shadow", conf.get('shadow', False))
            self.app_settings.setValue(f"p_{prof_name}_shadow_x", conf.get('shadow_x', 2))
            self.app_settings.setValue(f"p_{prof_name}_shadow_y", conf.get('shadow_y', 2))
        if self.worker and self.worker.isRunning():
            self.worker.is_running = False
            self.worker.wait()
        event.accept()

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Target Folder")
        if folder: self.input_folder.setText(folder)

    def browse_backup(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Backup Folder")
        if folder: self.input_backup.setText(folder)

    def refresh_fonts(self):
        self.combo_fonts.blockSignals(True)
        self.combo_fonts.clear()
        self.font_files.clear()
        font_dirs = []
        if sys.platform == "win32":
            windir = os.environ.get("WINDIR")
            if windir: font_dirs.append(os.path.join(windir, "Fonts"))
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata: font_dirs.append(os.path.join(local_appdata, "Microsoft", "Windows", "Fonts"))
            
        valid_exts = ('.ttf', '.otf', '.ttc', '.dfont', '.pfb', '.pfa', '.fnt', '.fon', '.bmf')
        
        tool_dir = os.path.dirname(os.path.abspath(__file__))
        for root, _, files in os.walk(tool_dir):
            for f in files:
                if f.lower().endswith(valid_exts):
                    path = os.path.join(root, f)
                    try:
                        face = load_face(path)
                        family = face.family_name.decode(errors='replace') if isinstance(face.family_name, bytes) else face.family_name
                        if not family: family = "Unknown"
                        
                        display_name = family
                        if f.lower().endswith('.bmf'):
                            display_name = f"🖫 [BMF Binary] {display_name} ({f})"
                        elif 'bzone' in f.lower() or 'bz1' in f.lower():
                            display_name = f"★ [BZ1 Classic] {display_name} ({f})"
                        elif root != tool_dir:
                            display_name = f"[Local] {display_name} ({f})"
                            
                        if display_name not in self.font_files:
                            self.font_files[display_name] = path
                            self.combo_fonts.addItem(display_name)
                    except Exception:
                        pass
        
        for d in font_dirs:
            if not os.path.exists(d) or not os.path.isdir(d): continue
            try:
                for f in os.listdir(d):
                    if f.lower().endswith(valid_exts):
                        path = os.path.join(d, f)
                        try:
                            face = load_face(path)
                            family = face.family_name.decode(errors='replace') if isinstance(face.family_name, bytes) else face.family_name
                            if not family: family = "Unknown"
                            display_name = family
                            if display_name not in self.font_files:
                                self.font_files[display_name] = path
                                self.combo_fonts.addItem(display_name)
                        except Exception:
                            pass
            except OSError:
                pass
                
        items = [self.combo_fonts.itemText(i) for i in range(self.combo_fonts.count())]
        bmf_items = sorted([item for item in items if "🖫 [BMF Binary]" in item])
        bz_items = sorted([item for item in items if "★ [BZ1 Classic]" in item])
        other_items = sorted([item for item in items if "🖫" not in item and "★" not in item])
        
        self.combo_fonts.clear()
        self.combo_fonts.addItems(bmf_items + bz_items + other_items)
        
        self.combo_fonts.blockSignals(False)
        self.schedule_preview_update()

    def load_custom_font_from_path(self, file_path):
        if file_path and os.path.exists(file_path):
            try:
                face = load_face(file_path)
                family = face.family_name.decode(errors='replace') if isinstance(face.family_name, bytes) else face.family_name
                if not family: family = "CustomFont"
                
                f = os.path.basename(file_path)
                if file_path.lower().endswith('.bmf'):
                    name_with_custom = f"🖫 [Custom BMF] {family} ({f})"
                else:
                    name_with_custom = f"[Custom] {family}"
                    
                self.font_files[name_with_custom] = file_path
                existing_idx = self.combo_fonts.findText(name_with_custom)
                if existing_idx >= 0:
                    self.combo_fonts.setCurrentIndex(existing_idx)
                else:
                    self.combo_fonts.addItem(name_with_custom)
                    self.combo_fonts.setCurrentText(name_with_custom)
            except Exception as e:
                QMessageBox.critical(self, "Font Error", f"Failed to load font:\n{e}")

    def browse_custom_font(self):
        file, _ = QFileDialog.getOpenFileName(self, "Load Custom Font", "", "Fonts (*.ttf *.otf *.ttc *.fon *.fnt *.pfb *.pfa *.dfont *.bmf)")
        if file:
            self.load_custom_font_from_path(file)

    def start_processing(self):
        folder = self.input_folder.text()
        if not folder or not os.path.exists(folder) or not os.path.isdir(folder):
            QMessageBox.warning(self, "Invalid Target", "Please select a valid directory.")
            return
        display_name = self.combo_fonts.currentText()
        if not display_name:
            QMessageBox.warning(self, "No Font", "Select a font first.")
            return
        font_path = self.font_files.get(display_name)
        if not font_path or not os.path.exists(font_path):
            QMessageBox.warning(self, "Missing Font", "Font file not found.")
            return
        scale_idx = self.combo_scale.currentIndex()
        self.btn_process.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_status.setText("Processing...")
        safe_profiles = copy.deepcopy(self.profiles)
        self.worker = ProcessWorker(folder, self.input_backup.text(), font_path, scale_idx, safe_profiles)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.log.connect(self.lbl_status.setText)
        self.worker.finished.connect(self.on_processing_finished)
        self.worker.start()

    def on_processing_finished(self, conv, errs):
        self.btn_process.setEnabled(True)
        self.progress_bar.setValue(100)
        if errs == 0:
            self.lbl_status.setText(f"Complete. {conv} files processed.")
        else:
            self.lbl_status.setText(f"Done. {conv} updated, {errs} failed.")
        self.worker.deleteLater()
        self.worker = None

if __name__ == "__main__":
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = FontToolApp()
    window.show()
    sys.exit(app.exec_())
