#!/usr/bin/env python3
"""
A treemap viewer for directories – inspired by SpaceMonger.

Features:
  - A toolbar at the top with small buttons (icon left, text right) and reduced margins.
  - "Open" to choose a directory.
  - "Reload" to re-scan the currently loaded base directory.
  - "Go Up" and "Go Top" for navigation.
  - The directory scan is performed in a separate thread using a recursive algorithm.
    This function computes each folder's total size recursively and emits progress updates every 1 second.
  - Each directory Node carries a 'complete' flag (False until its contents are fully scanned).
  - In the treemap view, if a directory isn’t complete, an overlay label “Scanning…” is drawn.
  - In each viewport, if a directory has more than 2000 children, only the 2000 largest are rendered.
  - When a new folder is selected, the viewport is cleared until the first progress update arrives.
  - Hovering over a block shows a tooltip with file details.
  - Double‑clicking a directory’s label area zooms into that directory.
  - The status bar indicates the overall scan status and shows which directory is currently being scanned (truncated).
  
Scanning is triggered only via Open or Reload.

Important: This version does not follow symlinks. If a file or directory is a symlink, it is either skipped (in directories) or treated as a zero‑sized file.
"""

import os, sys, stat, pwd, grp, datetime, time
from collections import deque
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QFileDialog,
                             QToolBar, QAction, QVBoxLayout, QStyle)
from PyQt5.QtGui import QPainter, QColor, QFont, QPen
from PyQt5.QtCore import Qt, QRectF, pyqtSignal, QThread, QSize

# ---------------- Utility Functions ----------------

def human_readable_size(size, decimal_places=1):
    for unit in ['B','KB','MB','GB','TB','PB','EB']:
        if size < 1024:
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024
    return f"{size:.{decimal_places}f} YB"

def format_stat(path):
    """Return a string with file stat details."""
    try:
        st = os.stat(path)
    except Exception as e:
        return "Stat error: " + str(e)
    size = st.st_size
    mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    ctime = datetime.datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
    atime = datetime.datetime.fromtimestamp(st.st_atime).strftime("%Y-%m-%d %H:%M:%S")
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    perms = stat.filemode(st.st_mode)
    return (f"Size: {size} bytes ({human_readable_size(size)})<br>"
            f"Created: {ctime}<br>Modified: {mtime}<br>Accessed: {atime}<br>"
            f"Owner: {owner}  Group: {group}<br>Permissions: {perms}")

# ---------------- Data Model ------------------

class Node:
    def __init__(self, path, name, is_dir, size=0, children=None, parent=None):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.children = children if children is not None else []
        self.parent = parent
        # For files, scanning is complete by definition.
        self.complete = True if not is_dir else False

# ---------------- Directory Scanner Thread ----------------

class DirectoryScanner(QThread):
    # Emits a tuple: (top_node, current_directory)
    progress = pyqtSignal(object, str)
    # Emits the final, complete top-level Node.
    finished = pyqtSignal(object)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        update_interval = 1.0  # seconds
        last_emit = time.time()

        # Recursive scanning function.
        def scan_directory(path, parent=None):
            nonlocal last_emit, update_interval, top
            # Do not follow symlinks.
            if os.path.islink(path):
                # Treat symlinks as zero-sized files.
                return Node(path, os.path.basename(path) or path, False, 0, parent=parent)
            if os.path.isfile(path):
                try:
                    size = os.path.getsize(path)
                except Exception:
                    size = 0
                return Node(path, os.path.basename(path) or path, False, size, parent=parent)
            elif os.path.isdir(path):
                node = Node(path, os.path.basename(path) or path, True, parent=parent)
                node.complete = False
                total = 0
                try:
                    for entry in os.scandir(path):
                        # Skip symlinks.
                        if entry.is_symlink():
                            continue
                        child = scan_directory(entry.path, node)
                        node.children.append(child)
                        total += child.size
                        # Emit progress update if enough time has passed.
                        if time.time() - last_emit >= update_interval:
                            # current directory being processed is 'path'
                            self.progress.emit(top, path)
                            last_emit = time.time()
                except Exception:
                    pass
                node.size = total
                node.complete = True
                return node
            else:
                return Node(path, os.path.basename(path) or path, False, 0, parent=parent)

        top = scan_directory(self.path)
        self.finished.emit(top)

# --------------- Squarified Treemap Algorithm ---------------

def worst_ratio(row, length):
    """
    Given a list of areas in 'row' and the current side length,
    compute the worst (maximum) aspect ratio. If any area is zero, return infinity.
    """
    total = sum(row)
    side = total / length if length != 0 else 0
    max_ratio = 0
    for r in row:
        if r == 0:
            return float('inf')
        else:
            ratio = max(side * side / r, r / (side * side))
            if ratio > max_ratio:
                max_ratio = ratio
    return max_ratio

