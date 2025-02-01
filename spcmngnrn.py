#!/usr/bin/env python3
"""
A Directory Treemap Viewer inspired by SpaceMonger.

Features:
- A top toolbar with small buttons that show an icon on the left and text on the right:
    • Open (which turns to Stop while scanning),
    • Reload (rescans the originally loaded directory),
    • Go Top and Go Up (to navigate the scanned tree).
- A status bar that shows messages such as “Please open a directory”, 
  and during scanning it shows the actual file/folder currently being processed.
- A viewport (the central widget) that displays the treemap.
- The treemap displays directories and files as rectangles whose areas are proportional
  to file sizes. The layout uses a squarified algorithm.
- Each block shows a truncated filename label that is clipped into a container whose layout is:
    1px border,
    2px padding,
    [label area],
    2px spacing,
    [stretchy sub–viewport],
    2px padding,
    1px border.
  As the block shrinks, the sub–viewport shrinks first, then the spacing, then the label area,
  and finally the paddings.
- For a directory with children the block shows a “sub‐treemap” inside the sub–viewport area.
- Mousing over a block shows a tooltip with details (full path, human‐readable size,
  modification/access/creation times, owner, group, permissions, etc.).
- Double‐clicking on a directory block (in its non‐child “label” area) zooms into that folder.
  When zooming in the folder’s computed hue is used as the new base so that its color remains.
- “Go Up” shows the parent (until the originally scanned directory, when it is disabled).
- Rescanning (via Reload or a new Open) always does a full scan without “zooming.”
- The scan runs in a background thread so that the Open button becomes a Stop button
  while scanning.
"""

import os, sys, time, pwd, grp, stat, hashlib
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QToolBar,
                             QAction, QFileDialog, QStatusBar, QToolTip, QStyle)
from PyQt5.QtGui import QPainter, QColor, QFont, QPen, QIcon
from PyQt5.QtCore import Qt, QRectF, QObject, QThread, pyqtSignal, QSize

# --------- Excluded Folders ---------
# These folders are skipped (unless the user explicitly selects one as the root).
EXCLUDED_DIRS = ['/proc', '/mnt', '/sys', '/dev', '/run']

def is_excluded(path):
    """Return True if the absolute path is in (or under) one of the excluded directories."""
    abs_path = os.path.abspath(path)
    for ex in EXCLUDED_DIRS:
        if abs_path == ex or abs_path.startswith(ex + os.sep):
            return True
    return False

# --------- Utility: Compute an initial hue from a path ---------
def compute_initial_hue(path):
    """
    Compute a hue value (0–359) based on the hash of the given path.
    This ensures that different folders naturally start with different hues.
    """
    h = hashlib.md5(path.encode('utf-8')).hexdigest()
    return int(h, 16) % 360

# --------- Data Model: Node and scanning ---------

class Node:
    def __init__(self, path, name, is_dir, size=0, children=None, parent=None):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.children = children if children is not None else []
        self.parent = parent
        self.stat = None  # will hold os.stat_result
        self.hue = None   # computed hue for this node when displayed

# A custom exception to abort a scan when “Stop” is requested.
class ScanCancelledException(Exception):
    pass

def human_readable_size(size):
    """Convert a size in bytes into a human–readable string."""
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def format_tooltip(node):
    """Return a multi–line tooltip string for the given node."""
    lines = [
        f"Name: {node.name}",
        f"Path: {node.path}",
        f"Size: {human_readable_size(node.size)}"
    ]
    if node.stat:
        st = node.stat
        lines.append(f"Modified: {time.ctime(st.st_mtime)}")
        lines.append(f"Accessed: {time.ctime(st.st_atime)}")
        lines.append(f"Created: {time.ctime(st.st_ctime)}")
        try:
            uid = st.st_uid
            user = pwd.getpwuid(uid).pw_name
            lines.append(f"Owner: {user} ({uid})")
        except Exception:
            pass
        try:
            gid = st.st_gid
            group = grp.getgrgid(gid).gr_name
            lines.append(f"Group: {group} ({gid})")
        except Exception:
            pass
        try:
            perms = stat.filemode(st.st_mode)
            lines.append(f"Permissions: {perms}")
        except Exception:
            pass
    return "\n".join(lines)

