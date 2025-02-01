#!/usr/bin/env python3
"""
A treemap viewer for directories – inspired by SpaceMonger.
Each directory’s content is laid out as rectangles whose areas
are proportional to file sizes. Labels are drawn on each block.
Small items (whose area would be too small to show a label)
are merged into an “others” block (filled with gray).
The hue is rotated with nesting depth.
It recalculates the layout on window resize.
"""

import os, sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget
from PyQt5.QtGui import QPainter, QColor, QFont, QPen
from PyQt5.QtCore import Qt, QRectF

# ---------------- Data Model ------------------

class Node:
    def __init__(self, path, name, is_dir, size=0, children=None):
        self.path = path
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.children = children if children is not None else []


def scan_directory(path):
    """
    Recursively scan a directory (or a file) and return a Node
    whose size is the file size (or the sum of children for a directory).
    """
    name = os.path.basename(path) or path
    if os.path.isfile(path):
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        return Node(path, name, False, size)
    elif os.path.isdir(path):
        total = 0
        children = []
        try:
            for entry in os.scandir(path):
                child = scan_directory(entry.path)
                total += child.size
                children.append(child)
        except Exception:
            pass
        return Node(path, name, True, total, children)
    else:
        return Node(path, name, False, 0)


# --------------- Squarified Treemap Algorithm ---------------

def worst_ratio(row, length):
    """
    Given a row of areas and the current side length,
    compute the “worst” aspect ratio.
    """
    total = sum(row)
    side = total / length
    return max(max(side * side / r, r / (side * side)) for r in row)


def squarify(areas, x, y, width, height):
    """
    Given a list of areas and a rectangle (x,y,width,height),
    return a list of rectangles (as tuples (x, y, w, h)) that partition
    the given rectangle with areas proportional to areas.
    
    Implements the “squarify” algorithm.
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


# --------------- Treemap Widget ---------------

class TreemapWidget(QWidget):
    MIN_VISIBLE_AREA = 500  # Minimum area (pixels^2) for showing an individual block

    def __init__(self, root_node, parent=None):
        super().__init__(parent)
        self.root_node = root_node
        self.baseHue = 200  # starting hue; will be rotated with depth

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont("Sans", 8)
        painter.setFont(font)
        rect = QRectF(0, 0, self.width(), self.height())
        self.draw_node(painter, self.root_node, rect, depth=0)
        painter.end()

    def draw_node(self, painter, node, rect, depth):
        """
        Recursively draw a node (directory or file) within the given rect.
        Directories with children get a sublayout.
        """
        if rect.width() <= 0 or rect.height() <= 0:
            return

        # Determine color based on depth: shift hue by 30 degrees per level.
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

        # If node is a directory with children and there's enough room, draw its sub-treemap.
        if node.is_dir and node.children and rect.width() > 30 and rect.height() > (fm.height() + 10):
            inner = QRectF(rect.x() + margin, rect.y() + fm.height() + margin,
                           rect.width() - 2 * margin, rect.height() - fm.height() - 2 * margin)
            if inner.width() < 20 or inner.height() < 20:
                return

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
            fraction = visibleTotal / total  # fraction for visible items
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
                elided = fm.elidedText(othersLabel, Qt.ElideRight, int(othersRect.width() - 4))
                # Convert float coordinates to int to satisfy the drawText signature.
                painter.drawText(int(othersRect.x() + 2),
                                 int(othersRect.y() + fm.ascent() + 2),
                                 elided)


# --------------- Main Window ---------------

class MainWindow(QMainWindow):
    def __init__(self, root_node, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Directory Treemap")
        self.widget = TreemapWidget(root_node)
        self.setCentralWidget(self.widget)


# --------------- Main Entry Point ---------------

def main():
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = os.getcwd()
    if not os.path.exists(target):
        print("Path does not exist:", target)
        sys.exit(1)
    print("Scanning", target, "…")
    root_node = scan_directory(target)
    app = QApplication(sys.argv)
    win = MainWindow(root_node)
    win.resize(800, 600)
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