def squarify(areas, x, y, width, height):
    """
    Given a list of areas and a rectangle (x, y, width, height),
    return a list of rectangles (tuples (x, y, w, h)) whose areas are proportional
    to the provided areas.
    """
    rects = []
    areas = areas[:]  # work on a copy
    while areas:
        row = [areas.pop(0)]
        if width >= height:
            current_worst = worst_ratio(row, width)
            while areas and current_worst >= worst_ratio(row + [areas[0]], width):
                row.append(areas.pop(0))
                current_worst = worst_ratio(row, width)
            total_row = sum(row)
            if total_row == 0:
                for _ in row:
                    rects.append((x, y, 0, 0))
            else:
                row_height = total_row / width
                rx = x
                for r in row:
                    rw = (r / row_height) if row_height != 0 else 0
                    rects.append((rx, y, rw, row_height))
                    rx += rw
                y += row_height
                height -= row_height
        else:
            current_worst = worst_ratio(row, height)
            while areas and current_worst >= worst_ratio(row + [areas[0]], height):
                row.append(areas.pop(0))
                current_worst = worst_ratio(row, height)
            total_row = sum(row)
            if total_row == 0:
                for _ in row:
                    rects.append((x, y, 0, 0))
            else:
                col_width = total_row / height
                ry = y
                for r in row:
                    rh = (r / col_width) if col_width != 0 else 0
                    rects.append((x, ry, col_width, rh))
                    ry += rh
                x += col_width
                width -= col_width
    return rects

# --------------- Treemap Widget ---------------

class TreemapWidget(QWidget):
    # Signal to notify that the current node has changed (for updating title, etc.)
    nodeChanged = pyqtSignal(object)

    def __init__(self, root_node=None, parent=None):
        super().__init__(parent)
        self.root_node = root_node  # base node
        self.current_node = root_node  # node being viewed
        self.baseHue = 200  # starting hue; rotates with depth
        self._node_rects = []  # list of (QRectF, Node) for drawn blocks
        self._label_rects = []  # list of (QRectF, Node) for directory labels
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(QFont("Sans", 8))
        rect = QRectF(0, 0, self.width(), self.height())
        self._node_rects = []
        self._label_rects = []
        if self.current_node is not None:
            self.draw_node(painter, self.current_node, rect, depth=0)
        painter.end()

    def draw_node(self, painter, node, rect, depth):
        if rect.width() <= 0 or rect.height() <= 0:
            return

        self._node_rects.append((QRectF(rect), node))
        hue = (self.baseHue + depth * 30) % 360
        sat = 150 if node.is_dir else 100
        col = QColor.fromHsv(hue, sat, 220)
        painter.fillRect(rect, col)
        pen = QPen(Qt.black, 1)
        painter.setPen(pen)
        painter.drawRect(rect)

        margin = 2
        fm = painter.fontMetrics()
        labelRect = QRectF(rect.x() + margin, rect.y() + margin,
                           rect.width() - 2 * margin, fm.height())
        elided = fm.elidedText(node.name, Qt.ElideRight, int(labelRect.width()))
        painter.drawText(labelRect, Qt.AlignLeft | Qt.AlignVCenter, elided)

        if node.is_dir:
            self._label_rects.append((QRectF(labelRect), node))
            if not node.complete:
                overlay_text = "Scanning..."
                overlay_rect = QRectF(rect.x(), rect.y(), rect.width(), rect.height())
                overlay_color = QColor(255, 255, 255, 180)
                painter.fillRect(overlay_rect.adjusted(0, rect.height()-fm.height()-2, 0, 0),
                                   overlay_color)
                painter.drawText(overlay_rect.adjusted(0, rect.height()-fm.height()-2, -2, -2),
                                 Qt.AlignRight | Qt.AlignVCenter, overlay_text)

        if node.is_dir and node.children and rect.width() > 30 and rect.height() > (fm.height() + 10):
            inner = QRectF(rect.x() + margin, rect.y() + fm.height() + margin,
                           rect.width() - 2 * margin, rect.height() - fm.height() - 2 * margin)
            if inner.width() < 20 or inner.height() < 20:
                return

            children = sorted(node.children, key=lambda n: n.size, reverse=True)
            if len(children) > 2000:
                children = children[:2000]
            total = sum(child.size for child in children)
            if total <= 0:
                return

            innerArea = inner.width() * inner.height()
            scaledAreas = [child.size / total * innerArea for child in children]
            rects = squarify(scaledAreas, inner.x(), inner.y(), inner.width(), inner.height())
            for child, r in zip(children, rects):
                childRect = QRectF(*r)
                self.draw_node(painter, child, childRect, depth + 1)

    def mouseMoveEvent(self, event):
        pos = event.pos()
        hit_node = None
        for rect, node in self._node_rects:
            if rect.contains(pos):
                if hit_node is None or rect.width() * rect.height() < hit_node[0].width() * hit_node[0].height():
                    hit_node = (rect, node)
        if hit_node:
            node = hit_node[1]
            tip = f"<b>{node.name}</b><br>{node.path}<br>"
            tip += f"Total size: {node.size} bytes ({human_readable_size(node.size)})<br>"
            tip += format_stat(node.path)
            self.setToolTip(tip)
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        pos = event.pos()
        hit_node = None
        for rect, node in self._label_rects:
            if rect.contains(pos):
                hit_node = node
                break
        if hit_node and hit_node.is_dir:
            self.current_node = hit_node
            self.nodeChanged.emit(self.current_node)
            self.update()
        else:
            super().mouseDoubleClickEvent(event)

    def reload_current(self):
        self.parent().start_scan(self.root_node.path)

    def go_up(self):
        if self.current_node and self.current_node.parent is not None:
            self.current_node = self.current_node.parent
            self.nodeChanged.emit(self.current_node)
            self.update()

    def go_top(self):
        if self.root_node and self.current_node != self.root_node:
            self.current_node = self.root_node
            self.nodeChanged.emit(self.current_node)
            self.update()