def scan_directory(path, stop_callback=None, update_callback=None, parent=None):
    """
    Recursively scan the directory (or file) at “path.”
    If stop_callback() returns True, then abort by raising ScanCancelledException.
    The update_callback(path) is called at the start of scanning each file or folder.
    Parent pointers and stat info are stored in each Node.
    """
    if update_callback:
        update_callback(path)
    if stop_callback and stop_callback():
        raise ScanCancelledException()
    name = os.path.basename(path) or path
    try:
        s = os.lstat(path)
    except Exception:
        s = None
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    if is_dir and parent is not None and is_excluded(path):
        try:
            s = os.lstat(path)
        except Exception:
            s = None
        node = Node(path, name, True, 0, parent=parent)
        node.stat = s
        return node

    if is_dir:
        node = Node(path, name, True, 0, parent=parent)
        node.stat = s
        total = 0
        children = []
        try:
            for entry in os.scandir(path):
                if stop_callback and stop_callback():
                    raise ScanCancelledException()
                child = scan_directory(entry.path, stop_callback, update_callback, parent=node)
                total += child.size
                children.append(child)
        except Exception:
            pass
        node.children = children
        node.size = total
        return node
    else:
        size = s.st_size if s else 0
        node = Node(path, name, False, size, parent=parent)
        node.stat = s
        return node

# --------- Squarified Treemap Algorithm ---------

def worst_ratio(row, length):
    total = sum(row)
    if length == 0 or total == 0:
        return float('inf')
    side = total / length
    worst = 0
    for r in row:
        if r == 0:
            return float('inf')
        ratio = max(side * side / r, r / (side * side))
        worst = max(worst, ratio)
    return worst

def squarify(areas, x, y, width, height):
    """
    Partition the given rectangle (x, y, width, height) into sub–rectangles
    with areas proportional to the values in the list 'areas' using the squarify algorithm.
    """
    rects = []
    areas = areas[:]
    while areas:
        row = [areas.pop(0)]
        if width >= height:
            current_worst = worst_ratio(row, width)
            while areas and current_worst >= worst_ratio(row + [areas[0]], width):
                row.append(areas.pop(0))
                current_worst = worst_ratio(row, width)
            total_row = sum(row)
            row_height = total_row / width
            rx = x
            for r in row:
                rw = r / row_height
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
            col_width = total_row / height
            ry = y
            for r in row:
                rh = r / col_width
                rects.append((x, ry, col_width, rh))
                ry += rh
            x += col_width
            width -= col_width
    return rects

# --------- Background Scan Worker ---------

class ScanWorker(QObject):
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    error = pyqtSignal(str)
    status_update = pyqtSignal(str)
    
    def __init__(self, path):
        super().__init__()
        self.path = path
        self._stopped = False
        
    def stop(self):
        self._stopped = True
        
    def run(self):
        try:
            self.status_update.emit(f"Scanning ... {self.path}")
            result = scan_directory(self.path, 
                                    stop_callback=lambda: self._stopped,
                                    update_callback=lambda p: self.status_update.emit("Scanning: " + p))
            self.status_update.emit("Scan completed.")
            self.finished.emit(result)
        except ScanCancelledException:
            self.status_update.emit("Scan cancelled.")
            self.cancelled.emit()
        except Exception as e:
            self.status_update.emit(f"Scan error: {str(e)}")
            self.error.emit(str(e))

# --------- Treemap Widget (Viewport) ---------

