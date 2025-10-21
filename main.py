import sys
from pathlib import Path
from PIL import Image, features
import json
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QComboBox, QSpinBox, QProgressBar, QCheckBox, QMessageBox,
    QLineEdit, QListWidget, QListWidgetItem, QGroupBox, QProgressDialog,
    QDialog, QFormLayout, QDialogButtonBox
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QMimeData
from PySide6.QtGui import QKeySequence, QShortcut, QDragEnterEvent, QDropEvent
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import subprocess
import logging
import threading


# Enable logging when script is run with -l/--log; enable tracing only with -lt/--log-traces
ARGS = sys.argv[1:]
LOG = any(arg in ('--log', '-l') for arg in ARGS)
TRACE = any(arg in ('--log-traces', '-lt') for arg in ARGS)

logger = logging.getLogger('imgconvert')
_handler = logging.StreamHandler()
_formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
_handler.setFormatter(_formatter)
logger.addHandler(_handler)
logger.setLevel(logging.DEBUG if (LOG or TRACE) else logging.WARNING)

if TRACE:
    logger.debug('Tracing enabled (full trace mode)')
    def _trace_calls(frame, event, arg):
        try:
            co = frame.f_code
            func = co.co_name
            filename = co.co_filename
            lineno = frame.f_lineno
            logger.debug(f'{event.upper()}: {func} - {filename}:{lineno}')
        except Exception:
            # Avoid the tracer raising
            pass
        return _trace_calls

    sys.settrace(_trace_calls)
    threading.settrace(_trace_calls)
elif LOG:
    logger.debug('Logging enabled (debug messages)')


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class ConversionJob:
    """Represents a single conversion job."""
    input_path: str
    output_path: str
    format: str
    quality: int
    size: Optional[Tuple[int, int]] = None


@dataclass
class Preset:
    """Represents a conversion preset."""
    name: str
    format: str
    quality: int
    resize: bool
    width: int
    height: int

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'format': self.format,
            'quality': self.quality,
            'resize': self.resize,
            'width': self.width,
            'height': self.height
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Preset':
        return cls(**data)


# ============================================================================
# Configuration Manager
# ============================================================================

