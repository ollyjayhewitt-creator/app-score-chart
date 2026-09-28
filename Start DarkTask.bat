@echo off
rem Runs DarkTask from source. Needs Python 3.8+ from python.org.
python -m pip install --quiet --disable-pip-version-check -r "%~dp0requirements.txt"
start "" pythonw "%~dp0darktask.py" %*