class TreemapWidget(QWidget):
    zoomedIn = pyqtSignal(object)
    
    MIN_VISIBLE_AREA = 500
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_node = None
        self.current_node = None
        # The base hue stack is initialized when a new root is set.
        self.baseHueStack = []
        self.rect_map = []      # (QRectF, Node, depth)
        self.zoomable_map = []  # (full QRectF, inner QRectF, Node, depth)
        self.setMouseTracking(True)
        
    def set_root_node(self, node):
        self.root_node = node
        self.current_node = node
        self.baseHueStack = [compute_initial_hue(node.path)]
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont("Sans", 8)
        painter.setFont(font)
        self.rect_map = []
        self.zoomable_map = []
        rect = QRectF(0, 0, self.width(), self.height())
        if self.current_node:
            self.draw_node(painter, self.current_node, rect, 0)
        else:
            painter.drawText(rect, Qt.AlignCenter, "No data")
        painter.end()
        
    def draw_node(self, painter, node, rect, depth):
        if rect.width() <= 0 or rect.height() <= 0:
            return
        
        # Save rectangle for tooltip lookup.
        self.rect_map.append((QRectF(rect), node, depth))
        
        # Compute and store this node’s hue.
        base = self.baseHueStack[-1]
        hue = (base + depth * 30) % 360
        if node.hue is None:
            node.hue = hue
        col = QColor.fromHsv(node.hue, 150 if node.is_dir else 100, 220)
        painter.fillRect(rect, col)
        pen = QPen(Qt.black, 1)
        painter.setPen(pen)
        painter.drawRect(rect)  # Outer border (1px)
        
        # Define fixed horizontal borders and padding.
        left_border = 1; right_border = 1; hpad = 2
        inner_x = rect.x() + left_border + hpad
        inner_width = rect.width() - (left_border + right_border + 2 * hpad)
        
        # Vertical layout:
        # Outer vertical borders (top and bottom) are fixed (1px each).
        top_border = 1; bottom_border = 1
        inner_y = rect.y() + top_border
        inner_height = rect.height() - (top_border + bottom_border)
        
        fm = painter.fontMetrics()
        L = fm.height()  # desired label height
        
        # Ideal fixed parts: top padding = 2, label = L, spacing = 2, bottom padding = 2.
        ideal_fixed = 2 + L + 2 + 2  # = L + 6
        if inner_height >= ideal_fixed:
            top_padding = 2
            label_height = L
            spacing = 2
            bottom_padding = 2
            sub_view_height = inner_height - (L + 6)
        else:
            # Not enough room for ideal layout.
            sub_view_height = 0  # sub-viewport shrinks first.
            remaining = inner_height
            if remaining >= L + 2:
                # Allocate full label and spacing; paddings share the rest.
                label_height = L
                spacing = 2
                padding_total = remaining - (L + 2)
                top_padding = bottom_padding = padding_total / 2
            else:
                # Not enough for label+spacing; spacing goes to 0.
                spacing = 0
                if remaining >= L:
                    label_height = L
                    padding_total = remaining - L
                    top_padding = bottom_padding = padding_total / 2
                else:
                    # Everything shrinks to available height.
                    label_height = remaining
                    top_padding = bottom_padding = 0
        
        # Compute label rectangle.
        label_rect = QRectF(inner_x, inner_y + top_padding, inner_width, label_height)
        painter.save()
        painter.setClipRect(label_rect)
        elided = fm.elidedText(node.name, Qt.ElideRight, int(label_rect.width()))
        painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)
        painter.restore()
        
        # Compute sub–viewport rectangle.
        if sub_view_height > 0:
            sub_view_rect = QRectF(inner_x, inner_y + top_padding + label_height + spacing,
                                   inner_width, sub_view_height)
            # Register the full block and its inner sub–viewport for zooming.
            full_rect = QRectF(rect)
            self.zoomable_map.append((full_rect, QRectF(sub_view_rect), node, depth))
            # If this node is a directory with children, draw its children.
            if node.is_dir and node.children and inner_width > 20 and sub_view_height > 20:
                children = sorted(node.children, key=lambda n: n.size, reverse=True)
                total = sum(child.size for child in children)
                if total > 0:
                    if len(children) > 2000:
                        visible = children[:2000]
                        othersSize = sum(child.size for child in children[2000:])
                    else:
                        visible = children
                        othersSize = 0
                    visibleTotal = sum(child.size for child in visible)
                    EPSILON = 1e-6
                    visArea = sub_view_rect.width() * sub_view_rect.height()
                    if visibleTotal <= 0:
                        scaledAreas = [visArea / len(visible)] * len(visible)
                    else:
                        scaledAreas = [((child.size if child.size > 0 else EPSILON) / visibleTotal) * visArea for child in visible]
                    rects = squarify(scaledAreas, sub_view_rect.x(), sub_view_rect.y(),
                                      sub_view_rect.width(), sub_view_rect.height())
                    for child, r in zip(visible, rects):
                        childRect = QRectF(*r)
                        self.draw_node(painter, child, childRect, depth + 1)
                    # Draw an "others" block if needed.
                    if othersSize > 0 and sub_view_rect.width() > 5 and sub_view_rect.height() > 5:
                        # Partition sub_view_rect between visible and "others"
                        fraction = visibleTotal / total
                        if sub_view_rect.width() >= sub_view_rect.height():
                            visRect = QRectF(sub_view_rect.x(), sub_view_rect.y(),
                                             sub_view_rect.width(), sub_view_rect.height() * fraction)
                            othersRect = QRectF(sub_view_rect.x(), sub_view_rect.y() + sub_view_rect.height() * fraction,
                                                  sub_view_rect.width(), sub_view_rect.height() * (1 - fraction))
                        else:
                            visRect = QRectF(sub_view_rect.x(), sub_view_rect.y(),
                                             sub_view_rect.width() * fraction, sub_view_rect.height())
                            othersRect = QRectF(sub_view_rect.x() + sub_view_rect.width() * fraction, sub_view_rect.y(),
                                                  sub_view_rect.width() * (1 - fraction), sub_view_rect.height())
                        painter.fillRect(othersRect, QColor(220, 220, 220))
                        painter.setPen(QPen(Qt.black, 1))
                        painter.drawRect(othersRect)
                        elided_others = fm.elidedText("others", Qt.ElideRight, int(othersRect.width() - 4))
                        painter.drawText(othersRect.adjusted(2, 2, -2, -2), Qt.AlignLeft | Qt.AlignVCenter, elided_others)
        # End draw_node
        
    def mouseMoveEvent(self, event):
        pos = event.pos()
        target = None
        max_depth = -1
        for rect, node, depth in self.rect_map:
            if rect.contains(pos) and depth >= max_depth:
                target = node
                max_depth = depth
        if target:
            QToolTip.showText(self.mapToGlobal(event.pos()), format_tooltip(target), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)
        
    def mouseDoubleClickEvent(self, event):
        pos = event.pos()
        target = None
        max_depth = -1
        selected_depth = 0
        for full_rect, inner_rect, node, depth in self.zoomable_map:
            if full_rect.contains(pos) and not inner_rect.contains(pos) and depth >= max_depth:
                target = node
                max_depth = depth
                selected_depth = depth
        if target and target.is_dir and target.children:
            # Use the target's computed hue as the new base.
            new_baseHue = target.hue if target.hue is not None else (self.baseHueStack[-1] + selected_depth * 30) % 360
            self.baseHueStack.append(new_baseHue)
            self.current_node = target
            self.zoomedIn.emit(target)
            self.update()
        super().mouseDoubleClickEvent(event)
        
    def go_up(self):
        if self.current_node and self.current_node.parent:
            self.current_node = self.current_node.parent
            if len(self.baseHueStack) > 1:
                self.baseHueStack.pop()
            self.update()
            self.zoomedIn.emit(self.current_node)
            
    def go_top(self):
        if self.root_node:
            self.current_node = self.root_node
            self.baseHueStack = [compute_initial_hue(self.root_node.path)]
            self.update()
            self.zoomedIn.emit(self.current_node)