# --------------- Main Window ---------------

class MainWindow(QMainWindow):
    def __init__(self, root_node=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Treemap")
        self.treemap = TreemapWidget(root_node)
        self.treemap.nodeChanged.connect(self.on_node_changed)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self.toolbar = QToolBar()
        small_icon_size = self.style().pixelMetric(QStyle.PM_SmallIconSize)
        self.toolbar.setIconSize(QSize(small_icon_size, small_icon_size))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.treemap)
        self.setCentralWidget(central)

        self.statusBar().showMessage("")
        
        self.create_actions()
        self.update_actions()
        self.scanner_thread = None

    def create_actions(self):
        self.open_action = QAction("Open", self)
        self.open_action.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.open_action.triggered.connect(self.open_directory)
        self.toolbar.addAction(self.open_action)

        self.reload_action = QAction("Reload", self)
        self.reload_action.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.reload_action.triggered.connect(self.reload_directory)
        self.toolbar.addAction(self.reload_action)

        self.up_action = QAction("Go Up", self)
        self.up_action.setIcon(self.style().standardIcon(QStyle.SP_ArrowUp))
        self.up_action.triggered.connect(self.treemap.go_up)
        self.toolbar.addAction(self.up_action)

        self.top_action = QAction("Go Top", self)
        self.top_action.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        self.top_action.triggered.connect(self.treemap.go_top)
        self.toolbar.addAction(self.top_action)

    def update_actions(self):
        node = self.treemap.current_node
        has_node = node is not None
        self.reload_action.setEnabled(has_node)
        self.up_action.setEnabled(has_node and node.parent is not None)
        self.top_action.setEnabled(has_node and (node != self.treemap.root_node))

    def start_scan(self, path):
        self.treemap.root_node = None
        self.treemap.current_node = None
        self.treemap.update()
        self.setWindowTitle(f"Treemap: {path} (scanning...)")
        self.statusBar().showMessage("Scanning...")
        self.open_action.setEnabled(False)
        self.reload_action.setEnabled(False)
        self.up_action.setEnabled(False)
        self.top_action.setEnabled(False)
        if self.scanner_thread is not None:
            self.scanner_thread.terminate()
            self.scanner_thread.wait()
        self.scanner_thread = DirectoryScanner(path)
        self.scanner_thread.progress.connect(self.on_scan_progress)
        self.scanner_thread.finished.connect(self.on_scan_finished)
        self.scanner_thread.start()

    def open_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory", os.path.expanduser("~"))
        if directory:
            self.setWindowTitle(f"Treemap: {directory} (scanning...)")
            self.start_scan(directory)

    def reload_directory(self):
        if self.treemap.root_node:
            self.start_scan(self.treemap.root_node.path)

    def on_scan_progress(self, top_node, current_dir):
        self.treemap.root_node = top_node
        self.treemap.current_node = top_node
        self.treemap.update()
        self.setWindowTitle(f"Treemap: {top_node.path} (scanning...)")
        max_length = 50
        current_dir_display = current_dir if len(current_dir) <= max_length else current_dir[:max_length] + "..."
        self.statusBar().showMessage(f"Scanning: {current_dir_display}")
        self.update_actions()

    def on_scan_finished(self, final_node):
        self.treemap.root_node = final_node
        self.treemap.current_node = final_node
        self.treemap.update()
        self.setWindowTitle(f"Treemap: {final_node.path}")
        self.statusBar().showMessage("Scan complete.", 3000)
        self.open_action.setEnabled(True)
        self.update_actions()

    def on_node_changed(self, node):
        self.setWindowTitle(f"Treemap: {node.path}")
        self.update_actions()

# --------------- Main Entry Point ---------------

def main():
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if not os.path.exists(target):
            print("Path does not exist:", target)
            sys.exit(1)
        initial_node = None
        auto_scan = target
    else:
        initial_node = None
        auto_scan = None

    app = QApplication(sys.argv)
    win = MainWindow(initial_node)
    win.resize(1000, 700)
    win.show()

    if auto_scan:
        win.start_scan(auto_scan)
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
