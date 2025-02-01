#!/usr/bin/env python3
"""
A treemap viewer for directories – inspired by SpaceMonger.

Features:
  - A toolbar at the top with:
      • Open: Let the user select a new directory (scan occurs then)
      • Reload: Re-scan the currently selected base directory
      • Go Up: Zoom out one level (if available)
      • Go Top: Return to the originally scanned (base) directory
  - The main viewport shows a treemap of the current directory node.
  - Hovering over a block shows a tooltip with details (full path, size,
    times, owner/group, permissions, etc.).
  - Double-clicking the label area of a directory block “zooms in” to that directory.
  - Navigation buttons are disabled if you are at the base directory or if no directory is open.
  
Rescans occur only when the user clicks “Open” or “Reload.”
"""

import os, sys, stat, pwd, grp, datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QFileDialog,
                             QToolBar, QAction, QVBoxLayout, QStyle)
from PyQt5.QtGui import QPainter, QColor, QFont, QPen
from PyQt5.QtCore import Qt, QRectF, QPoint, pyqtSignal

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

def scan_directory(path, parent=None):
    """
    Recursively scan a directory (or file) and return a Node.
    The directory size is the sum of its children.
    """
    name = os.path.basename(path) or path
    if os.path.isfile(path):
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        return Node(path, name, False, size, parent=parent)
    elif os.path.isdir(path):
        total = 0
        children = []
        try:
            for entry in os.scandir(path):
                child = scan_directory(entry.path, parent=None)  # assign parent later
                total += child.size
                children.append(child)
        except Exception:
            pass
        node = Node(path, name, True, total, children, parent=parent)
        for child in children:
            child.parent = node
        return node
    else:
        return Node(path, name, False, 0, parent=parent)

# --------------- Squarified Treemap Algorithm ---------------

def worst_ratio(row, length):
    total = sum(row)
    side = total / length
    return max(max(side * side / r, r / (side * side)) for r in row)

