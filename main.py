import sys
import os
import re
import csv
import json
import subprocess
import tempfile

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QFileDialog, QHeaderView, QMessageBox, QGroupBox, QAbstractItemView,
    QAction, QDialog, QListWidget, QListWidgetItem, QDialogButtonBox,
    QSizePolicy, QTextEdit, QTreeWidget, QTreeWidgetItem, QProgressDialog,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence

from osp_generator import generate_osp, FFprobeError
from svg_generator import generate_score_svg


PROJECT_EXT = ".ospm"
PROJECT_FILTER = "OSP Maker Project (*.ospm);;All Files (*)"
PROJECT_VERSION = 1


class ClipTable(QTableWidget):
    # Columns 0-4 are editable; 5 (Near) and 6 (Far) are read-only computed; 7 (SVG File) is auto-filled.
    COLUMNS = ["Start Min", "Start Sec", "End Min", "End Sec", "Winner (N/F)",
               "Near", "Far", "SVG File"]
    _EDITABLE_COLS = 5   # cols 0-4 accept user input

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        header = self.horizontalHeader()
        for i in range(self._EDITABLE_COLS):
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)  # Near
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)  # Far
        header.setSectionResizeMode(7, QHeaderView.Stretch)           # SVG File
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            self._paste()
        elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(event)

    def _paste(self):
        text = QApplication.clipboard().text()
        if not text:
            return

        rows = []
        for line in text.strip().splitlines():
            if '\t' in line:
                row = [c.strip() for c in line.split('\t')]
            else:
                row = next(csv.reader([line]))
                row = [c.strip() for c in row]
            if any(row):
                rows.append(row)

        if not rows:
            return

        start_row = self.currentRow()
        if start_row < 0:
            start_row = self.rowCount()

        for i, row_data in enumerate(rows):
            target = start_row + i
            if target >= self.rowCount():
                self.insertRow(self.rowCount())
            for col, value in enumerate(row_data[:self._EDITABLE_COLS]):
                self.setItem(target, col, QTableWidgetItem(value))

    def delete_selected(self):
        rows = sorted(set(idx.row() for idx in self.selectedIndexes()), reverse=True)
        for row in rows:
            self.removeRow(row)

    @staticmethod
    def _readonly_item(text):
        item = QTableWidgetItem(str(text))
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def get_rows(self):
        """Return raw cell text as list-of-lists (for project save)."""
        rows = []
        for r in range(self.rowCount()):
            row = []
            for c in range(self.columnCount()):
                item = self.item(r, c)
                row.append(item.text() if item else "")
            rows.append(row)
        return rows

    def set_rows(self, rows):
        """Replace all rows with the given data (list-of-lists of strings)."""
        self.setRowCount(0)
        for row_data in rows:
            r = self.rowCount()
            self.insertRow(r)
            for c, value in enumerate(row_data[:len(self.COLUMNS)]):
                if c in (5, 6):
                    self.setItem(r, c, self._readonly_item(value))
                else:
                    self.setItem(r, c, QTableWidgetItem(str(value)))

    def get_clips(self):
        clips = []
        errors = []
        for row in range(self.rowCount()):
            def cell(col):
                item = self.item(row, col)
                return item.text().strip() if item else ""

            try:
                start_min = int(cell(0) or 0)
                start_sec = float(cell(1) or 0)
                end_min = int(cell(2) or 0)
                end_sec = float(cell(3) or 0)
                svg_file = cell(7)

                start_total = start_min * 60 + start_sec
                end_total = end_min * 60 + end_sec

                if end_total <= start_total:
                    errors.append(f"Row {row + 1}: end time must be after start time.")
                    continue
                if not svg_file:
                    errors.append(f"Row {row + 1}: SVG file missing — run Prepare Scores first.")
                    continue

                clips.append({"start": start_total, "end": end_total, "svg_file": svg_file})
            except ValueError:
                errors.append(f"Row {row + 1}: invalid numeric value.")

        return clips, errors


VIDEO_FILE_FILTER = "Video Files (*.mp4 *.avi *.mov *.mkv *.wmv *.flv *.webm);;All Files (*)"

# Matches:  GOPRdddd.MP4  (chapter 0)
#           GHccdddd.MP4  (chapter cc, cc >= 01)
_GOPRO_RE = re.compile(r'^(GOPR|GH(\d{2}))(\d{4})\.MP4$', re.IGNORECASE)


def _parse_gopro_groups(folder):
    """
    Scan *folder* for GoPro chaptered MP4 files and group them by 4-digit video ID.

    Returns:
        dict: video_id (str) -> list of (chapter_int, full_path) sorted by chapter.
    """
    groups = {}
    try:
        entries = os.listdir(folder)
    except OSError:
        return groups
    for fname in entries:
        m = _GOPRO_RE.match(fname)
        if not m:
            continue
        prefix, chapter_str, video_id = m.group(1), m.group(2), m.group(3)
        chapter = 0 if prefix.upper() == "GOPR" else int(chapter_str)
        groups.setdefault(video_id, []).append((chapter, os.path.join(folder, fname)))
    for vid_id in groups:
        groups[vid_id].sort(key=lambda x: x[0])
    return groups


