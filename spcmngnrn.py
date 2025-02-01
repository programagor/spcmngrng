#!/usr/bin/env python3
"""
A Directory Treemap Viewer inspired by SpaceMonger.

Features:
- A top toolbar with buttons (with icons):
    • Open (which turns to Stop while scanning),
    • Reload (rescans the originally loaded directory),
    • Go Top and Go Up (to navigate the scanned tree).
- A status bar that shows messages such as “Please open a directory”, 
  and during scanning it shows the actual file/folder currently being processed.
- A viewport (the central widget) that displays the treemap.
- The treemap displays directories and files as rectangles whose areas are proportional
  to file sizes. The layout uses a squarified algorithm.
- Each block shows a truncated filename label.
- For a directory with children the block shows a “sub‐treemap” inside a reserved inner area.
- Mousing over a block shows a tooltip with details (full path, human‐readable size,
  modification/access/creation times, owner, group, permissions, etc.).
- Double–clicking on a directory block (in its non‐child “label” area) zooms into that folder.
- “Go Up” shows the parent (until the originally scanned directory, when it is disabled).
- Rescanning (via Reload or a new Open) always does a full scan without “zooming.”
- The scan runs in a background thread so that the Open button becomes a Stop button
  while scanning.
"""

import os, sys, time, pwd, grp, stat
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QToolBar,
                             QAction, QFileDialog, QStatusBar, QToolTip, QStyle)
from PyQt5.QtGui import QPainter, QColor, QFont, QPen, QIcon
from PyQt5.QtCore import Qt, QRectF, QObject, QThread, pyqtSignal

# --------- Excluded Folders ---------
# These folders are skipped (unless the user explicitly selects one as the root).
EXCLUDED_DIRS = ['/proc', '/mnt', '/sys', '/dev', '/run']

