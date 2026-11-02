"""One cooperating notebook process may own the physical TFT. Python 3.6.

An OS lock survives a Python module reload. The file itself is never deleted;
process exit releases its lock. This is not permission to reprogram a running
FPGA from a different kernel. Always stop the old notebook first.
"""
from pathlib import Path
import os

VERSION=88

class ScreenLease:
    def __init__(self,path=None):
        self.file=None
        path=Path(path) if path else Path(__file__).resolve().parent/'1026_screen_owner_v88.lock'
        f=path.open('a+b')
        try:
            f.seek(0,2)
            if f.tell()==0:f.write(b'0');f.flush()
            f.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            f.close()
            raise RuntimeError('实体屏已被另一内核占用；先结束旧内核的UI，再绑定本内核：%s'%exc)
        self.file=f

    def close(self):
        f=self.file
        if f is None:return
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        f.close();self.file=None