def squarify(areas, x, y, width, height):
    """
    Given a list of areas and a rectangle (x,y,width,height),
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

# --------------- Treemap Widget ---------------

class TreemapWidget(QWidget):
    # Signal to notify that the current node has changed (for updating title, etc.)
    nodeChanged = pyqtSignal(object)

    def __init__(self, root_node=None, parent=None):
        super().__init__(parent)
        # The base node (the scanned directory). May be None initially.
        self.root_node = root_node
        # The current node whose contents are shown.
        self.current_node = root_node
        self.baseHue = 200  # starting hue; rotates with depth
        # We'll store two lists for hit-testing:
        # 1. _node_rects: (QRectF, Node) for every drawn block.
        # 2. _label_rects: (QRectF, Node) for the label area of directory blocks.
        self._node_rects = []
        self._label_rects = []
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(QFont("Sans", 8))
        rect = QRectF(0, 0, self.width(), self.height())
        # Clear previous hit-test info.
        self._node_rects = []
        self._label_rects = []
        if self.current_node is not None:
            self.draw_node(painter, self.current_node, rect, depth=0)
        painter.end()

    def draw_node(self, painter, node, rect, depth):
        """
        Recursively draw a node (file or directory) in the given rect.
        For directories, record the label area for double-click zooming.
        """
        if rect.width() <= 0 or rect.height() <= 0:
            return

        # Record this rectangle with its node for generic hit-testing.
        self._node_rects.append((QRectF(rect), node))

        # Choose a color based on depth (rotate hue by 30° per level)
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

        # If the node is a directory, record its label area for double-click zooming.
        if node.is_dir:
            self._label_rects.append((QRectF(labelRect), node))

        # If this node is a directory with children and there’s room, draw its treemap.
        if node.is_dir and node.children and rect.width() > 30 and rect.height() > (fm.height() + 10):
            inner = QRectF(rect.x() + margin, rect.y() + fm.height() + margin,
                           rect.width() - 2 * margin, rect.height() - fm.height() - 2 * margin)
            if inner.width() < 20 or inner.height() < 20:
                return

            # Use all children (sorted by size descending) with no grouping.
            children = sorted(node.children, key=lambda n: n.size, reverse=True)
            total = sum(child.size for child in children)
            if total <= 0:
                return

            # Layout all children into the entire inner area.
            innerArea = inner.width() * inner.height()
            # Scale each child's area.
            scaledAreas = [child.size / total * innerArea for child in children]
            rects = squarify(scaledAreas, inner.x(), inner.y(), inner.width(), inner.height())
            for child, r in zip(children, rects):
                childRect = QRectF(*r)
                self.draw_node(painter, child, childRect, depth + 1)

    def mouseMoveEvent(self, event):
        """
        On mouse move, check if the cursor is over any block.
        If so, show a tooltip with details about that node.
        """
        pos = event.pos()
        hit_node = None
        # Use the smallest rectangle that contains the point.
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
        """
        When the user double-clicks, check whether the click is on a directory's label area.
        If so, zoom into that directory.
        """
        pos = event.pos()
        hit_node = None
        # Check label areas first.
        for rect, node in self._label_rects:
            if rect.contains(pos):
                hit_node = node
                break
        # If found and it is a directory, then zoom in.
        if hit_node and hit_node.is_dir:
            self.current_node = hit_node
            self.nodeChanged.emit(self.current_node)
            self.update()
        else:
            super().mouseDoubleClickEvent(event)

    def reload_current(self):
        """
        Re-scan the base directory and update the current view (if possible).
        """
        if self.root_node is None:
            return
        new_root = scan_directory(self.root_node.path)
        self.root_node = new_root
        # Try to find the current node in the new tree.
        def find_node(node, path):
            if node.path == path:
                return node
            if node.is_dir:
                for child in node.children:
                    res = find_node(child, path)
                    if res:
                        return res
            return None
        new_current = find_node(new_root, self.current_node.path) if self.current_node else None
        if new_current:
            self.current_node = new_current
        else:
            self.current_node = new_root
        self.nodeChanged.emit(self.current_node)
        self.update()

    def go_up(self):
        """
        Set the current node to its parent, if available.
        """
        if self.current_node and self.current_node.parent is not None:
            self.current_node = self.current_node.parent
            self.nodeChanged.emit(self.current_node)
            self.update()

    def go_top(self):
        """
        Return to the base (originally scanned) directory.
        """
        if self.root_node and self.current_node != self.root_node:
            self.current_node = self.root_node
            self.nodeChanged.emit(self.current_node)
            self.update()

# --------------- Main Window ---------------

class MainWindow(QMainWindow):
    def __init__(self, root_node=None, parent=None):
        super().__init__(parent)
        # Initially, if no directory was provided, show an empty viewport.
        self.setWindowTitle("Treemap")
        self.treemap = TreemapWidget(root_node)
        self.treemap.nodeChanged.connect(self.on_node_changed)

        central = QWidget()
        layout = QVBoxLayout(central)
        # Create a toolbar and add it at the top.
        self.toolbar = QToolBar()
        layout.addWidget(self.toolbar)
        layout.addWidget(self.treemap)
        self.setCentralWidget(central)
        self.create_actions()
        self.update_actions()

    def create_actions(self):
        self.open_action = QAction("Open", self)
        self.open_action.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.open_action.triggered.connect(self.open_directory)
        self.toolbar.addAction(self.open_action)

        self.reload_action = QAction("Reload", self)
        self.reload_action.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.reload_action.triggered.connect(self.treemap.reload_current)
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
        """
        Enable or disable actions based on the current node.
        If no directory is loaded or if at the base directory,
        disable Reload, Go Up and Go Top.
        """
        node = self.treemap.current_node
        has_node = node is not None
        self.reload_action.setEnabled(has_node)
        # For "Go Up": disable if there is no parent.
        self.up_action.setEnabled(has_node and node.parent is not None)
        # For "Go Top": disable if current node is the base.
        self.top_action.setEnabled(has_node and (node != self.treemap.root_node))

    def open_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory", os.path.expanduser("~"))
        if directory:
            new_root = scan_directory(directory)
            self.treemap.root_node = new_root
            self.treemap.current_node = new_root
            self.treemap.update()
            self.setWindowTitle(f"Treemap: {directory}")
            self.update_actions()

    def on_node_changed(self, node):
        self.setWindowTitle(f"Treemap: {node.path}")
        self.update_actions()

# --------------- Main Entry Point ---------------

def main():
    # If an argument is given, scan that directory.
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if not os.path.exists(target):
            print("Path does not exist:", target)
            sys.exit(1)
        print("Scanning", target, "…")
        root_node = scan_directory(target)
    else:
        root_node = None  # start with an empty viewport

    app = QApplication(sys.argv)
    win = MainWindow(root_node)
    win.resize(1000, 700)
    win.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