def is_excluded(path):
    """Return True if the absolute path is in (or under) one of the excluded directories."""
    abs_path = os.path.abspath(path)
    for ex in EXCLUDED_DIRS:
        # If the folder is exactly excluded or is a sub–folder (and not the root of the scan)
        if abs_path == ex or abs_path.startswith(ex + os.sep):
            return True
    return False

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
    lines = []
    lines.append(f"Name: {node.name}")
    lines.append(f"Path: {node.path}")
    lines.append(f"Size: {human_readable_size(node.size)}")
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
    # Determine if this is a directory (and not a symlink)
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    # If this is a directory (and not the root scan), check for problematic folders.
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
    Given a list of areas and a rectangle (x,y,width,height),
    partition the rectangle into sub–rectangles with areas proportional
    to the input areas using the squarify algorithm.
    """
    rects = []
    areas = areas[:]  # copy list
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
    finished = pyqtSignal(object)   # emits the root Node on success
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
            # Pass an update_callback that sends the currently scanned path.
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
    # Signal emitted when the view “zooms” (i.e. current_node changes).
    zoomedIn = pyqtSignal(object)  # emits the new current node
    
    MIN_VISIBLE_AREA = 500  # (unused threshold constant; you may adjust if desired)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_node = None  # the top–level scanned Node
        self.current_node = None  # the node that is currently being viewed (may be a subfolder)
        self.baseHue = 200  # base hue for coloring; will be shifted with nesting depth
        # These lists are rebuilt at every paintEvent to map drawn rectangles to nodes.
        self.rect_map = []      # list of tuples: (QRectF, Node, depth)
        self.zoomable_map = []  # list of tuples: (full QRectF, inner QRectF, Node, depth)
        self.setMouseTracking(True)
        
    def set_root_node(self, node):
        self.root_node = node
        self.current_node = node
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont("Sans", 8)
        painter.setFont(font)
        # Clear any previous mapping.
        self.rect_map = []
        self.zoomable_map = []
        rect = QRectF(0, 0, self.width(), self.height())
        if self.current_node:
            self.draw_node(painter, self.current_node, rect, depth=0)
        else:
            painter.drawText(rect, Qt.AlignCenter, "No data")
        painter.end()
        
    def draw_node(self, painter, node, rect, depth):
        if rect.width() <= 0 or rect.height() <= 0:
            return
        # Save this node’s full rectangle for tooltip lookup.
        self.rect_map.append((QRectF(rect), node, depth))
        
        # Choose a background color based on depth.
        hue = (self.baseHue + depth * 30) % 360
        sat = 150 if node.is_dir else 100
        col = QColor.fromHsv(hue, sat, 220)
        painter.fillRect(rect, col)
        pen = QPen(Qt.black, 1)
        painter.setPen(pen)
        painter.drawRect(rect)
        
        margin = 2
        fm = painter.fontMetrics()
        # Draw the label at the top.
        labelRect = QRectF(rect.x() + margin, rect.y() + margin,
                           rect.width() - 2 * margin, fm.height())
        elided = fm.elidedText(node.name, Qt.ElideRight, int(labelRect.width()))
        painter.drawText(labelRect, Qt.AlignLeft | Qt.AlignVCenter, elided)
        
        # If this is a directory with children and there is enough room,
        # reserve an inner rectangle (below the label) for the sub–treemap.
        if node.is_dir and node.children and rect.width() > 30 and rect.height() > (fm.height() + 10):
            inner = QRectF(rect.x() + margin, rect.y() + fm.height() + margin,
                           rect.width() - 2 * margin, rect.height() - fm.height() - 2 * margin)
            if inner.width() < 20 or inner.height() < 20:
                return
            
            # For zooming purposes, we want to allow “double–click to zoom”
            # anywhere in the parent’s block except in the area occupied by the inner sub–treemap.
            # (We save the full rectangle and inner rectangle so that later we can decide if
            # the double–click happened in the “non–child” region.)
            self.zoomable_map.append((QRectF(rect), QRectF(inner), node, depth))
            
            # Sort children by size (largest first) and implement the “top 2000 items” rule.
            children = sorted(node.children, key=lambda n: n.size, reverse=True)
            total = sum(child.size for child in children)
            if total <= 0:
                return
            if len(children) > 2000:
                visible = children[:2000]
                othersSize = sum(child.size for child in children[2000:])
            else:
                visible = children
                othersSize = 0
            visibleTotal = sum(child.size for child in visible)
            fraction = visibleTotal / total
            # Partition the inner area between the visible items and an “others” block.
            if inner.width() >= inner.height():
                visRect = QRectF(inner.x(), inner.y(), inner.width(), inner.height() * fraction)
                othersRect = QRectF(inner.x(), inner.y() + inner.height() * fraction,
                                     inner.width(), inner.height() * (1 - fraction))
            else:
                visRect = QRectF(inner.x(), inner.y(), inner.width() * fraction, inner.height())
                othersRect = QRectF(inner.x() + inner.width() * fraction, inner.y(),
                                     inner.width() * (1 - fraction), inner.height())
            # Layout the visible children using the squarify algorithm.
            if visible:
                visArea = visRect.width() * visRect.height()
                EPSILON = 1e-6
                if visibleTotal <= 0:
                    scaledAreas = [visArea / len(visible)] * len(visible)
                else:
                    scaledAreas = [((child.size if child.size > 0 else EPSILON) / visibleTotal) * visArea for child in visible]
                rects = squarify(scaledAreas, visRect.x(), visRect.y(), visRect.width(), visRect.height())
                for child, r in zip(visible, rects):
                    childRect = QRectF(*r)
                    self.draw_node(painter, child, childRect, depth + 1)
            # Draw an “others” block if needed.
            if othersSize > 0 and othersRect.width() > 5 and othersRect.height() > 5:
                painter.fillRect(othersRect, QColor(220, 220, 220))
                painter.setPen(QPen(Qt.black, 1))
                painter.drawRect(othersRect)
                othersLabel = "others"
                elided = fm.elidedText(othersLabel, Qt.ElideRight, int(othersRect.width() - 4))
                painter.drawText(int(othersRect.x() + 2),
                                 int(othersRect.y() + fm.ascent() + 2),
                                 elided)
    
    def mouseMoveEvent(self, event):
        pos = event.pos()
        # Find the deepest node (largest depth) whose drawn rectangle contains pos.
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
        # Look for a directory whose “zoomable” area (i.e. the parent’s full rectangle minus
        # its inner sub–treemap area) contains the click.
        target = None
        max_depth = -1
        for full_rect, inner_rect, node, depth in self.zoomable_map:
            if full_rect.contains(pos) and not inner_rect.contains(pos) and depth >= max_depth:
                target = node
                max_depth = depth
        if target and target.is_dir and target.children:
            self.current_node = target
            self.zoomedIn.emit(target)
            self.update()
        super().mouseDoubleClickEvent(event)
        
    def go_up(self):
        """Set the view to the parent directory (if any) and update."""
        if self.current_node and self.current_node.parent:
            self.current_node = self.current_node.parent
            self.update()
            self.zoomedIn.emit(self.current_node)
            
    def go_top(self):
        """Return to the top–level (originally scanned) directory."""
        if self.root_node:
            self.current_node = self.root_node
            self.update()
            self.zoomedIn.emit(self.current_node)

# --------- Main Window with Toolbar and Status Bar ---------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Directory Treemap")
        self.resize(800, 600)
        
        # Create the central treemap widget.
        self.treemapWidget = TreemapWidget()
        self.setCentralWidget(self.treemapWidget)
        
        # Create a top toolbar.
        self.toolbar = QToolBar("Main Toolbar")
        self.addToolBar(self.toolbar)
        
        # Use the application style for standard icons.
        style = self.style()
        self.openAction = QAction(style.standardIcon(QStyle.SP_DirOpenIcon), "Open", self)
        self.openAction.triggered.connect(self.open_or_stop)
        self.toolbar.addAction(self.openAction)
        
        self.reloadAction = QAction(style.standardIcon(QStyle.SP_BrowserReload), "Reload", self)
        self.reloadAction.triggered.connect(self.reload_directory)
        self.toolbar.addAction(self.reloadAction)
        
        self.goTopAction = QAction(style.standardIcon(QStyle.SP_DirHomeIcon), "Go Top", self)
        self.goTopAction.triggered.connect(self.go_top)
        self.toolbar.addAction(self.goTopAction)
        
        self.goUpAction = QAction(style.standardIcon(QStyle.SP_ArrowUp), "Go Up", self)
        self.goUpAction.triggered.connect(self.go_up)
        self.toolbar.addAction(self.goUpAction)
        
        # Create the status bar.
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        
        self.loaded_directory = None  # The directory that was opened.
        self.scan_thread = None
        self.scan_worker = None
        self.scanning = False
        
        # Initially disable navigation and reload buttons.
        self.reloadAction.setEnabled(False)
        self.goTopAction.setEnabled(False)
        self.goUpAction.setEnabled(False)
        
        # When the treemap widget “zooms in” (or out), update the navigation buttons.
        self.treemapWidget.zoomedIn.connect(self.update_navigation_buttons)
        
    def open_or_stop(self):
        if self.scanning:
            # If scanning is in progress, stop it.
            if self.scan_worker:
                self.scan_worker.stop()
                self.statusBar.showMessage("Stopping scan...")
        else:
            # Otherwise, open a directory chooser.
            directory = QFileDialog.getExistingDirectory(self, "Select Directory", os.getcwd())
            if directory:
                self.start_scan(directory)
                
    def start_scan(self, directory):
        # Clear the current view.
        self.treemapWidget.root_node = None
        self.treemapWidget.current_node = None
        self.treemapWidget.update()
        self.loaded_directory = directory
        self.statusBar.showMessage(f"Scanning ... {directory}")
        self.reloadAction.setEnabled(False)
        self.goTopAction.setEnabled(False)
        self.goUpAction.setEnabled(False)
        
        # Change the Open button to “Stop.”
        self.openAction.setText("Stop")
        self.scanning = True
        
        # Set up the background worker and thread.
        self.scan_thread = QThread()
        self.scan_worker = ScanWorker(directory)
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(self.scan_finished)
        self.scan_worker.cancelled.connect(self.scan_cancelled)
        self.scan_worker.error.connect(self.scan_error)
        self.scan_worker.status_update.connect(self.statusBar.showMessage)
        # When done, clean up the thread.
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
        """Rescan the originally opened directory (without changing any zoom state)."""
        if self.loaded_directory and not self.scanning:
            self.start_scan(self.loaded_directory)
            
    def go_top(self):
        self.treemapWidget.go_top()
        self.update_navigation_buttons(self.treemapWidget.current_node)
        
    def go_up(self):
        self.treemapWidget.go_up()
        self.update_navigation_buttons(self.treemapWidget.current_node)
        
    def update_navigation_buttons(self, current_node):
        # If we’re at the root of the scanned directory, disable Go Up/Top.
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
    # If a directory was provided as a command–line argument, start scanning immediately.
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

