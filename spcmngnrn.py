#!/usr/bin/env python3
"""
A treemap viewer for directories – inspired by SpaceMonger.

Features:
  - A toolbar at the top with:
      • Open: Let the user select a new directory (scan occurs here)
      • Reload: Rescan the currently selected (base) directory
      • Go Up: Zoom out one level (if possible)
      • Go Top: Return to the originally scanned (base) directory
  - The viewport shows a treemap of the current directory node.
  - Hovering over a block shows a tooltip with details (full path, size, times, owner/group, permissions).
  - Double-clicking a directory block zooms into it (viewport replaces with that directory’s contents),
    and the window title is updated accordingly.
    
Rescans occur only when “Open” or “Reload” is clicked.
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
    return (f"Size: {size} bytes ({human_readable_size(size)})\n"
            f"Created: {ctime}\nModified: {mtime}\nAccessed: {atime}\n"
            f"Owner: {owner}  Group: {group}\nPermissions: {perms}")

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
    Recursively scan a directory (or a file) and return a Node.
    Directories have a size equal to the sum of their children.
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
                child = scan_directory(entry.path, parent=None)  # assign parent below
                total += child.size
                child.parent = None  # will set later
                children.append(child)
        except Exception:
            pass
        node = Node(path, name, True, total, children, parent=parent)
        # Now update child's parent pointer
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
    return a list of rectangles (tuples (x, y, w, h)) whose areas
    are proportional to the given areas.
    """
    rects = []
    areas = areas[:]  # copy
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
    # Signal to notify the main window that the current node changed (e.g., via double-click)
    nodeChanged = pyqtSignal(object)

    MIN_VISIBLE_AREA = 500  # minimum area in pixels^2 for an individual block

    def __init__(self, root_node, parent=None):
        super().__init__(parent)
        # The originally scanned node (base directory)
        self.root_node = root_node
        # The current node whose contents we are displaying
        self.current_node = root_node
        self.baseHue = 200  # starting hue; rotates with depth
        # We'll build a list of drawn blocks as tuples: (QRectF, Node)
        self._node_rects = []
        # Enable mouse tracking so that hover events occur without pressing a button.
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont("Sans", 8)
        painter.setFont(font)
        rect = QRectF(0, 0, self.width(), self.height())
        self._node_rects = []  # reset the mapping of screen rects to nodes
        self.draw_node(painter, self.current_node, rect, depth=0)
        painter.end()

    def draw_node(self, painter, node, rect, depth):
        """
        Recursively draw a node in the given rect.
        Records each drawn block in self._node_rects for hit-testing.
        """
        if rect.width() <= 0 or rect.height() <= 0:
            return

        # Save this rectangle with its node.
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

        # If this node is a directory with children, and there's room, draw its sub-treemap.
        if node.is_dir and node.children and rect.width() > 30 and rect.height() > (fm.height() + 10):
            inner = QRectF(rect.x() + margin, rect.y() + fm.height() + margin,
                           rect.width() - 2 * margin, rect.height() - fm.height() - 2 * margin)
            if inner.width() < 20 or inner.height() < 20:
                return

            # Sort children by size descending.
            children = sorted(node.children, key=lambda n: n.size, reverse=True)
            total = sum(child.size for child in children)
            if total <= 0:
                return

            visible = []
            othersSize = 0
            innerArea = inner.width() * inner.height()
            for child in children:
                child_area = (child.size / total) * innerArea
                if child_area < self.MIN_VISIBLE_AREA:
                    othersSize += child.size
                else:
                    visible.append(child)

            visibleTotal = sum(child.size for child in visible)
            fraction = visibleTotal / total  # fraction of inner area for visible children
            if inner.width() >= inner.height():
                visRect = QRectF(inner.x(), inner.y(), inner.width(), inner.height() * fraction)
                othersRect = QRectF(inner.x(), inner.y() + inner.height() * fraction,
                                     inner.width(), inner.height() * (1 - fraction))
            else:
                visRect = QRectF(inner.x(), inner.y(), inner.width() * fraction, inner.height())
                othersRect = QRectF(inner.x() + inner.width() * fraction, inner.y(),
                                     inner.width() * (1 - fraction), inner.height())

            if visible:
                visArea = visRect.width() * visRect.height()
                scaledAreas = [child.size / visibleTotal * visArea for child in visible]
                rects = squarify(scaledAreas, visRect.x(), visRect.y(), visRect.width(), visRect.height())
                for child, r in zip(visible, rects):
                    childRect = QRectF(*r)
                    self.draw_node(painter, child, childRect, depth + 1)

            if othersSize > 0 and othersRect.width() > 5 and othersRect.height() > 5:
                painter.fillRect(othersRect, QColor(220, 220, 220))
                painter.setPen(QPen(Qt.black, 1))
                painter.drawRect(othersRect)
                othersLabel = "others"
                elidedOthers = fm.elidedText(othersLabel, Qt.ElideRight, int(othersRect.width() - 4))
                painter.drawText(int(othersRect.x() + 2),
                                 int(othersRect.y() + fm.ascent() + 2),
                                 elidedOthers)

    def mouseMoveEvent(self, event):
        """
        On mouse move, check if the cursor is over any block. If so, show a tooltip
        with details about that node.
        """
        pos = event.pos()
        hit_node = None
        hit_rect = None
        # Choose the smallest rectangle (most detailed) that contains the point.
        for rect, node in self._node_rects:
            if rect.contains(pos):
                if hit_rect is None or rect.width() * rect.height() < hit_rect.width() * hit_rect.height():
                    hit_rect = rect
                    hit_node = node
        if hit_node:
            tip = f"<b>{hit_node.name}</b><br>{hit_node.path}<br>"
            tip += f"Total size: {hit_node.size} bytes ({human_readable_size(hit_node.size)})<br>"
            tip += format_stat(hit_node.path).replace("\n", "<br>")
            self.setToolTip(tip)
        else:
            self.setToolTip("")
        # Call the base class for default handling.
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        """
        On double-click, if over a directory block, zoom into that directory.
        """
        pos = event.pos()
        hit_node = None
        for rect, node in self._node_rects:
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
        """
        Re-scan the original base directory and update the current node if it is still valid.
        """
        new_root = scan_directory(self.root_node.path)
        self.root_node = new_root
        # Attempt to find a node matching the current node's path in the new tree.
        def find_node(node, path):
            if node.path == path:
                return node
            if node.is_dir:
                for child in node.children:
                    res = find_node(child, path)
                    if res:
                        return res
            return None
        new_current = find_node(new_root, self.current_node.path)
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
        if self.current_node.parent is not None:
            self.current_node = self.current_node.parent
            self.nodeChanged.emit(self.current_node)
            self.update()

    def go_top(self):
        """
        Go back to the originally scanned base directory.
        """
        self.current_node = self.root_node
        self.nodeChanged.emit(self.current_node)
        self.update()

