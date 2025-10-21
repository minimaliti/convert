# min image convert

A modern Python application for converting between different file types.

## Setup

1. Ensure Python 3.13+ is installed
2. Create virtual environment: `python -m venv .venv`
3. Activate: `.venv\Scripts\activate` (Windows)
4. Install dependencies: `pip install -r requirements.txt`
5. Run: `python main.py`

## Build
You need to have nuitka installed for building.
```bash
python -m nuitka main.py --enable-plugin=pyside6 --windows-console-mode=disable --mode=standalone --output-filename=minimgconvert.exe
```