# --------- Main Window with Toolbar and Status Bar ---------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Directory Treemap")
        self.resize(800, 600)
        
        self.treemapWidget = TreemapWidget()
        self.setCentralWidget(self.treemapWidget)
        
        self.toolbar = QToolBar("Main Toolbar")
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(self.toolbar)
        
        style = self.style()
        self.openAction = QAction(style.standardIcon(QStyle.SP_DialogOpenButton), "Open", self)
        self.openAction.triggered.connect(self.open_or_stop)
        self.toolbar.addAction(self.openAction)
        
        self.reloadAction = QAction(style.standardIcon(QStyle.SP_BrowserReload), "Reload", self)
        self.reloadAction.triggered.connect(self.reload_directory)
        self.toolbar.addAction(self.reloadAction)
        
        self.goTopAction = QAction(style.standardIcon(QStyle.SP_DesktopIcon), "Go Top", self)
        self.goTopAction.triggered.connect(self.go_top)
        self.toolbar.addAction(self.goTopAction)
        
        self.goUpAction = QAction(style.standardIcon(QStyle.SP_ArrowUp), "Go Up", self)
        self.goUpAction.triggered.connect(self.go_up)
        self.toolbar.addAction(self.goUpAction)
        
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        
        self.loaded_directory = None
        self.scan_thread = None
        self.scan_worker = None
        self.scanning = False
        
        self.reloadAction.setEnabled(False)
        self.goTopAction.setEnabled(False)
        self.goUpAction.setEnabled(False)
        
        self.treemapWidget.zoomedIn.connect(self.update_navigation_buttons)
        
    def open_or_stop(self):
        if self.scanning:
            if self.scan_worker:
                self.scan_worker.stop()
                self.statusBar.showMessage("Stopping scan...")
        else:
            directory = QFileDialog.getExistingDirectory(self, "Select Directory", os.getcwd())
            if directory:
                self.start_scan(directory)
                
    def start_scan(self, directory):
        self.treemapWidget.root_node = None
        self.treemapWidget.current_node = None
        self.treemapWidget.update()
        self.loaded_directory = directory
        self.statusBar.showMessage(f"Scanning ... {directory}")
        self.reloadAction.setEnabled(False)
        self.goTopAction.setEnabled(False)
        self.goUpAction.setEnabled(False)
        
        self.openAction.setText("Stop")
        self.scanning = True
        
        self.scan_thread = QThread()
        self.scan_worker = ScanWorker(directory)
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(self.scan_finished)
        self.scan_worker.cancelled.connect(self.scan_cancelled)
        self.scan_worker.error.connect(self.scan_error)
        self.scan_worker.status_update.connect(self.statusBar.showMessage)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.start()
        
    def scan_finished(self, root_node):
        self.scanning = False
        self.openAction.setText("Open")
        self.reloadAction.setEnabled(True)
        self.treemapWidget.set_root_node(root_node)
        self.update_navigation_buttons(self.treemapWidget.current_node)
        
    def scan_cancelled(self):
        self.scanning = False
        self.openAction.setText("Open")
        self.reloadAction.setEnabled(self.loaded_directory is not None)
        self.statusBar.showMessage("Scan cancelled.")
        
    def scan_error(self, error_msg):
        self.scanning = False
        self.openAction.setText("Open")
        self.reloadAction.setEnabled(self.loaded_directory is not None)
        self.statusBar.showMessage(f"Scan error: {error_msg}")
        
    def reload_directory(self):
        if self.loaded_directory and not self.scanning:
            self.start_scan(self.loaded_directory)
            
    def go_top(self):
        self.treemapWidget.go_top()
        self.update_navigation_buttons(self.treemapWidget.current_node)
        
    def go_up(self):
        self.treemapWidget.go_up()
        self.update_navigation_buttons(self.treemapWidget.current_node)
        
    def update_navigation_buttons(self, current_node):
        if current_node is None or current_node == self.treemapWidget.root_node:
            self.goUpAction.setEnabled(False)
            self.goTopAction.setEnabled(False)
        else:
            self.goUpAction.setEnabled(True)
            self.goTopAction.setEnabled(True)

# --------- Main Entry Point ---------

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    if len(sys.argv) > 1:
        directory = sys.argv[1]
        if os.path.exists(directory) and os.path.isdir(directory):
            window.start_scan(directory)
        else:
            window.statusBar.showMessage("Invalid directory provided.")
    else:
        window.statusBar.showMessage("Please open a directory")
    window.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