# --------------- Main Window ---------------

class MainWindow(QMainWindow):
    def __init__(self, root_node, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Treemap: {root_node.path}")
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

    def create_actions(self):
        # "Open" button.
        open_action = QAction("Open", self)
        open_action.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        open_action.triggered.connect(self.open_directory)
        self.toolbar.addAction(open_action)

        # "Reload" button.
        reload_action = QAction("Reload", self)
        reload_action.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        reload_action.triggered.connect(self.treemap.reload_current)
        self.toolbar.addAction(reload_action)

        # "Go Up" button.
        up_action = QAction("Go Up", self)
        up_action.setIcon(self.style().standardIcon(QStyle.SP_ArrowUp))
        up_action.triggered.connect(self.treemap.go_up)
        self.toolbar.addAction(up_action)

        # "Go Top" button.
        top_action = QAction("Go Top", self)
        top_action.setIcon(self.style().standardIcon(QStyle.SP_ArrowBack))
        top_action.triggered.connect(self.treemap.go_top)
        self.toolbar.addAction(top_action)

    def open_directory(self):
        # Open a QFileDialog to let the user select a directory.
        directory = QFileDialog.getExistingDirectory(self, "Select Directory", os.path.expanduser("~"))
        if directory:
            new_root = scan_directory(directory)
            self.treemap.root_node = new_root
            self.treemap.current_node = new_root
            self.treemap.update()
            self.setWindowTitle(f"Treemap: {directory}")

    def on_node_changed(self, node):
        """
        Slot to update the window title when the current node changes.
        """
        self.setWindowTitle(f"Treemap: {node.path}")

# --------------- Main Entry Point ---------------

def main():
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = os.path.expanduser("~")
    if not os.path.exists(target):
        print("Path does not exist:", target)
        sys.exit(1)
    print("Scanning", target, "…")
    root_node = scan_directory(target)
    app = QApplication(sys.argv)
    win = MainWindow(root_node)
    win.resize(1000, 700)
    win.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
