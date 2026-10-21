"""Only synthetic dependencies, launched by test_d365_combined's real stdio client."""
import sqlite3
import sys
from pathlib import Path
from boring_pondnt import xref
from boring_pondnt.d365_server import create_server

if __name__ == '__main__':
    db, state = sys.argv[1:]
    index = xref.Xref(lambda: sqlite3.connect(db, check_same_thread=False), dialect='sqlite')
    create_server(state_dir=Path(state), start_session=False, xref_index=index).run('stdio')
