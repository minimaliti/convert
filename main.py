import sys
from pathlib import Path
from PIL import Image, UnidentifiedImageError, features
import json

from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFileDialog,
    QComboBox,
    QSpinBox,
    QProgressBar,
    QCheckBox,
    QMessageBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QGroupBox,
    QProgressDialog,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QEvent
from PyQt6.QtGui import QKeySequence, QShortcut, QEnterEvent
import os
from concurrent.futures import ThreadPoolExecutor


class Signals(QObject):
    job_done = pyqtSignal(bool, str)


class FileLoaderThread(QThread):
    """Thread for loading files without freezing UI."""

    progress = pyqtSignal(int, int, str)  # current, total, current_file
    file_found = pyqtSignal(str, str)  # path, name
    finished = pyqtSignal(int)  # total files added

    def __init__(self, paths=None, folder=None, existing_paths=None):
        super().__init__()
        self.paths = paths or []
        self.folder = folder
        self.existing_paths = existing_paths or set()
        self.should_stop = False

    def stop(self):
        self.should_stop = True

    def run(self):
        added = 0

        if self.folder:
            # Scan folder for image files
            all_files = list(Path(self.folder).glob("*"))
            total = len(all_files)

            for idx, p in enumerate(all_files):
                if self.should_stop:
                    break

                self.progress.emit(
                    idx + 1, total, p.name if len(p.name) < 50 else p.name[:47] + "..."
                )

                if p.is_file():
                    path_str = str(p)
                    if path_str not in self.existing_paths:
                        try:
                            # Verify it's an image
                            img = Image.open(p)
                            img.close()
                            self.file_found.emit(path_str, p.name)
                            added += 1
                        except Exception:
                            pass
        else:
            # Process individual file paths
            total = len(self.paths)
            for idx, p in enumerate(self.paths):
                if self.should_stop:
                    break

                fname = Path(p).name if p else "unknown"
                fname = fname if len(fname) < 50 else fname[:47] + "..."
                self.progress.emit(idx + 1, total, fname)

                if p and p not in self.existing_paths:
                    try:
                        # Verify it's an image
                        img = Image.open(p)
                        img.close()
                        name = Path(p).name
                        self.file_found.emit(p, name)
                        added += 1
                    except Exception:
                        pass

        self.finished.emit(added)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("converter")
        self.setMinimumSize(700, 480)

        # Remember settings
        self.last_input_dir = str(Path.home())
        self.last_output_dir = str(Path.home())

        self.layout = QVBoxLayout()

        # File list (batch)
        files_layout = QVBoxLayout()
        files_top = QHBoxLayout()
        files_label = QLabel("Files to convert:")
        self.file_count_label = QLabel("(0 files)")
        files_top.addWidget(files_label)
        files_top.addWidget(self.file_count_label)
        files_top.addStretch()
        add_btn = QPushButton("Add Files")
        add_btn.clicked.connect(self.add_files)
        add_folder_btn = QPushButton("Add Folder")
        add_folder_btn.clicked.connect(self.add_folder)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self.remove_selected)
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self.clear_list)
        files_top.addWidget(add_btn)
        files_top.addWidget(add_folder_btn)
        files_top.addWidget(remove_btn)
        files_top.addWidget(clear_btn)
        files_layout.addLayout(files_top)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragEnabled(True)
        files_layout.addWidget(self.file_list)
        self.layout.addLayout(files_layout)

        # Load presets from config first
        self.load_presets()

        # Quick presets
        presets_group = QGroupBox("Quick Presets")
        presets_layout = QHBoxLayout()

        self.preset_buttons = []
        for preset_key in ["web", "webp", "thumb", "custom1"]:
            preset_data = self.custom_presets[preset_key]
            btn = QPushButton(preset_data["name"])
            btn.setProperty("preset_key", preset_key)
            btn.clicked.connect(
                lambda checked, key=preset_key: self.on_preset_clicked(key)
            )
            self.preset_buttons.append(btn)
            presets_layout.addWidget(btn)

        presets_group.setLayout(presets_layout)
        self.layout.addWidget(presets_group)

        # Output selection (file or folder)
        out_layout = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            "Output file or folder (leave empty to use input folder)"
        )
        out_btn = QPushButton("Choose Folder")
        out_btn.clicked.connect(self.choose_folder)
        out_layout.addWidget(QLabel("Output:"))
        out_layout.addWidget(self.output_edit)
        out_layout.addWidget(out_btn)
        self.layout.addLayout(out_layout)

        # Format and options
        opts_layout = QHBoxLayout()
        self.format_cb = QComboBox()
        self.format_cb.addItems(["png", "jpeg", "webp", "bmp", "gif", "tiff", "ico"])
        self.format_cb.currentTextChanged.connect(self.on_format_changed)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(85)
        self.quality_label = QLabel("Quality:")
        self.resize_check = QCheckBox("Resize")
        self.resize_check.toggled.connect(self.on_resize_toggled)
        self.maintain_aspect = QCheckBox("Keep aspect ratio")
        self.maintain_aspect.setChecked(True)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 10000)
        self.width_spin.setValue(800)
        self.width_spin.valueChanged.connect(self.on_width_changed)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, 10000)
        self.height_spin.setValue(600)
        self.height_spin.valueChanged.connect(self.on_height_changed)
        self.width_spin.setEnabled(False)
        self.height_spin.setEnabled(False)
        self.maintain_aspect.setEnabled(False)

        opts_layout.addWidget(QLabel("Format:"))
        opts_layout.addWidget(self.format_cb)
        opts_layout.addWidget(self.quality_label)
        opts_layout.addWidget(self.quality_spin)
        opts_layout.addWidget(self.resize_check)
        opts_layout.addWidget(self.maintain_aspect)
        opts_layout.addWidget(QLabel("W:"))
        opts_layout.addWidget(self.width_spin)
        opts_layout.addWidget(QLabel("H:"))
        opts_layout.addWidget(self.height_spin)
        self.layout.addLayout(opts_layout)

        # Convert button and progress
        action_layout = QHBoxLayout()
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.start_conversion)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        action_layout.addWidget(self.convert_btn)
        action_layout.addWidget(self.progress)
        self.layout.addLayout(action_layout)

        # Simple status
        self.status = QLabel("Ready • Use Ctrl+O to add files")
        self.status.setWordWrap(True)
        self.layout.addWidget(self.status)

        # Keyboard Shortcuts button
        shortcut_layout = QHBoxLayout()
        shortcut_btn = QPushButton("Keyboard Shortcuts")
        shortcut_btn.clicked.connect(self.show_shortcuts)
        about_btn = QPushButton("About")
        about_btn.clicked.connect(self.show_about)
        shortcut_layout.addStretch()
        shortcut_layout.addWidget(shortcut_btn)
        shortcut_layout.addWidget(about_btn)
        self.layout.addLayout(shortcut_layout)

        self.setLayout(self.layout)

        self.thread = None
        self.original_aspect_ratio = 800 / 600
        self.updating_dimensions = False
        self.file_loader_thread = None
        self.progress_dialog = None
        self.shift_pressed = False

        # Setup keyboard shortcuts
        self.setup_shortcuts()

        # Load presets from config
        self.load_presets()

    def load_presets(self):
        """Load presets from config file."""
        config_path = Path(__file__).parent / "config.json"
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    loaded_presets = json.load(f)
                    # Validate and merge with defaults
                    default_presets = {
                        "web": {
                            "name": "Web Optimized (JPEG 85%)",
                            "format": "jpeg",
                            "quality": 85,
                            "resize": False,
                            "width": 800,
                            "height": 600,
                        },
                        "webp": {
                            "name": "Small WebP",
                            "format": "webp",
                            "quality": 75,
                            "resize": False,
                            "width": 800,
                            "height": 600,
                        },
                        "thumb": {
                            "name": "Thumbnail (200x200)",
                            "format": "jpeg",
                            "quality": 80,
                            "resize": True,
                            "width": 200,
                            "height": 200,
                        },
                        "custom1": {
                            "name": "+",
                            "format": "png",
                            "quality": 85,
                            "resize": False,
                            "width": 800,
                            "height": 600,
                        },
                    }
                    # Merge loaded presets with defaults
                    self.custom_presets = default_presets.copy()
                    self.custom_presets.update(loaded_presets)
            else:
                # Create default config
                self.custom_presets = {
                    "web": {
                        "name": "Web Optimized (JPEG 85%)",
                        "format": "jpeg",
                        "quality": 85,
                        "resize": False,
                        "width": 800,
                        "height": 600,
                    },
                    "webp": {
                        "name": "Small WebP",
                        "format": "webp",
                        "quality": 75,
                        "resize": False,
                        "width": 800,
                        "height": 600,
                    },
                    "thumb": {
                        "name": "Thumbnail (200x200)",
                        "format": "jpeg",
                        "quality": 80,
                        "resize": True,
                        "width": 200,
                        "height": 200,
                    },
                    "custom1": {
                        "name": "+",
                        "format": "png",
                        "quality": 85,
                        "resize": False,
                        "width": 800,
                        "height": 600,
                    },
                }
                self.save_presets()
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(
                f"Warning: Could not load config file: {e}. Recreating with defaults."
            )
            # Try to remove corrupted config file
            try:
                config_path.unlink(missing_ok=True)
            except:
                pass
            # Set defaults
            self.custom_presets = {
                "web": {
                    "name": "Web Optimized (JPEG 85%)",
                    "format": "jpeg",
                    "quality": 85,
                    "resize": False,
                    "width": 800,
                    "height": 600,
                },
                "webp": {
                    "name": "Small WebP",
                    "format": "webp",
                    "quality": 75,
                    "resize": False,
                    "width": 800,
                    "height": 600,
                },
                "thumb": {
                    "name": "Thumbnail (200x200)",
                    "format": "jpeg",
                    "quality": 80,
                    "resize": True,
                    "width": 200,
                    "height": 200,
                },
                "custom1": {
                    "name": "+",
                    "format": "png",
                    "quality": 85,
                    "resize": False,
                    "width": 800,
                    "height": 600,
                },
            }
            self.save_presets()

    def save_presets(self):
        """Save presets to config file."""
        config_path = Path(__file__).parent / "config.json"
        try:
            with open(config_path, "w") as f:
                json.dump(self.custom_presets, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save config file: {e}")

    def setup_shortcuts(self):
        """Setup keyboard shortcuts for common actions."""
        # Ctrl+O to add files
        add_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        add_shortcut.activated.connect(self.add_files)

        # Ctrl+Shift+O to add folder
        add_folder_shortcut = QShortcut(QKeySequence("Ctrl+Shift+O"), self)
        add_folder_shortcut.activated.connect(self.add_folder)

        # Delete to remove selected
        delete_shortcut = QShortcut(QKeySequence("Delete"), self)
        delete_shortcut.activated.connect(self.remove_selected)

        # Ctrl+Return to start conversion
        convert_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        convert_shortcut.activated.connect(self.start_conversion)

        # Ctrl+A to select all files
        select_all_shortcut = QShortcut(QKeySequence("Ctrl+A"), self)
        select_all_shortcut.activated.connect(self.file_list.selectAll)

    def keyPressEvent(self, event):
        """Track Shift key presses."""
        if event.key() == Qt.Key.Key_Shift:
            self.shift_pressed = True
            self.update_preset_button_texts()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Track Shift key releases."""
        if event.key() == Qt.Key.Key_Shift:
            self.shift_pressed = False
            self.update_preset_button_texts()
        super().keyReleaseEvent(event)

    def update_preset_button_texts(self):
        """Update preset button texts based on Shift state."""
        for btn in self.preset_buttons:
            preset_key = btn.property("preset_key")
            preset_data = self.custom_presets[preset_key]
            if preset_key == "custom1" and preset_data["name"] == "+":
                # Special handling for the + button
                if self.shift_pressed:
                    btn.setText("Add Preset")
                    btn.setToolTip("Click to create a new custom preset")
                else:
                    btn.setText("+")
                    btn.setToolTip("Click to create a new custom preset")
            elif self.shift_pressed:
                btn.setText("Edit")
                btn.setToolTip(f'Click to edit "{preset_data["name"]}" preset')
            else:
                btn.setText(preset_data["name"])
                btn.setToolTip(
                    f'Click to apply "{preset_data["name"]}" preset\nHold Shift to edit presets'
                )

    def on_preset_clicked(self, preset_key):
        """Handle preset button clicks."""
        preset_data = self.custom_presets[preset_key]
        if self.shift_pressed or (
            preset_key == "custom1" and preset_data["name"] == "+"
        ):
            self.edit_preset(preset_key)
        else:
            self.apply_preset(preset_key)

    def show_shortcuts(self):
        """Display keyboard shortcuts dialog."""
        shortcuts_text = """
<h3>Keyboard Shortcuts</h3>
<table>
<tr><td><b>Ctrl+O</b></td><td>Add Files</td></tr>
<tr><td><b>Ctrl+Shift+O</b></td><td>Add Folder</td></tr>
<tr><td><b>Delete</b></td><td>Remove Selected Files</td></tr>
<tr><td><b>Ctrl+A</b></td><td>Select All Files</td></tr>
<tr><td><b>Ctrl+Enter</b></td><td>Start Conversion</td></tr>
</table>
        """
        QMessageBox.information(self, "Keyboard Shortcuts - Minimal Converter", shortcuts_text)

    def show_about(self):
        """Display about dialog."""
        about_text = """
<h3>converter</h3>
<p><b>Version:</b> b0.1</p>
<p><b>Description:</b> A simple and efficient converter with batch processing capabilities.</p>

<h3>Dependencies</h3>
<ul>
<li>PyQt6 - GUI framework</li>
<li>Pillow (PIL) - Image processing</li>
</ul>

<h3>License</h3>
<p>This application is open source and available under the MIT License.</p>
<a href="https://github.com/minimaliti/convert">converter on GitHub</a>
        """
        QMessageBox.about(self, "About - Minimal Converter", about_text)

    def apply_preset(self, preset_name):
        """Apply a conversion preset."""
        if preset_name not in self.custom_presets:
            return

        preset = self.custom_presets[preset_name]
        self.format_cb.setCurrentText(preset["format"])
        self.quality_spin.setValue(preset["quality"])
        self.resize_check.setChecked(preset["resize"])
        if preset["resize"]:
            self.width_spin.setValue(preset["width"])
            self.height_spin.setValue(preset["height"])
            self.maintain_aspect.setChecked(
                False
            )  # Custom presets have fixed dimensions
        self.status.setText(f'✓ Preset applied: {preset["name"]}')

    def edit_preset(self, preset_key):
        """Edit a preset."""
        preset = self.custom_presets[preset_key]

        # Create a simple dialog for editing
        from PyQt6.QtWidgets import (
            QDialog,
            QFormLayout,
            QDialogButtonBox,
            QLineEdit,
            QComboBox,
            QSpinBox,
            QCheckBox,
        )

        dialog = QDialog(self)
        dialog.setWindowTitle(f'Edit Preset: {preset["name"]}')
        dialog.setModal(True)

        layout = QFormLayout()

        # Name
        name_edit = QLineEdit(preset["name"])
        layout.addRow("Name:", name_edit)

        # Format
        format_combo = QComboBox()
        format_combo.addItems(["png", "jpeg", "webp", "bmp", "gif", "tiff", "ico"])
        format_combo.setCurrentText(preset["format"])
        layout.addRow("Format:", format_combo)

        # Quality
        quality_spin = QSpinBox()
        quality_spin.setRange(1, 100)
        quality_spin.setValue(preset["quality"])
        layout.addRow("Quality:", quality_spin)

        # Resize
        resize_check = QCheckBox()
        resize_check.setChecked(preset["resize"])
        layout.addRow("Resize:", resize_check)

        # Width
        width_spin = QSpinBox()
        width_spin.setRange(1, 10000)
        width_spin.setValue(preset["width"])
        width_spin.setEnabled(preset["resize"])
        layout.addRow("Width:", width_spin)

        # Height
        height_spin = QSpinBox()
        height_spin.setRange(1, 10000)
        height_spin.setValue(preset["height"])
        height_spin.setEnabled(preset["resize"])
        layout.addRow("Height:", height_spin)

        # Connect resize checkbox to enable/disable dimensions
        resize_check.toggled.connect(lambda checked: width_spin.setEnabled(checked))
        resize_check.toggled.connect(lambda checked: height_spin.setEnabled(checked))

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        dialog.setLayout(layout)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            # Update preset
            new_name = name_edit.text().strip()
            if not new_name:
                new_name = f"Custom ({format_combo.currentText()})"

            self.custom_presets[preset_key] = {
                "name": new_name,
                "format": format_combo.currentText(),
                "quality": quality_spin.value(),
                "resize": resize_check.isChecked(),
                "width": width_spin.value(),
                "height": height_spin.value(),
            }

            # Update button text
            for btn in self.preset_buttons:
                if btn.property("preset_key") == preset_key:
                    btn.setText(new_name)
                    break

            self.status.setText(f'✓ Preset "{new_name}" updated')
            self.save_presets()  # Save changes to config file

        # Reset button texts after dialog closes
        self.shift_pressed = (
            QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        )
        self.update_preset_button_texts()

    def on_format_changed(self, fmt):
        """Enable/disable quality setting based on format."""
        # Quality only applies to JPEG and WebP
        quality_formats = ["jpeg", "webp"]
        enabled = fmt.lower() in quality_formats
        self.quality_spin.setEnabled(enabled)
        self.quality_label.setEnabled(enabled)

    def on_resize_toggled(self, checked):
        """Enable/disable resize options."""
        self.width_spin.setEnabled(checked)
        self.height_spin.setEnabled(checked)
        self.maintain_aspect.setEnabled(checked)

    def on_width_changed(self, value):
        """Update height to maintain aspect ratio if enabled."""
        if self.updating_dimensions or not self.maintain_aspect.isChecked():
            return
        self.updating_dimensions = True
        new_height = int(value / self.original_aspect_ratio)
        self.height_spin.setValue(new_height)
        self.updating_dimensions = False

    def on_height_changed(self, value):
        """Update width to maintain aspect ratio if enabled."""
        if self.updating_dimensions or not self.maintain_aspect.isChecked():
            return
        self.updating_dimensions = True
        new_width = int(value * self.original_aspect_ratio)
        self.width_spin.setValue(new_width)
        self.updating_dimensions = False

    def update_file_count(self):
        """Update the file count label."""
        count = self.file_list.count()
        self.file_count_label.setText(f'({count} file{"s" if count != 1 else ""})')

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select files",
            self.last_input_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tiff *.tif *.ico);;All Files (*.*)",
        )
        if not paths:
            return

        self.last_input_dir = str(Path(paths[0]).parent)

        # Get existing paths to avoid duplicates
        existing_paths = set()
        for i in range(self.file_list.count()):
            existing_paths.add(self.file_list.item(i).data(Qt.ItemDataRole.UserRole))

        # Create progress dialog
        self.progress_dialog = QProgressDialog(
            "Verifying image files...", "Cancel", 0, len(paths), self
        )
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(500)  # Only show if takes > 500ms
        self.progress_dialog.setWindowTitle("Adding Files")
        # Align label to left
        label = self.progress_dialog.findChild(QLabel)
        if label:
            label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

        # Start file loader thread
        self.file_loader_thread = FileLoaderThread(
            paths=paths, existing_paths=existing_paths
        )
        self.file_loader_thread.progress.connect(self._on_load_progress)
        self.file_loader_thread.file_found.connect(self._on_file_found)
        self.file_loader_thread.finished.connect(self._on_load_finished)
        self.progress_dialog.canceled.connect(self._on_load_canceled)

        self.file_loader_thread.start()

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder", self.last_input_dir
        )
        if not folder:
            return

        self.last_input_dir = folder

        # Get existing paths to avoid duplicates
        existing_paths = set()
        for i in range(self.file_list.count()):
            existing_paths.add(self.file_list.item(i).data(Qt.ItemDataRole.UserRole))

        # Count files first for progress bar
        file_count = sum(1 for _ in Path(folder).glob("*"))

        # Create progress dialog
        self.progress_dialog = QProgressDialog(
            f"Scanning folder for images...", "Cancel", 0, file_count, self
        )
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(0)  # Always show for folder operations
        self.progress_dialog.setWindowTitle("Adding Folder")
        # Align label to left
        label = self.progress_dialog.findChild(QLabel)
        if label:
            label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

        # Start file loader thread
        self.file_loader_thread = FileLoaderThread(
            folder=folder, existing_paths=existing_paths
        )
        self.file_loader_thread.progress.connect(self._on_load_progress)
        self.file_loader_thread.file_found.connect(self._on_file_found)
        self.file_loader_thread.finished.connect(self._on_load_finished)
        self.progress_dialog.canceled.connect(self._on_load_canceled)

        self.file_loader_thread.start()

    def _on_load_progress(self, current, total, current_file):
        """Update progress dialog during file loading."""
        if self.progress_dialog and not self.progress_dialog.wasCanceled():
            self.progress_dialog.setMaximum(total)
            self.progress_dialog.setValue(current)
            self.progress_dialog.setLabelText(
                f"Processing {current} of {total}: {current_file}"
            )

    def _on_file_found(self, path, name):
        """Add a verified image file to the list."""
        if self.progress_dialog and self.progress_dialog.wasCanceled():
            return  # Don't add files if operation was canceled
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        self.file_list.addItem(item)
        self.update_file_count()

    def _on_load_finished(self, added):
        """File loading complete."""
        # Disconnect signals first to prevent further updates
        if self.file_loader_thread:
            try:
                self.file_loader_thread.progress.disconnect()
                self.file_loader_thread.file_found.disconnect()
                self.file_loader_thread.finished.disconnect()
            except:
                pass

        if self.progress_dialog:
            try:
                self.progress_dialog.canceled.disconnect()
            except:
                pass
            self.progress_dialog.close()
            self.progress_dialog = None

        if self.file_loader_thread:
            self.file_loader_thread.wait()
            self.file_loader_thread = None

        if added > 0:
            self.status.setText(
                f'✓ Added {added} image{"s" if added != 1 else ""} to the list'
            )
        else:
            self.status.setText("No new image files were added")

    def _on_load_canceled(self):
        """User canceled file loading."""
        if self.file_loader_thread:
            self.file_loader_thread.stop()
            # Disconnect signals to prevent further updates
            try:
                self.file_loader_thread.progress.disconnect()
                self.file_loader_thread.file_found.disconnect()
                self.file_loader_thread.finished.disconnect()
            except:
                pass
            self.file_loader_thread.wait()
            self.file_loader_thread = None

        if self.progress_dialog:
            try:
                self.progress_dialog.canceled.disconnect()
            except:
                pass
            self.progress_dialog.close()
            self.progress_dialog = None

        self.status.setText("File loading canceled")

    def remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self.update_file_count()

    def clear_list(self):
        if self.file_list.count() > 0:
            reply = QMessageBox.question(
                self,
                "Clear all files",
                f"Remove all {self.file_list.count()} files from the list?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.file_list.clear()
                self.update_file_count()

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select output folder", self.last_output_dir
        )
        if folder:
            self.last_output_dir = folder
            self.output_edit.setText(folder)

    def start_conversion(self):
        # collect input files
        inputs = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            stored = item.data(Qt.ItemDataRole.UserRole)
            if stored:
                inputs.append(stored)
            else:
                inputs.append(item.text())

        if not inputs:
            QMessageBox.warning(self, "No input", "Please add files to convert")
            return

        output_text = self.output_edit.text().strip()
        fmt = self.format_cb.currentText()
        quality = self.quality_spin.value()
        size = None
        if self.resize_check.isChecked():
            size = (int(self.width_spin.value()), int(self.height_spin.value()))

        # create job list
        jobs = []
        for inp in inputs:
            inp_path = Path(inp)
            if output_text:
                out_candidate = Path(output_text)
                if out_candidate.exists() and out_candidate.is_dir():
                    out_path = out_candidate / inp_path.with_suffix("." + fmt).name
                else:
                    if len(inputs) > 1:
                        out_dir = (
                            out_candidate
                            if out_candidate.exists() and out_candidate.is_dir()
                            else out_candidate.parent
                        )
                        out_path = out_dir / inp_path.with_suffix("." + fmt).name
                    else:
                        if out_candidate.suffix:
                            out_path = out_candidate
                        else:
                            out_path = (
                                out_candidate / inp_path.with_suffix("." + fmt).name
                            )
            else:
                out_path = inp_path.with_suffix("." + fmt)

            jobs.append(
                {
                    "input": str(inp_path),
                    "output": str(out_path),
                    "format": fmt,
                    "quality": quality,
                    "size": size,
                }
            )

        # prepare scheduler using ThreadPoolExecutor
        self.pending_jobs = list(jobs)
        self.total_jobs = len(jobs)
        self.finished_jobs = 0
        self.max_workers = min(8, os.cpu_count() or 2)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.futures = []

        # disable UI
        self.convert_btn.setEnabled(False)
        self.status.setText(
            f'Converting {self.total_jobs} file{"s" if self.total_jobs != 1 else ""}...'
        )
        self.progress.setValue(0)
        self.failed_jobs = []

        # submit all jobs to the pool
        self.signals = Signals()
        self.signals.job_done.connect(self._on_job_done)
        for job in self.pending_jobs:
            fut = self.executor.submit(self._run_job, job)
            fut.add_done_callback(lambda f: None)  # keep a reference
            self.futures.append(fut)

    def on_finished(self, ok: bool, message: str):
        # legacy single-thread finished handler (not used)
        pass

    def _run_job(self, job):
        try:
            input_path = job["input"]
            output_path = job["output"]
            fmt = job["format"]
            quality = job.get("quality", 85)
            size = job.get("size")
            # quick feature checks for some formats (helpful when Pillow lacks optional codec support)
            in_ext = Path(input_path).suffix.lower().lstrip(".")
            if in_ext == "webp" and not features.check("webp"):
                self.signals.job_done.emit(
                    False,
                    "WebP support is not available in this Pillow build. Install libwebp or a Pillow wheel with WebP support.",
                )
                return

            try:
                img = Image.open(input_path)
            except UnidentifiedImageError as e:
                # more helpful message for unknown / unsupported image formats
                self.signals.job_done.emit(
                    False,
                    f"Cannot identify/open image file: {input_path}. Pillow error: {e}",
                )
                return
            if size:
                img = img.resize(size, Image.Resampling.LANCZOS)

            save_kwargs = {}
            if fmt.lower() in ("jpeg", "jpg"):
                save_kwargs["quality"] = int(quality)
            elif fmt.lower() == "webp":
                save_kwargs["quality"] = int(quality)
            out_dir = Path(output_path).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            if fmt.lower() in ("jpeg", "jpg"):
                img = img.convert("RGB")

            img.save(output_path, fmt.upper(), **save_kwargs)
            # signal success
            self.signals.job_done.emit(True, str(output_path))
        except Exception as e:
            self.signals.job_done.emit(False, str(e))

    def _start_next_job(self):
        # kept for backward compatibility; not used with ThreadPoolExecutor
        pass

    def _on_job_done(self, ok: bool, message: str):
        self.finished_jobs += 1
        # update progress
        self.progress.setValue(int(self.finished_jobs / max(1, self.total_jobs) * 100))

        # Update status with progress
        self.status.setText(f"Converting: {self.finished_jobs}/{self.total_jobs}")

        if not ok:
            self.failed_jobs.append(message)

        if self.finished_jobs >= self.total_jobs:
            # all done
            self.convert_btn.setEnabled(True)
            success_count = self.total_jobs - len(self.failed_jobs)

            if len(self.failed_jobs) == 0:
                self.status.setText(
                    f'✓ Successfully converted {success_count} file{"s" if success_count != 1 else ""}'
                )
                QMessageBox.information(
                    self,
                    "Success",
                    f"All {self.total_jobs} conversions completed successfully!",
                )
            else:
                self.status.setText(
                    f'Completed with {len(self.failed_jobs)} error{"s" if len(self.failed_jobs) != 1 else ""}'
                )
                error_msg = f"Converted {success_count} of {self.total_jobs} files.\n\n"
                error_msg += f"{len(self.failed_jobs)} failed:\n"
                for err in self.failed_jobs[:5]:  # Show first 5 errors
                    error_msg += f"• {err}\n"
                if len(self.failed_jobs) > 5:
                    error_msg += f"... and {len(self.failed_jobs) - 5} more errors"
                QMessageBox.warning(self, "Conversion Complete with Errors", error_msg)

    def closeEvent(self, event):
        """Ensure worker threads are finished or terminated before closing."""
        # Check if file loading is in progress
        if self.file_loader_thread and self.file_loader_thread.isRunning():
            resp = QMessageBox.question(
                self,
                "Loading in progress",
                "File loading is in progress. Cancel and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if resp == QMessageBox.StandardButton.Yes:
                self.file_loader_thread.stop()
                self.file_loader_thread.wait(2000)  # Wait up to 2 seconds
                if self.progress_dialog:
                    self.progress_dialog.close()
            else:
                event.ignore()
                return

        # Check if conversions are running
        if getattr(self, "executor", None):
            running = any(not f.done() for f in getattr(self, "futures", []))
            if running:
                resp = QMessageBox.question(
                    self,
                    "Conversions running",
                    "There are active conversions running. Wait for them to finish before closing?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp == QMessageBox.StandardButton.Yes:
                    # wait for futures to finish
                    for f in getattr(self, "futures", []):
                        try:
                            f.result()
                        except Exception:
                            pass
                    try:
                        self.executor.shutdown(wait=True)
                    except Exception:
                        pass
                    event.accept()
                    return
                else:
                    # attempt to cancel remaining futures and shutdown
                    for f in getattr(self, "futures", []):
                        try:
                            f.cancel()
                        except Exception:
                            pass
                    try:
                        self.executor.shutdown(wait=False)
                    except Exception:
                        pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