class ConfigManager:
    """Handles loading and saving of presets."""

    DEFAULT_PRESETS = {
        'web': Preset('Web Optimized (JPEG 85%)', 'jpeg', 85, False, 800, 600),
        'webp': Preset('Small WebP', 'webp', 75, False, 800, 600),
        'thumb': Preset('Thumbnail (200x200)', 'jpeg', 80, True, 200, 200),
        'custom1': Preset('+', 'png', 85, False, 800, 600)
    }

    def __init__(self):
        self.config_path = Path(__file__).parent / 'config.json'
        self.presets = self.load()

    def load(self) -> Dict[str, Preset]:
        """Load presets from config file or return defaults."""
        if not self.config_path.exists():
            self.save(self.DEFAULT_PRESETS)
            logger.debug("Config file did not exist; saved default presets.")
            return self.DEFAULT_PRESETS.copy()

        try:
            with open(self.config_path, 'r') as f:
                data = json.load(f)
                logger.debug(f"Loaded config from {self.config_path}")
                return {key: Preset.from_dict(val) for key, val in data.items()}
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.exception(f"Config error: {e}. Using defaults.")
            self.config_path.unlink(missing_ok=True)
            self.save(self.DEFAULT_PRESETS)
            return self.DEFAULT_PRESETS.copy()

    def save(self, presets: Dict[str, Preset]) -> None:
        """Save presets to config file."""
        try:
            data = {key: preset.to_dict() for key, preset in presets.items()}
            with open(self.config_path, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved config to {self.config_path}")
        except Exception as e:
            logger.exception(f"Failed to save config: {e}")


# ============================================================================
# Worker Threads
# ============================================================================

class FileLoaderThread(QThread):
    """Loads and validates image files in background."""

    progress = Signal(int, int, str)  # current, total, filename
    file_found = Signal(str, str)  # path, display_name
    finished = Signal(int)  # files_added

    def __init__(self, paths: List[str] = None, folder: str = None, 
                 existing_paths: set = None):
        super().__init__()
        self.paths = paths or []
        self.folder = folder
        self.existing_paths = existing_paths or set()
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        added = 0
        files_to_check = []

        if self.folder:
            try:
                # Recursively gather files from the folder and subfolders
                files_to_check = [p for p in Path(self.folder).rglob('*') if p.is_file()]
            except Exception:
                files_to_check = []
                logger.exception(f"Failed to iterate folder recursively: {self.folder}")
        else:
            files_to_check = [Path(p) for p in self.paths if p]

        total = len(files_to_check)
        logger.debug(f"FileLoaderThread starting: total={total} folder={self.folder} paths={len(self.paths)}")
        if total == 0:
            self.finished.emit(0)
            logger.debug("FileLoaderThread finished immediately (no files).")
            return

        # Use a thread pool to validate images in parallel and emit progress as tasks complete.
        max_workers = min(8, os.cpu_count() or 2)
        processed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {executor.submit(self._validate_image, path): path for path in files_to_check}

            for future in as_completed(future_to_path):
                if self._stop:
                    logger.debug("FileLoaderThread stopped by user.")
                    break

                path = future_to_path[future]
                processed += 1

                display_name = path.name[:47] + '...' if len(path.name) > 50 else path.name
                try:
                    valid = future.result()
                except Exception as e:
                    logger.exception(f"Error validating image {path}: {e}")
                    valid = False

                # Emit progress with how many have completed and the current filename
                self.progress.emit(processed, total, display_name)
                logger.debug(f"FileLoaderThread progress: {processed}/{total} - {display_name}")

                path_str = str(path)
                if path_str in self.existing_paths:
                    logger.debug(f"Skipping already-added file: {path_str}")
                    continue

                if valid:
                    logger.debug(f"Valid image: {path_str}")
                    self.file_found.emit(path_str, path.name)
                    added += 1
                else:
                    logger.debug(f"Invalid image (skipped): {path_str}")

        self.finished.emit(added)
        logger.debug(f"FileLoaderThread finished: added={added}")

    @staticmethod
    def _validate_image(path: Path) -> bool:
        """Check if file is a valid image."""
        try:
            with Image.open(path) as img:
                img.verify()
            return True
        except Exception:
            return False


class ConversionWorker(QObject):
    """Handles batch image conversion."""

    progress = Signal(int, int)  # current, total
    job_completed = Signal(bool, str)  # success, message
    all_done = Signal(int, int)  # success_count, total_count

    def __init__(self, jobs: List[ConversionJob], max_workers: int = None):
        super().__init__()
        self.jobs = jobs
        self.max_workers = max_workers or min(8, os.cpu_count() or 2)
        self.completed = 0
        self.failed = []

    def run(self, delete_original: bool = False):
        """Execute all conversion jobs, deleting originals if requested."""
        total = len(self.jobs)
        logger.debug(f"ConversionWorker starting: total_jobs={total} delete_original={delete_original}")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_job = {executor.submit(self._convert_image, job, delete_original): job 
                           for job in self.jobs}
            
            for future in as_completed(future_to_job):
                self.completed += 1
                self.progress.emit(self.completed, total)
                logger.debug(f"ConversionWorker progress: {self.completed}/{total}")

                try:
                    success, message = future.result()
                    self.job_completed.emit(success, message)
                    if not success:
                        logger.debug(f"Conversion failed: {message}")
                        self.failed.append(message)
                    else:
                        logger.debug(f"Conversion succeeded: {message}")
                except Exception as e:
                    logger.exception(f"Unexpected error in conversion: {e}")
                    self.job_completed.emit(False, str(e))
                    self.failed.append(str(e))

        success_count = total - len(self.failed)
        logger.debug(f"ConversionWorker finished: success={success_count} total={total} failed={len(self.failed)}")
        self.all_done.emit(success_count, total)

    def _convert_image(self, job: ConversionJob, delete_original: bool = False) -> Tuple[bool, str]:
        """Convert a single image and optionally delete the original if successful."""
        logger.debug(f"Converting: {job.input_path} -> {job.output_path} fmt={job.format} size={job.size}")
        try:
            # Check format support
            if not self._check_format_support(job.input_path):
                logger.debug(f"Unsupported format for: {job.input_path}")
                return False, f"Format not supported: {job.input_path}"

            with Image.open(job.input_path) as img:
                if job.size:
                    img = img.resize(job.size, Image.Resampling.LANCZOS)

                # Ensure output directory exists
                Path(job.output_path).parent.mkdir(parents=True, exist_ok=True)

                # Convert mode if needed
                if job.format.lower() in ('jpeg', 'jpg') and img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                # Save with appropriate options
                save_kwargs = {}
                if job.format.lower() in ('jpeg', 'jpg', 'webp'):
                    save_kwargs['quality'] = job.quality

                img.save(job.output_path, job.format.upper(), **save_kwargs)
                logger.debug(f"Saved output: {job.output_path}")

            # Delete original if requested and output is not the same as input
            if delete_original and job.input_path != job.output_path:
                try:
                    Path(job.input_path).unlink()
                    logger.debug(f"Deleted original: {job.input_path}")
                except Exception as e:
                    logger.exception(f"Failed to delete original {job.input_path}: {e}")
                    return False, f"Converted but failed to delete original: {Path(job.input_path).name}: {str(e)}"

            return True, job.output_path

        except Exception as e:
            logger.exception(f"Error converting {job.input_path}: {e}")
            return False, f"{Path(job.input_path).name}: {str(e)}"

    @staticmethod
    def _check_format_support(path: str) -> bool:
        """Check if image format is supported."""
        ext = Path(path).suffix.lower().lstrip('.')
        if ext == 'webp' and not features.check('webp'):
            logger.debug("WebP not supported by Pillow on this system.")
            return False
        return True


# ============================================================================
# Custom Widgets
# ============================================================================

class FileListWidget(QListWidget):
    """Custom list widget with drag-and-drop support."""

    files_dropped = Signal(list)  # List of file paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls()]
            logger.debug(f"Files dropped: {paths}")
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


# ============================================================================
# Overwrite Dialog
# ============================================================================

class OverwriteDialog(QDialog):
    """Dialog for handling file overwrite conflicts."""

    def __init__(self, file_path: str, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.choice = None  # 'overwrite', 'rename', 'skip'
        self.apply_all = False
        self._init_ui()

    def _init_ui(self):
        self.setWindowTitle('File Already Exists')
        self.setModal(True)
        layout = QVBoxLayout()

        # Message
        msg = QLabel(f'The file "{Path(self.file_path).name}" already exists.\nWhat would you like to do?')
        msg.setWordWrap(True)
        layout.addWidget(msg)

        # Buttons
        button_layout = QHBoxLayout()
        
        overwrite_btn = QPushButton('Overwrite')
        overwrite_btn.clicked.connect(lambda: self._set_choice('overwrite'))
        button_layout.addWidget(overwrite_btn)

        rename_btn = QPushButton('Rename')
        rename_btn.clicked.connect(lambda: self._set_choice('rename'))
        button_layout.addWidget(rename_btn)

        skip_btn = QPushButton('Skip')
        skip_btn.clicked.connect(lambda: self._set_choice('skip'))
        button_layout.addWidget(skip_btn)

        layout.addLayout(button_layout)

        # Apply to all checkbox
        self.apply_all_cb = QCheckBox('Apply to all remaining conflicts')
        layout.addWidget(self.apply_all_cb)

        self.setLayout(layout)

    def _set_choice(self, choice: str):
        self.choice = choice
        self.apply_all = self.apply_all_cb.isChecked()
        logger.debug(f"Overwrite dialog choice: {choice} apply_all={self.apply_all} file={self.file_path}")
        self.accept()


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('min image convert')
        self.setMinimumSize(750, 520)

        # State
        self.config_manager = ConfigManager()
        self.last_input_dir = str(Path.home())
        self.last_output_dir = str(Path.home())
        self.shift_pressed = False
        self.updating_dimensions = False
        self.original_aspect_ratio = 4/3
        self.file_loader_thread = None
        self.progress_dialog = None
        self.conversion_thread = None
        self.current_jobs = []

        self._init_ui()
        self._setup_shortcuts()
        self._connect_signals()
        logger.debug("MainWindow initialized")

    def _init_ui(self):
        """Initialize user interface."""
        layout = QVBoxLayout()

        # File list section
        layout.addWidget(self._create_file_list_section())

        # Quick presets
        layout.addWidget(self._create_presets_section())

        # Output selection
        layout.addLayout(self._create_output_section())

        # Conversion options
        layout.addLayout(self._create_options_section())

        # Action buttons and progress
        layout.addLayout(self._create_action_section())

        # Status bar
        self.status = QLabel('Ready • Drag images here or press Ctrl+O')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # Footer buttons
        layout.addLayout(self._create_footer_section())

        self.setLayout(layout)

    def _create_file_list_section(self) -> QGroupBox:
        """Create file list section."""
        group = QGroupBox('Images to Convert')
        layout = QVBoxLayout()

        # Top bar with count and buttons
        top_bar = QHBoxLayout()
        self.file_count_label = QLabel('(0 images)')
        top_bar.addWidget(self.file_count_label)
        top_bar.addStretch()

        for text, callback in [
            ('Add Files', self.add_files),
            ('Add Folder', self.add_folder),
            ('Remove', self.remove_selected),
            ('Clear', self.clear_list)
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(callback)
            top_bar.addWidget(btn)

        layout.addLayout(top_bar)

        # File list with drag-drop support
        self.file_list = FileListWidget()
        self.file_list.files_dropped.connect(self.handle_dropped_files)
        layout.addWidget(self.file_list)

        group.setLayout(layout)
        return group

    def _create_presets_section(self) -> QGroupBox:
        """Create quick presets section."""
        group = QGroupBox('Quick Presets (Hold Shift to Edit)')
        layout = QHBoxLayout()

        self.preset_buttons = []
        for key in ['web', 'webp', 'thumb', 'custom1']:
            preset = self.config_manager.presets[key]
            btn = QPushButton(preset.name)
            btn.setProperty('preset_key', key)
            btn.clicked.connect(lambda checked, k=key: self.on_preset_clicked(k))
            self.preset_buttons.append(btn)
            layout.addWidget(btn)

        group.setLayout(layout)
        return group

    def _create_output_section(self) -> QHBoxLayout:
        """Create output selection section."""
        layout = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            'Output folder (leave empty to use input folder)'
        )
        browse_btn = QPushButton('Browse...')
        browse_btn.clicked.connect(self.choose_folder)
        open_btn = QPushButton('Open')
        open_btn.clicked.connect(self.open_output_folder)

        layout.addWidget(QLabel('Output:'))
        layout.addWidget(self.output_edit, 1)
        layout.addWidget(browse_btn)
        layout.addWidget(open_btn)
        return layout

    def _create_options_section(self) -> QHBoxLayout:
        """Create conversion options section."""
        layout = QHBoxLayout()

        # Format selection
        self.format_cb = QComboBox()
        self.format_cb.addItems(['png', 'jpeg', 'webp', 'bmp', 'gif', 'tiff', 'ico'])
        layout.addWidget(QLabel('Format:'))
        layout.addWidget(self.format_cb)

        # Quality
        self.quality_label = QLabel('Quality:')
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(85)
        layout.addWidget(self.quality_label)
        layout.addWidget(self.quality_spin)

        # Resize options
        self.resize_check = QCheckBox('Resize')
        self.maintain_aspect = QCheckBox('Keep aspect')
        self.maintain_aspect.setChecked(True)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 10000)
        self.width_spin.setValue(800)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(1, 10000)
        self.height_spin.setValue(600)

        for widget in [self.width_spin, self.height_spin, self.maintain_aspect]:
            widget.setEnabled(False)

        layout.addWidget(self.resize_check)
        layout.addWidget(self.maintain_aspect)
        layout.addWidget(QLabel('W:'))
        layout.addWidget(self.width_spin)
        layout.addWidget(QLabel('H:'))
        layout.addWidget(self.height_spin)

        # Delete original files option
        self.delete_original_check = QCheckBox('Delete originals after conversion')
        layout.addWidget(self.delete_original_check)

        return layout

    def _create_action_section(self) -> QHBoxLayout:
        """Create action buttons and progress bar."""
        layout = QHBoxLayout()
        self.convert_btn = QPushButton('Convert')
        self.convert_btn.setMinimumHeight(35)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        
        layout.addWidget(self.convert_btn, 1)
        layout.addWidget(self.progress_bar, 2)
        return layout

    def _create_footer_section(self) -> QHBoxLayout:
        """Create footer with info buttons."""
        layout = QHBoxLayout()
        layout.addStretch()
        
        shortcuts_btn = QPushButton('Shortcuts')
        shortcuts_btn.clicked.connect(self.show_shortcuts)
        about_btn = QPushButton('About')
        about_btn.clicked.connect(self.show_about)
        
        layout.addWidget(shortcuts_btn)
        layout.addWidget(about_btn)
        return layout

    def _connect_signals(self):
        """Connect all signals."""
        self.format_cb.currentTextChanged.connect(self.on_format_changed)
        self.resize_check.toggled.connect(self.on_resize_toggled)
        self.width_spin.valueChanged.connect(self.on_width_changed)
        self.height_spin.valueChanged.connect(self.on_height_changed)
        self.convert_btn.clicked.connect(self.start_conversion)

    def _setup_shortcuts(self):
        """Setup keyboard shortcuts."""
        shortcuts = [
            ('Ctrl+O', self.add_files),
            ('Ctrl+Shift+O', self.add_folder),
            ('Delete', self.remove_selected),
            ('Ctrl+Return', self.start_conversion),
            ('Ctrl+A', self.file_list.selectAll)
        ]
        for key, func in shortcuts:
            QShortcut(QKeySequence(key), self).activated.connect(func)

    # ========================================================================
    # Event Handlers
    # ========================================================================

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Shift:
            self.shift_pressed = True
            self._update_preset_buttons()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Shift:
            self.shift_pressed = False
            self._update_preset_buttons()
        super().keyReleaseEvent(event)

    def on_format_changed(self, fmt: str):
        """Enable/disable quality for lossy formats."""
        enabled = fmt.lower() in ['jpeg', 'webp']
        self.quality_spin.setEnabled(enabled)
        self.quality_label.setEnabled(enabled)

    def on_resize_toggled(self, checked: bool):
        """Enable/disable resize options."""
        for widget in [self.width_spin, self.height_spin, self.maintain_aspect]:
            widget.setEnabled(checked)

    def on_width_changed(self, value: int):
        """Update height when width changes (maintain aspect ratio)."""
        if not self.updating_dimensions and self.maintain_aspect.isChecked():
            self.updating_dimensions = True
            self.height_spin.setValue(int(value / self.original_aspect_ratio))
            self.updating_dimensions = False

    def on_height_changed(self, value: int):
        """Update width when height changes (maintain aspect ratio)."""
        if not self.updating_dimensions and self.maintain_aspect.isChecked():
            self.updating_dimensions = True
            self.width_spin.setValue(int(value * self.original_aspect_ratio))
            self.updating_dimensions = False

    # ========================================================================
    # File Management
    # ========================================================================

    def handle_dropped_files(self, paths: List[str]):
        """Handle files/folders dropped onto the list."""
        files = []
        folders = []

        for path in paths:
            p = Path(path)
            if p.is_file():
                files.append(str(p))
            elif p.is_dir():
                folders.append(str(p))

        # Collect files from all dropped folders and add them together to a single loader.
        for folder in folders:
            try:
                # recursively include files in subfolders
                for p in Path(folder).rglob('*'):
                    if p.is_file():
                        files.append(str(p))
            except Exception:
                # Ignore folders we can't read and continue with others
                logger.exception(f"Failed to iterate folder recursively: {folder}")
                continue

        if files:
            # Deduplicate while preserving order
            seen = set()
            uniq_files = []
            for f in files:
                if f not in seen:
                    seen.add(f)
                    uniq_files.append(f)
            self._load_files(uniq_files)

    def add_files(self):
        """Open file dialog to add files."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Select Images', self.last_input_dir,
            'Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tiff *.ico);;All (*.*)'
        )
        if paths:
            self.last_input_dir = str(Path(paths[0]).parent)
            self._load_files(paths)

    def add_folder(self):
        """Open folder dialog to add all images from folder."""
        folder = QFileDialog.getExistingDirectory(
            self, 'Select Folder', self.last_input_dir
        )
        if folder:
            self.last_input_dir = folder
            self._load_folder(folder)

    def _load_files(self, paths: List[str]):
        """Load files in background thread."""
        existing = self._get_existing_paths()
        self._start_file_loader(paths=paths, existing_paths=existing)

    def _load_folder(self, folder: str):
        """Load folder contents in background thread."""
        existing = self._get_existing_paths()
        self._start_file_loader(folder=folder, existing_paths=existing)

    def _start_file_loader(self, paths: List[str] = None, folder: str = None,
                          existing_paths: set = None):
        """Start file loading thread with progress dialog."""
        if paths:
            count = len(paths)
        else:
            try:
                count = sum(1 for p in Path(folder).rglob('*') if p.is_file())
            except Exception:
                logger.exception(f"Failed to count files recursively in folder: {folder}")
                count = 0
        logger.debug(f"Starting file loader: count={count} folder={folder} paths_provided={bool(paths)}")

        self.progress_dialog = QProgressDialog(
            'Loading files...', 'Cancel', 0, count, self
        )
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setWindowTitle('Adding files')
        self.progress_dialog.setMinimumDuration(300)

        self.file_loader_thread = FileLoaderThread(paths, folder, existing_paths)
        self.file_loader_thread.progress.connect(self._on_load_progress)
        self.file_loader_thread.file_found.connect(self._on_file_found)
        self.file_loader_thread.finished.connect(self._on_load_finished)
        self.progress_dialog.canceled.connect(self._on_load_canceled)
        
        self.file_loader_thread.start()
        logger.debug("FileLoaderThread started (QThread)")

    def _on_load_progress(self, current: int, total: int, filename: str):
        """Update loading progress."""
        logger.debug(f"Load progress: {current}/{total} - {filename}")
        if self.progress_dialog is not None:
            self.progress_dialog.setMaximum(total)
            self.progress_dialog.setValue(current)
            if self.progress_dialog is not None: # because that works for some reason
                self.progress_dialog.setLabelText(f'<div align="left">Adding: {filename}</div>')
            # QProgressDialog does not support setAlignment, but we can left-align the label text using HTML
            
    def _on_file_found(self, path: str, name: str):
        """Add validated file to list."""
        logger.debug(f"File found and added to list: {path}")
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        self.file_list.addItem(item)
        self._update_file_count()

    def _on_load_finished(self, added: int):
        """File loading complete."""
        self._cleanup_file_loader()
        msg = f'✓ Added {added} file{"s" if added != 1 else ""}' if added > 0 else 'No new files added'
        self.status.setText(msg)

    def _on_load_canceled(self):
        """User canceled loading."""
        if self.file_loader_thread:
            self.file_loader_thread.stop()
        self._cleanup_file_loader()
        self.status.setText('Loading canceled')

    def _cleanup_file_loader(self):
        """Clean up file loader thread and dialog."""
        if self.file_loader_thread:
            self.file_loader_thread.wait()
            self.file_loader_thread = None
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None

    def remove_selected(self):
        """Remove selected files from list."""
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self._update_file_count()

    def clear_list(self):
        """Clear all files from list."""
        if self.file_list.count() > 0:
            reply = QMessageBox.question(
                self, 'Clear All',
                f'Remove all {self.file_list.count()} files?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.file_list.clear()
                self._update_file_count()

    def _get_existing_paths(self) -> set:
        """Get set of currently loaded file paths."""
        return {
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
        }

    def _update_file_count(self):
        """Update file count label."""
        count = self.file_list.count()
        self.file_count_label.setText(f'({count} file{"s" if count != 1 else ""})')

    def choose_folder(self):
        """Choose output folder."""
        folder = QFileDialog.getExistingDirectory(
            self, 'Select Output Folder', self.last_output_dir
        )
        if folder:
            self.last_output_dir = folder
            self.output_edit.setText(folder)

    def open_output_folder(self):
        """Open the folder provided in the output field."""
        folder = self.output_edit.text().strip()
        if not folder:
            QMessageBox.information(self, "Open Folder", "No output folder specified.")
            return
        path = Path(folder)
        if not path.exists():
            QMessageBox.warning(self, "Open Folder", "Folder does not exist.")
            return
        if not path.is_dir():
            path = path.parent
        try:
            if sys.platform.startswith('darwin'):
                subprocess.Popen(['open', str(path)])
            elif os.name == 'nt':
                os.startfile(str(path))
            elif os.name == 'posix':
                subprocess.Popen(['xdg-open', str(path)])
            else:
                QMessageBox.warning(self, "Open Folder", "Unsupported OS.")
        except Exception as e:
            QMessageBox.warning(self, "Open Folder", f"Could not open folder:\n{e}")

    # ========================================================================
    # Preset Management
    # ========================================================================

    def _update_preset_buttons(self):
        """Update preset button appearance based on Shift state."""
        for btn in self.preset_buttons:
            key = btn.property('preset_key')
            preset = self.config_manager.presets[key]
            
            if self.shift_pressed:
                btn.setText('Edit' if key != 'custom1' or preset.name != '+' else 'Add')
                btn.setToolTip(f'Edit "{preset.name}"' if preset.name != '+' else 'Add preset')
            else:
                btn.setText(preset.name)
                btn.setToolTip(f'Apply "{preset.name}"\nShift+Click to edit')

    def on_preset_clicked(self, key: str):
        """Handle preset button click."""
        preset = self.config_manager.presets[key]
        if self.shift_pressed or (key == 'custom1' and preset.name == '+'):
            self._edit_preset(key)
        else:
            self._apply_preset(key)

    def _apply_preset(self, key: str):
        """Apply preset to current settings."""
        preset = self.config_manager.presets[key]
        self.format_cb.setCurrentText(preset.format)
        self.quality_spin.setValue(preset.quality)
        self.resize_check.setChecked(preset.resize)
        if preset.resize:
            self.width_spin.setValue(preset.width)
            self.height_spin.setValue(preset.height)
        self.status.setText(f'✓ Applied: {preset.name}')

    def _edit_preset(self, key: str):
        """Open preset editor dialog."""
        preset = self.config_manager.presets[key]
        
        dialog = QDialog(self)
        dialog.setWindowTitle(f'Edit Preset: {preset.name}')
        dialog.setModal(True)
        layout = QFormLayout()

        # Input fields
        name_edit = QLineEdit(preset.name)
        format_combo = QComboBox()
        format_combo.addItems(['png', 'jpeg', 'webp', 'bmp', 'gif', 'tiff', 'ico'])
        format_combo.setCurrentText(preset.format)
        quality_spin = QSpinBox()
        quality_spin.setRange(1, 100)
        quality_spin.setValue(preset.quality)
        resize_check = QCheckBox()
        resize_check.setChecked(preset.resize)
        width_spin = QSpinBox()
        width_spin.setRange(1, 10000)
        width_spin.setValue(preset.width)
        width_spin.setEnabled(preset.resize)
        height_spin = QSpinBox()
        height_spin.setRange(1, 10000)
        height_spin.setValue(preset.height)
        height_spin.setEnabled(preset.resize)

        resize_check.toggled.connect(width_spin.setEnabled)
        resize_check.toggled.connect(height_spin.setEnabled)

        layout.addRow('Name:', name_edit)
        layout.addRow('Format:', format_combo)
        layout.addRow('Quality:', quality_spin)
        layout.addRow('Resize:', resize_check)
        layout.addRow('Width:', width_spin)
        layout.addRow('Height:', height_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        dialog.setLayout(layout)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = name_edit.text().strip() or f'Custom ({format_combo.currentText()})'
            self.config_manager.presets[key] = Preset(
                name, format_combo.currentText(), quality_spin.value(),
                resize_check.isChecked(), width_spin.value(), height_spin.value()
            )
            self.config_manager.save(self.config_manager.presets)
            
            # Update button
            for btn in self.preset_buttons:
                if btn.property('preset_key') == key:
                    btn.setText(name)
                    break
            
            self.status.setText(f'✓ Preset "{name}" updated')

        self.shift_pressed = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        self._update_preset_buttons()

    # ========================================================================
    # Conversion
    # ========================================================================

    def start_conversion(self):
        """Start batch conversion process."""
        logger.debug("Start conversion requested")
        # Collect input files
        inputs = [
            self.file_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.file_list.count())
        ]

        if not inputs:
            logger.debug("No files to convert (user alerted)")
            QMessageBox.warning(self, 'No Files', 'Add files to convert first.')
            return

        # Build conversion jobs
        jobs = self._build_conversion_jobs(inputs)
        if not jobs:
            logger.debug("No conversion jobs after build (maybe all skipped).")
            return

        self.current_jobs = jobs  # Store for deletion after conversion

        # Determine if originals should be deleted (with double confirmation)
        delete_original = False
        if self.delete_original_check.isChecked():
            logger.debug("User requested deletion of originals; requesting confirmations.")
            reply1 = QMessageBox.question(
                self, 'Delete Original Files',
                f'Are you sure you want to delete the {len(jobs)} original file{"s" if len(jobs) != 1 else ""} after conversion?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply1 == QMessageBox.StandardButton.Yes:
                reply2 = QMessageBox.question(
                    self, 'Confirm Deletion',
                    'This action cannot be undone. Really delete the original files after conversion?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply2 == QMessageBox.StandardButton.Yes:
                    delete_original = True
                    logger.debug("User confirmed deletion of originals.")
                else:
                    logger.debug("User declined final deletion confirmation.")
            else:
                logger.debug("User declined deletion confirmation (first prompt).")

        # Run conversion in thread
        logger.debug(f"Spawning conversion thread, delete_original={delete_original}")
        self.conversion_thread = QThread()
        self.conversion_worker = ConversionWorker(jobs)
        self.conversion_worker.moveToThread(self.conversion_thread)
        
        # Pass delete_original to worker
        self.conversion_thread.started.connect(lambda: self.conversion_worker.run(delete_original))
        self.conversion_worker.progress.connect(self._on_conversion_progress)
        self.conversion_worker.all_done.connect(self._on_conversion_finished)
        
        self.conversion_thread.start()
        logger.debug("Conversion QThread started")

    def _build_conversion_jobs(self, inputs: List[str]) -> List[ConversionJob]:
        """Build list of conversion jobs."""
        output_text = self.output_edit.text().strip()
        fmt = self.format_cb.currentText()
        quality = self.quality_spin.value()
        size = None
        
        if self.resize_check.isChecked():
            size = (self.width_spin.value(), self.height_spin.value())

        jobs = []
        for inp in inputs:
            inp_path = Path(inp)
            out_path = self._determine_output_path(inp_path, output_text, fmt, len(inputs))
            jobs.append(ConversionJob(str(inp_path), str(out_path), fmt, quality, size))

        # Check for overwrite conflicts
        resolved_jobs = []
        apply_all_choice = None
        for job in jobs:
            out_path = Path(job.output_path)
            if out_path.exists():
                if apply_all_choice is not None:
                    choice = apply_all_choice
                else:
                    dialog = OverwriteDialog(job.output_path, self)
                    dialog.exec()
                    choice = dialog.choice
                    if dialog.apply_all:
                        apply_all_choice = choice
                
                if choice == 'overwrite':
                    resolved_jobs.append(job)
                elif choice == 'rename':
                    new_path = self._generate_unique_path(out_path)
                    resolved_jobs.append(ConversionJob(job.input_path, str(new_path), job.format, job.quality, job.size))
                elif choice == 'skip':
                    continue  # Skip this job
            else:
                resolved_jobs.append(job)

        return resolved_jobs

    def _generate_unique_path(self, path: Path) -> Path:
        """Generate a unique path by adding a suffix if needed."""
        if not path.exists():
            return path
        
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        
        counter = 1
        while True:
            new_path = parent / f"{stem} ({counter}){suffix}"
            if not new_path.exists():
                return new_path
            counter += 1

    def _determine_output_path(self, inp_path: Path, output_text: str, 
                               fmt: str, total_files: int) -> Path:
        """Determine output path for a file."""
        if not output_text:
            return inp_path.with_suffix(f'.{fmt}')

        out_candidate = Path(output_text)
        
        if out_candidate.is_dir():
            return out_candidate / inp_path.with_suffix(f'.{fmt}').name
        
        if total_files == 1 and out_candidate.suffix:
            return out_candidate
        
        out_dir = out_candidate if out_candidate.is_dir() else out_candidate.parent
        return out_dir / inp_path.with_suffix(f'.{fmt}').name

    def _on_conversion_progress(self, current: int, total: int):
        """Update conversion progress."""
        logger.debug(f"Conversion progress: {current}/{total}")
        self.progress_bar.setValue(int(current / total * 100))
        self.status.setText(f'Converting: {current}/{total}')

    def _on_conversion_finished(self, success: int, total: int):
        """Conversion complete."""
        logger.debug(f"Conversion finished: success={success} total={total}")
        self.convert_btn.setEnabled(True)
        self.progress_bar.setValue(100)
        
        failed = total - success
        if failed == 0:
            self.status.setText(f'✓ Converted {success} file{"s" if success != 1 else ""}')
            QMessageBox.information(self, 'Success', f'Converted {total} files!')
        else:
            self.status.setText(f'Completed with {failed} error{"s" if failed != 1 else ""}')
            QMessageBox.warning(
                self, 'Completed with Errors',
                f'Converted {success} of {total} files.\n{failed} failed.'
            )

        if self.conversion_thread:
            self.conversion_thread.quit()
            self.conversion_thread.wait()
            self.conversion_thread = None

    # ========================================================================
    # Dialogs
    # ========================================================================

    def show_shortcuts(self):
        """Show keyboard shortcuts dialog."""
        QMessageBox.information(
            self, 'Keyboard Shortcuts',
            '<h3>Shortcuts</h3>'
            '<table>'
            '<tr><td><b>Ctrl+O</b></td><td>Add Files</td></tr>'
            '<tr><td><b>Ctrl+Shift+O</b></td><td>Add Folder</td></tr>'
            '<tr><td><b>Delete</b></td><td>Remove Selected</td></tr>'
            '<tr><td><b>Ctrl+A</b></td><td>Select All</td></tr>'
            '<tr><td><b>Ctrl+Enter</b></td><td>Convert</td></tr>'
            '</table>'
        )

    def show_about(self):
        """Show about dialog."""
        QMessageBox.about(
            self, 'About',
            '<h3>min image convert</h3>'
            '<p><b>Version:</b> b0.3</p>'
            '<p>Simple, efficient batch image converter</p>'
            '<p><b>Libraries:</b> PySide6, Pillow</p>'
            '<p><b>License:</b> <a href="https://opensource.org/licenses/MIT">MIT</a></p>'
            '<p><a href="https://github.com/minimaliti/imgconvert/">GitHub</a></p>'
        )

    def closeEvent(self, event):
        """Handle application close."""
        # Check for active operations
        if self.file_loader_thread and self.file_loader_thread.isRunning():
            reply = QMessageBox.question(
                self, 'Loading in Progress',
                'Cancel loading and close?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.file_loader_thread.stop()
                self.file_loader_thread.wait(2000)
            else:
                event.ignore()
                return

        if self.conversion_thread and self.conversion_thread.isRunning():
            reply = QMessageBox.question(
                self, 'Conversion in Progress',
                'Wait for conversion to finish?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.conversion_thread.wait()
            else:
                self.conversion_thread.quit()

        event.accept()


# ============================================================================
# Application Entry Point
# ============================================================================

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