class MergeVideoFolderDialog(QDialog):
    """Detect GoPro chapter groups in a folder and merge each group into one file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge Video Folder")
        self.setMinimumSize(860, 520)
        self._groups = {}   # video_id -> [(chapter, path), ...]
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # Source folder row
        in_row = QHBoxLayout()
        in_row.addWidget(QLabel("Source Folder:"))
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Browse or type path…")
        in_row.addWidget(self.folder_edit)
        browse_in = QPushButton("Browse…")
        browse_in.setFixedWidth(80)
        browse_in.clicked.connect(self._browse_folder)
        in_row.addWidget(browse_in)
        scan_btn = QPushButton("Re-scan")
        scan_btn.setFixedWidth(70)
        scan_btn.clicked.connect(self._scan)
        in_row.addWidget(scan_btn)
        root.addLayout(in_row)

        # Preview table
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["✓", "Video ID", "Output File", "Ch.", "Segments (in order)"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table)

        # Output folder row
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output Folder:"))
        self.out_folder_edit = QLineEdit()
        self.out_folder_edit.setPlaceholderText("Defaults to source folder")
        out_row.addWidget(self.out_folder_edit)
        browse_out = QPushButton("Browse…")
        browse_out.setFixedWidth(80)
        browse_out.clicked.connect(self._browse_out_folder)
        out_row.addWidget(browse_out)
        root.addLayout(out_row)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Merge Selected")
        buttons.accepted.connect(self._do_merge)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── Slots ─────────────────────────────────────────────────────────

    def _browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Source Folder")
        if path:
            self.folder_edit.setText(path)
            if not self.out_folder_edit.text().strip():
                self.out_folder_edit.setText(path)
            self._scan()

    def _browse_out_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path:
            self.out_folder_edit.setText(path)

    def _scan(self):
        folder = self.folder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "Invalid Folder",
                                "Please enter a valid source folder path.")
            return
        groups = _parse_gopro_groups(folder)
        self._groups = groups
        self.table.setRowCount(0)
        if not groups:
            QMessageBox.information(self, "No GoPro Files Found",
                                    "No GoPro chaptered files (GOPRxxxx / GHccxxxx) "
                                    "were found in that folder.")
            return
        for video_id in sorted(groups.keys()):
            chapters = groups[video_id]
            row = self.table.rowCount()
            self.table.insertRow(row)

            # Col 0 — checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)
            self.table.setItem(row, 0, chk)

            # Col 1 — video ID (read-only)
            vid_item = QTableWidgetItem(video_id)
            vid_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 1, vid_item)

            # Col 2 — output filename (editable)
            self.table.setItem(row, 2, QTableWidgetItem(f"{video_id}.mp4"))

            # Col 3 — chapter count (read-only)
            ch_item = QTableWidgetItem(str(len(chapters)))
            ch_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 3, ch_item)

            # Col 4 — segment filenames (read-only)
            names = ", ".join(os.path.basename(p) for _, p in chapters)
            names_item = QTableWidgetItem(names)
            names_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 4, names_item)

    # ── Merge ─────────────────────────────────────────────────────────

    def _do_merge(self):
        out_folder = (self.out_folder_edit.text().strip()
                      or self.folder_edit.text().strip())
        if not out_folder or not os.path.isdir(out_folder):
            QMessageBox.warning(self, "Invalid Output Folder",
                                "Please select a valid output folder.")
            return

        # Collect checked rows
        to_merge = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() != Qt.Checked:
                continue
            video_id = self.table.item(row, 1).text()
            out_name = self.table.item(row, 2).text().strip()
            if not out_name:
                out_name = f"{video_id}.mp4"
            if not out_name.lower().endswith(".mp4"):
                out_name += ".mp4"
            paths = [p for _, p in self._groups.get(video_id, [])]
            if paths:
                to_merge.append((out_name, paths))

        if not to_merge:
            QMessageBox.warning(self, "Nothing Selected",
                                "Check at least one video to merge.")
            return

        progress = QProgressDialog("Starting…", None, 0, len(to_merge), self)
        progress.setWindowTitle("Merging Videos")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QApplication.processEvents()

        errors = []
        tmp_path = None
        try:
            for i, (out_name, paths) in enumerate(to_merge):
                progress.setLabelText(
                    f"Merging {out_name}  ({i + 1} of {len(to_merge)})…"
                )
                progress.setValue(i)
                QApplication.processEvents()

                output_path = os.path.join(out_folder, out_name)
                try:
                    with tempfile.NamedTemporaryFile(
                        mode='w', suffix='.txt', delete=False, encoding='utf-8'
                    ) as tmp:
                        tmp_path = tmp.name
                        for p in paths:
                            tmp.write(f"file '{p.replace(chr(92), '/')}'\n")

                    result = subprocess.run(
                        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                         "-i", tmp_path, "-map", "0:v", "-map", "0:a",
                         "-c", "copy", output_path],
                        capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        errors.append(f"{out_name}:\n{result.stderr[-600:]}")
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    tmp_path = None

            progress.setValue(len(to_merge))
        finally:
            progress.close()
            QApplication.processEvents()

        if errors:
            err_dlg = QDialog(self)
            err_dlg.setWindowTitle("Merge Errors")
            err_dlg.setMinimumSize(640, 320)
            lay = QVBoxLayout(err_dlg)
            lay.addWidget(QLabel(f"{len(errors)} error(s) — text is selectable:"))
            te = QTextEdit()
            te.setReadOnly(True)
            te.setPlainText("\n\n".join(errors))
            lay.addWidget(te)
            ok = QDialogButtonBox(QDialogButtonBox.Ok)
            ok.accepted.connect(err_dlg.accept)
            lay.addWidget(ok)
            err_dlg.exec_()
        else:
            succeeded = len(to_merge) - len(errors)
            QMessageBox.information(
                self, "Done",
                f"Successfully merged {succeeded} video(s) into:\n{out_folder}"
            )
            self.accept()


class MergeVideosDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge Videos")
        self.setMinimumSize(600, 400)
        self.merged_path = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        self.file_list = QListWidget()
        self.file_list.setAlternatingRowColors(True)
        self.file_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.file_list)

        btn_bar = QHBoxLayout()
        for label, slot in [
            ("Add Files…",  self._add_files),
            ("Remove",      self._remove_selected),
            ("Move Up",     self._move_up),
            ("Move Down",   self._move_down),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            btn_bar.addWidget(btn)
        btn_bar.addStretch()
        root.addLayout(btn_bar)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output:"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Browse or type path…")
        out_row.addWidget(self.output_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        root.addLayout(out_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Merge")
        buttons.accepted.connect(self._do_merge)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── File list management ───────────────────────────────────────────

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Video Files", "", VIDEO_FILE_FILTER
        )
        if not paths:
            return
        existing = {self.file_list.item(i).data(Qt.UserRole)
                    for i in range(self.file_list.count())}
        for p in paths:
            if p not in existing:
                item = QListWidgetItem(os.path.basename(p))
                item.setData(Qt.UserRole, p)
                self.file_list.addItem(item)
        self._sort_alphabetically()
        if not self.output_edit.text().strip() and paths:
            self.output_edit.setText(
                os.path.join(os.path.dirname(paths[0]), "merged.mp4")
            )

    def _sort_alphabetically(self):
        items = [(self.file_list.item(i).data(Qt.UserRole),
                  self.file_list.item(i).text())
                 for i in range(self.file_list.count())]
        items.sort(key=lambda x: x[0].lower())
        self.file_list.clear()
        for path, name in items:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, path)
            self.file_list.addItem(item)

    def _remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _move_up(self):
        row = self.file_list.currentRow()
        if row <= 0:
            return
        self._swap_rows(row, row - 1)
        self.file_list.setCurrentRow(row - 1)

    def _move_down(self):
        row = self.file_list.currentRow()
        if row < 0 or row >= self.file_list.count() - 1:
            return
        self._swap_rows(row, row + 1)
        self.file_list.setCurrentRow(row + 1)

    def _swap_rows(self, a, b):
        item_a = self.file_list.takeItem(a)
        item_b = self.file_list.takeItem(b - 1)  # b-1 after takeItem(a) shifted it
        self.file_list.insertItem(a, item_b)
        self.file_list.insertItem(b, item_a)

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Merged Video", "",
            "Video Files (*.mp4);;All Files (*)"
        )
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self.output_edit.setText(path)

    # ── Merge ─────────────────────────────────────────────────────────

    def _do_merge(self):
        paths = [self.file_list.item(i).data(Qt.UserRole)
                 for i in range(self.file_list.count())]
        output = self.output_edit.text().strip()
        if output and not output.lower().endswith(".mp4"):
            output += ".mp4"
            self.output_edit.setText(output)

        if len(paths) < 2:
            QMessageBox.warning(self, "Not Enough Files",
                                "Add at least two video files to merge.")
            return
        if not output:
            QMessageBox.warning(self, "No Output", "Please specify an output file.")
            return

        wait_box = QMessageBox(self)
        wait_box.setWindowTitle("Merging")
        wait_box.setText("Merging video files… please wait.")
        wait_box.setStandardButtons(QMessageBox.NoButton)
        wait_box.show()
        QApplication.processEvents()

        result = None
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                             delete=False, encoding='utf-8') as tmp:
                tmp_path = tmp.name
                for p in paths:
                    tmp.write(f"file '{p.replace(chr(92), '/')}'\n")

            result = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", tmp_path, "-map", "0:v", "-map", "0:a",
                 "-c", "copy", output],
                capture_output=True, text=True
            )
        finally:
            wait_box.close()
            QApplication.processEvents()
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if result is None or result.returncode != 0:
            err_dlg = QDialog(self)
            err_dlg.setWindowTitle("Merge Failed")
            err_dlg.setMinimumSize(600, 300)
            err_layout = QVBoxLayout(err_dlg)
            err_layout.addWidget(QLabel("ffmpeg returned an error (text is selectable):"))
            err_text = QTextEdit()
            err_text.setReadOnly(True)
            err_text.setPlainText(result.stderr)
            err_layout.addWidget(err_text)
            close_btn = QDialogButtonBox(QDialogButtonBox.Ok)
            close_btn.accepted.connect(err_dlg.accept)
            err_layout.addWidget(close_btn)
            err_dlg.exec_()
            return

        self.merged_path = output
        self.accept()


class MergeVideoFolderDialog(QDialog):
    """Detect GoPro chapter segments in a folder and merge each group."""

    _GOPRO_RE = re.compile(r'^GH(\d{2})(\d{4})\.MP4$', re.IGNORECASE)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Merge GoPro Video Folder")
        self.setMinimumSize(660, 500)
        self.merged_paths = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # Input folder row
        in_row = QHBoxLayout()
        in_row.addWidget(QLabel("Folder:"))
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Select a folder containing GoPro segments…")
        in_row.addWidget(self.folder_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_folder)
        in_row.addWidget(browse_btn)
        scan_btn = QPushButton("Scan")
        scan_btn.setFixedWidth(60)
        scan_btn.clicked.connect(self._scan)
        in_row.addWidget(scan_btn)
        root.addLayout(in_row)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Output File / Segments", "Count"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        root.addWidget(self.tree)

        # Status
        self.status_label = QLabel("Select a folder and click Scan (or Browse to auto-scan).")
        root.addWidget(self.status_label)

        # Output folder row
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output folder:"))
        self.out_folder_edit = QLineEdit()
        self.out_folder_edit.setPlaceholderText("Defaults to input folder")
        out_row.addWidget(self.out_folder_edit)
        out_browse_btn = QPushButton("Browse…")
        out_browse_btn.setFixedWidth(80)
        out_browse_btn.clicked.connect(self._browse_out_folder)
        out_row.addWidget(out_browse_btn)
        root.addLayout(out_row)

        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Merge Selected")
        btns.accepted.connect(self._do_merge)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ── Browse ─────────────────────────────────────────────────────────

    def _browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select GoPro Video Folder")
        if path:
            self.folder_edit.setText(path)
            if not self.out_folder_edit.text().strip():
                self.out_folder_edit.setText(path)
            self._scan()

    def _browse_out_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path:
            self.out_folder_edit.setText(path)

    # ── Scan ───────────────────────────────────────────────────────────

    def _scan(self):
        folder = self.folder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            self.status_label.setText("Please enter a valid folder path.")
            return

        self.tree.clear()
        groups = {}
        try:
            for filename in sorted(os.listdir(folder)):
                m = self._GOPRO_RE.match(filename)
                if m:
                    chapter = int(m.group(1))
                    vid_id = m.group(2)
                    groups.setdefault(vid_id, []).append((chapter, filename))
        except OSError as exc:
            self.status_label.setText(f"Error reading folder: {exc}")
            return

        for vid_id in groups:
            groups[vid_id].sort(key=lambda x: x[0])

        # Only groups with 2+ segments need merging
        merge_groups = {
            vid_id: segs
            for vid_id, segs in sorted(groups.items())
            if len(segs) > 1
        }

        if not merge_groups:
            self.status_label.setText("No multi-segment GoPro videos found in this folder.")
            return

        for vid_id, segs in merge_groups.items():
            out_name = f"GOPR{vid_id}.MP4"
            top = QTreeWidgetItem(self.tree, [out_name, str(len(segs))])
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable)
            top.setCheckState(0, Qt.Checked)
            top.setData(0, Qt.UserRole, (vid_id, [seg[1] for seg in segs]))
            for _chapter, filename in segs:
                child = QTreeWidgetItem(top, [filename, ""])
                child.setFlags(child.flags() & ~Qt.ItemIsUserCheckable)

        self.tree.expandAll()
        self.status_label.setText(
            f"Found {len(merge_groups)} video(s) to merge. Uncheck any you want to skip."
        )

    # ── Merge ──────────────────────────────────────────────────────────

    def _do_merge(self):
        folder = self.folder_edit.text().strip()
        out_folder = self.out_folder_edit.text().strip() or folder

        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "No Folder", "Please select a valid input folder.")
            return
        if not os.path.isdir(out_folder):
            QMessageBox.warning(self, "Invalid Output Folder",
                                f"Output folder not found:\n{out_folder}")
            return

        # Collect checked groups
        checked = []
        root_item = self.tree.invisibleRootItem()
        for i in range(root_item.childCount()):
            top = root_item.child(i)
            if top.checkState(0) == Qt.Checked:
                vid_id, segs = top.data(0, Qt.UserRole)
                checked.append((vid_id, segs))

        if not checked:
            QMessageBox.warning(self, "Nothing Selected",
                                "Check at least one video to merge.")
            return

        total = len(checked)
        errors = []

        for idx, (vid_id, segs) in enumerate(checked):
            out_name = f"GOPR{vid_id}.MP4"
            out_path = os.path.join(out_folder, out_name)
            self.status_label.setText(f"Merging {idx + 1}/{total}: {out_name}…")
            QApplication.processEvents()

            result = None
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                                 delete=False, encoding='utf-8') as tmp:
                    tmp_path = tmp.name
                    for seg in segs:
                        full = os.path.join(folder, seg).replace('\\', '/')
                        tmp.write(f"file '{full}'\n")

                result = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", tmp_path, "-map", "0:v", "-map", "0:a",
                     "-c", "copy", out_path],
                    capture_output=True, text=True
                )
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            if result is None or result.returncode != 0:
                stderr = result.stderr if result else "Unknown error"
                errors.append(f"{out_name}:\n{stderr}")
            else:
                self.merged_paths.append(out_path)

        if errors:
            err_dlg = QDialog(self)
            err_dlg.setWindowTitle("Merge Errors")
            err_dlg.setMinimumSize(600, 300)
            err_layout = QVBoxLayout(err_dlg)
            err_layout.addWidget(QLabel(f"{len(errors)} merge(s) failed (text is selectable):"))
            err_text = QTextEdit()
            err_text.setReadOnly(True)
            err_text.setPlainText("\n\n".join(errors))
            err_layout.addWidget(err_text)
            close_btn = QDialogButtonBox(QDialogButtonBox.Ok)
            close_btn.accepted.connect(err_dlg.accept)
            err_layout.addWidget(close_btn)
            err_dlg.exec_()

        if self.merged_paths:
            n = len(self.merged_paths)
            self.status_label.setText(f"Done — {n} video(s) merged successfully.")
            self.accept()
        else:
            self.status_label.setText("No videos were merged successfully.")


class OSPMakerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_project_path = None  # path to .ospm file (None = unsaved)
        self.setMinimumSize(960, 640)
        self._build_ui()
        self._build_menu()
        self.table.insertRow(0)
        self._update_title()
        self.statusBar().showMessage("Ready")

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        file_group = QGroupBox("Project Files")
        file_layout = QVBoxLayout(file_group)
        file_layout.setSpacing(6)

        self.video_edit = self._file_row(file_layout, "Video File:", self._browse_video)
        self.svg_dir_edit = self._file_row(file_layout, "SVG Folder:", self._browse_svg_dir)
        self.output_edit = self._file_row(file_layout, "Output (.osp):", self._browse_output)

        root.addWidget(file_group)

        xform_group = QGroupBox("Video Transform")
        xform_layout = QHBoxLayout(xform_group)
        xform_layout.setSpacing(10)
        self.vid_loc_x, self.vid_loc_y, self.vid_scale_x, self.vid_scale_y, self.vid_rotation = (
            self._xform_field(xform_layout, lbl, ph)
            for lbl, ph in [
                ("Loc X", "0"), ("Loc Y", "0"),
                ("Scale X", "1"), ("Scale Y", "1"),
                ("Rotation", "0"),
            ]
        )
        xform_layout.addStretch()
        root.addWidget(xform_group)

        clips_group = QGroupBox("Clips")
        clips_layout = QVBoxLayout(clips_group)
        clips_layout.setSpacing(6)

        btn_bar = QHBoxLayout()
        for label, slot in [
            ("Add Row", self._add_row),
            ("Delete Selected", lambda: self.table.delete_selected()),
            ("Import CSV…", self._import_csv),
            ("Clear All", self._clear_all),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            btn_bar.addWidget(btn)
        btn_bar.addStretch()
        clips_layout.addLayout(btn_bar)

        self.table = ClipTable()
        clips_layout.addWidget(self.table)
        root.addWidget(clips_group)

        action_bar = QHBoxLayout()
        prep_btn = QPushButton("Prepare Scores")
        prep_btn.setFixedHeight(38)
        prep_btn.clicked.connect(self._prepare_scores)
        action_bar.addWidget(prep_btn)

        gen_btn = QPushButton("Generate OSP File")
        gen_btn.setFixedHeight(38)
        gen_btn.clicked.connect(self._generate)
        action_bar.addWidget(gen_btn)
        root.addLayout(action_bar)

    def _build_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        act_new = QAction("&New", self)
        act_new.setShortcut(QKeySequence.New)
        act_new.triggered.connect(self._new_project)
        file_menu.addAction(act_new)

        act_open = QAction("&Open…", self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self._open_project)
        file_menu.addAction(act_open)

        file_menu.addSeparator()

        act_save = QAction("&Save", self)
        act_save.setShortcut(QKeySequence.Save)
        act_save.triggered.connect(self._save_project)
        file_menu.addAction(act_save)

        act_save_as = QAction("Save &As…", self)
        act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        act_save_as.triggered.connect(self._save_project_as)
        file_menu.addAction(act_save_as)

        file_menu.addSeparator()

        act_exit = QAction("E&xit", self)
        act_exit.setShortcut(QKeySequence.Quit)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        video_menu = menubar.addMenu("&Video")
        act_merge = QAction("Merge Videos…", self)
        act_merge.triggered.connect(self._merge_videos)
        video_menu.addAction(act_merge)

        act_merge_folder = QAction("Merge Video Folder…", self)
        act_merge_folder.triggered.connect(self._merge_video_folder)
        video_menu.addAction(act_merge_folder)

        act_merge_folder = QAction("Merge Video Folder…", self)
        act_merge_folder.triggered.connect(self._merge_video_folder)
        video_menu.addAction(act_merge_folder)

    def _xform_field(self, parent_layout, label_text, placeholder):
        lbl = QLabel(label_text)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedWidth(60)
        parent_layout.addWidget(lbl)
        parent_layout.addWidget(edit)
        return edit

    def _file_row(self, parent_layout, label_text, browse_slot):
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setFixedWidth(110)
        edit = QLineEdit()
        edit.setPlaceholderText("Browse or type path…")
        btn = QPushButton("Browse…")
        btn.setFixedWidth(80)
        btn.clicked.connect(browse_slot)
        row.addWidget(lbl)
        row.addWidget(edit)
        row.addWidget(btn)
        parent_layout.addLayout(row)
        return edit

    def _update_title(self):
        if self.current_project_path:
            name = os.path.basename(self.current_project_path)
            self.setWindowTitle(f"OSP Generator — {name}")
        else:
            self.setWindowTitle("OSP Generator — Untitled")

    # ── Project save / load ───────────────────────────────────────────

    def _project_state(self):
        return {
            "version":     PROJECT_VERSION,
            "video_file":  self.video_edit.text(),
            "svg_dir":     self.svg_dir_edit.text(),
            "output_file": self.output_edit.text(),
            "clips":       self.table.get_rows(),
            "vid_transform": {
                "loc_x":    self.vid_loc_x.text(),
                "loc_y":    self.vid_loc_y.text(),
                "scale_x":  self.vid_scale_x.text(),
                "scale_y":  self.vid_scale_y.text(),
                "rotation": self.vid_rotation.text(),
            },
        }

    def _apply_project_state(self, state):
        self.video_edit.setText(state.get("video_file", ""))
        self.svg_dir_edit.setText(state.get("svg_dir", ""))
        self.output_edit.setText(state.get("output_file", ""))
        rows = state.get("clips", [])
        if rows:
            self.table.set_rows(rows)
        else:
            self.table.setRowCount(0)
            self.table.insertRow(0)
        xf = state.get("vid_transform", {})
        self.vid_loc_x.setText(xf.get("loc_x", ""))
        self.vid_loc_y.setText(xf.get("loc_y", ""))
        self.vid_scale_x.setText(xf.get("scale_x", ""))
        self.vid_scale_y.setText(xf.get("scale_y", ""))
        self.vid_rotation.setText(xf.get("rotation", ""))

    def _new_project(self):
        self.video_edit.clear()
        self.svg_dir_edit.clear()
        self.output_edit.clear()
        self.table.setRowCount(0)
        self.table.insertRow(0)
        self.current_project_path = None
        self._update_title()
        self.statusBar().showMessage("New project")

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", PROJECT_FILTER
        )
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                state = json.load(f)
            self._apply_project_state(state)
            self.current_project_path = path
            self._update_title()
            self.statusBar().showMessage(f"Opened: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Open Error", f"Could not open project:\n{exc}")
            return

        # Always prompt for the video file so we get a fresh absolute path.
        stored_video = self.video_edit.text().strip()
        start_dir = ""
        if stored_video:
            candidate = os.path.dirname(os.path.abspath(stored_video))
            if os.path.isdir(candidate):
                start_dir = candidate
        self.video_edit.clear()
        video_path, _ = QFileDialog.getOpenFileName(
            self, "Locate Video File", start_dir, VIDEO_FILE_FILTER
        )
        if video_path:
            self.video_edit.setText(video_path)
            if not self.output_edit.text().strip():
                stem = os.path.splitext(os.path.basename(video_path))[0]
                self.output_edit.setText(
                    os.path.join(os.path.dirname(video_path), stem + ".osp")
                )

    def _save_project(self):
        if self.current_project_path:
            self._write_project_to(self.current_project_path)
        else:
            self._save_project_as()

    def _save_project_as(self):
        # Default filename derived from the video file stem if available
        default_name = "untitled" + PROJECT_EXT
        default_dir = ""
        video = self.video_edit.text().strip()
        if video:
            stem = os.path.splitext(os.path.basename(video))[0]
            default_name = stem + PROJECT_EXT
            default_dir = os.path.dirname(video)

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project As",
            os.path.join(default_dir, default_name) if default_dir else default_name,
            PROJECT_FILTER
        )
        if not path:
            return
        if not path.lower().endswith(PROJECT_EXT):
            path += PROJECT_EXT
        self._write_project_to(path)
        self.current_project_path = path
        self._update_title()

    def _write_project_to(self, path):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._project_state(), f, indent=2)
            self.statusBar().showMessage(f"Project saved: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", f"Could not save project:\n{exc}")

    # ── Video menu actions ─────────────────────────────────────────────

    def _merge_videos(self):
        dlg = MergeVideosDialog(self)
        if dlg.exec_() == QDialog.Accepted and dlg.merged_path:
            self.video_edit.setText(dlg.merged_path)
            if not self.output_edit.text().strip():
                stem = os.path.splitext(os.path.basename(dlg.merged_path))[0]
                self.output_edit.setText(
                    os.path.join(os.path.dirname(dlg.merged_path), stem + ".osp")
                )
            self.statusBar().showMessage(f"Loaded merged video: {dlg.merged_path}")

    def _merge_video_folder(self):
        dlg = MergeVideoFolderDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.statusBar().showMessage("Folder merge complete.")

    def _merge_video_folder(self):
        dlg = MergeVideoFolderDialog(self)
        if dlg.exec_() == QDialog.Accepted and dlg.merged_paths:
            first = dlg.merged_paths[0]
            self.video_edit.setText(first)
            if not self.output_edit.text().strip():
                stem = os.path.splitext(os.path.basename(first))[0]
                self.output_edit.setText(
                    os.path.join(os.path.dirname(first), stem + ".osp")
                )
            n = len(dlg.merged_paths)
            self.statusBar().showMessage(
                f"Merged {n} video(s) — loaded: {first}"
            )

    # ── Browse slots ───────────────────────────────────────────────────

    def _browse_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", "", VIDEO_FILE_FILTER
        )
        if path:
            self.video_edit.setText(path)
            if not self.output_edit.text().strip():
                stem = os.path.splitext(os.path.basename(path))[0]
                default_out = os.path.join(os.path.dirname(path), stem + ".osp")
                self.output_edit.setText(default_out)

    def _browse_svg_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Select SVG Folder")
        if path:
            self.svg_dir_edit.setText(path)

    def _browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save OSP File", "",
            "OpenShot Project (*.osp);;All Files (*)"
        )
        if path:
            if not path.lower().endswith(".osp"):
                path += ".osp"
            self.output_edit.setText(path)

    # ── Table actions ──────────────────────────────────────────────────

    def _add_row(self):
        self.table.insertRow(self.table.rowCount())

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import CSV", "", "CSV Files (*.csv *.txt);;All Files (*)"
        )
        if not path:
            return
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]

            self.table.setRowCount(0)
            for row_data in rows:
                row = self.table.rowCount()
                self.table.insertRow(row)
                for col, value in enumerate(row_data[:5]):
                    self.table.setItem(row, col, QTableWidgetItem(value.strip()))

            self.statusBar().showMessage(f"Imported {len(rows)} rows from {os.path.basename(path)}")
        except Exception as exc:
            QMessageBox.critical(self, "Import Error", str(exc))

    def _clear_all(self):
        if self.table.rowCount() == 0:
            return
        if QMessageBox.question(
            self, "Clear All", "Remove all rows?",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.Yes:
            self.table.setRowCount(0)

    # ── Prepare Scores ─────────────────────────────────────────────────

    def _prepare_scores(self):
        svg_folder = self.svg_dir_edit.text().strip()
        if not svg_folder:
            QMessageBox.warning(self, "Missing SVG Folder",
                                "Please select an SVG Folder before preparing scores.")
            return
        if not os.path.isdir(svg_folder):
            QMessageBox.warning(self, "Folder Not Found",
                                f"SVG Folder not found:\n{svg_folder}")
            return

        n_rows = self.table.rowCount()
        if n_rows == 0:
            QMessageBox.warning(self, "No Clips", "Please add at least one clip row.")
            return

        # Validate Winner column (col 4) — N/i → Near, F/o → Far, or empty
        _WINNER_MAP = {"N": "N", "I": "N", "F": "F", "O": "F", "": ""}
        errors = []
        winners = []
        for row in range(n_rows):
            item = self.table.item(row, 4)
            raw = item.text().strip() if item else ""
            w = _WINNER_MAP.get(raw.upper())
            if w is None:
                errors.append(f"Row {row + 1}: Winner must be N, F, i, o, or empty (got '{raw}').")
                w = ""
            else:
                # Write the normalised value back so the table shows N/F
                if raw and raw.upper() != w:
                    norm_item = QTableWidgetItem(w)
                    self.table.setItem(row, 4, norm_item)
            winners.append(w)

        if errors:
            QMessageBox.warning(self, "Invalid Winner Values", "\n".join(errors))
            return

        # Compute running scores and generate SVG files.
        # Reuse the same SVG file when the score hasn't changed.
        near, far = 0, 0
        score_svg_map = {}   # (near, far) -> svg_name
        svg_counter = 0

        for row, winner in enumerate(winners):
            score_key = (near, far)
            if score_key not in score_svg_map:
                svg_counter += 1
                svg_name = f"{svg_counter:02d}.svg"
                svg_path = os.path.join(svg_folder, svg_name)
                try:
                    generate_score_svg(near, far, svg_path)
                except Exception as exc:
                    QMessageBox.critical(self, "SVG Error",
                                         f"Could not write {svg_name}:\n{exc}")
                    return
                score_svg_map[score_key] = svg_name
            else:
                svg_name = score_svg_map[score_key]

            self.table.setItem(row, 5, ClipTable._readonly_item(near))
            self.table.setItem(row, 6, ClipTable._readonly_item(far))
            self.table.setItem(row, 7, QTableWidgetItem(svg_name))

            if winner == "N":
                near += 1
            elif winner == "F":
                far += 1
            # empty: score unchanged

        self.statusBar().showMessage(
            f"Prepared {n_rows} clip(s) → {svg_counter} SVG file(s) in {svg_folder}"
        )
        QMessageBox.information(
            self, "Scores Prepared",
            f"Generated {svg_counter} SVG file(s) for {n_rows} clip(s) in:\n{svg_folder}\n\n"
            f"Final score — Near: {near} | Far: {far}"
        )

    # ── Generate OSP ───────────────────────────────────────────────────

    def _generate(self):
        video = self.video_edit.text().strip()
        svg_dir = self.svg_dir_edit.text().strip()
        output = self.output_edit.text().strip()

        if not video:
            QMessageBox.warning(self, "Missing Input", "Please select a video file.")
            return
        if not os.path.isfile(video):
            QMessageBox.warning(self, "File Not Found", f"Video file not found:\n{video}")
            return
        if not svg_dir:
            QMessageBox.warning(self, "Missing Input", "Please select an SVG Folder.")
            return
        if not os.path.isdir(svg_dir):
            QMessageBox.warning(self, "Folder Not Found", f"SVG Folder not found:\n{svg_dir}")
            return
        if not output:
            QMessageBox.warning(self, "Missing Input", "Please specify an output file path.")
            return

        clips, errors = self.table.get_clips()

        if errors:
            QMessageBox.warning(self, "Clip Errors", "\n".join(errors))
            return
        if not clips:
            QMessageBox.warning(self, "No Clips", "Please add at least one valid clip row.")
            return

        missing = [c["svg_file"] for c in clips
                   if not os.path.isfile(os.path.join(svg_dir, c["svg_file"]))]
        if missing:
            reply = QMessageBox.warning(
                self, "Missing SVG Files",
                "The following SVG files were not found in the SVG directory:\n\n"
                + "\n".join(missing)
                + "\n\nContinue anyway?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        def _parse_xform(edit, default):
            t = edit.text().strip()
            return float(t) if t else default

        try:
            loc_x    = _parse_xform(self.vid_loc_x,    0.0)
            loc_y    = _parse_xform(self.vid_loc_y,    0.0)
            scale_x  = _parse_xform(self.vid_scale_x,  1.0)
            scale_y  = _parse_xform(self.vid_scale_y,  1.0)
            rotation = _parse_xform(self.vid_rotation, 0.0)
        except ValueError:
            QMessageBox.warning(self, "Invalid Transform",
                                "Video Transform fields must be numeric.")
            return

        try:
            generate_osp(video, svg_dir, clips, output,
                         loc_x=loc_x, loc_y=loc_y,
                         scale_x=scale_x, scale_y=scale_y,
                         rotation=rotation)
            self.statusBar().showMessage(f"Saved: {output}")
            QMessageBox.information(self, "Done", f"OSP file written to:\n{output}")
        except FFprobeError as exc:
            QMessageBox.critical(
                self, "ffprobe Required",
                f"Cannot read video metadata.\n\n{exc}\n\n"
                "Without ffprobe the generated OSP would tell OpenShot the wrong "
                "video duration, frame count, and resolution — playback would break."
            )
            self.statusBar().showMessage("ffprobe missing — OSP not generated.")
        except Exception as exc:
            QMessageBox.critical(self, "Generation Error", str(exc))
            self.statusBar().showMessage("Error — OSP not generated.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OSPMakerWindow()
    window.show()
    sys.exit(app.exec_())
