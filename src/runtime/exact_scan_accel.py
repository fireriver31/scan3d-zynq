"""Optional, integer-exact binary run labelling. Python 3.6 / NumPy 1.13.

Missing compiler/library falls back to the retained Python implementation.
Never requires a new bit, SciPy, a changed mask threshold, or fewer witnesses.
"""
from pathlib import Path
import ctypes,hashlib,os,platform,subprocess,threading,time
import numpy as np

VERSION=89
_LIB=None
_TRIED=False
_ERROR=None
_LOCK=threading.Lock()
_CALLS=0
_NATIVE_S=0.

def status():
    return dict(version=VERSION,backend='native_exact_runs4' if _LIB is not None else 'python_reference',
        load_attempted=_TRIED,error=_ERROR,native_calls=_CALLS,native_elapsed_s=_NATIVE_S,
        board_tested=False)

def _load(compiler='gcc'):
    global _LIB,_TRIED,_ERROR
    if _TRIED:return _LIB
    with _LOCK:
        if _TRIED:return _LIB
        try:
            here=Path(__file__).resolve().parent;source=here/'exact_scan_accel.c'
            tag=hashlib.sha256(source.read_bytes()).hexdigest()[:16]
            suffix='.dll' if os.name=='nt' else '.so'
            output=here/('1026_scan_accel_v89_%s_%s%s'%(platform.machine(),tag,suffix))
            if not output.is_file():
                temp=output.with_name(output.name+'.building.%d'%os.getpid())
                args=[compiler,'-O3','-std=c99','-shared']
                if os.name!='nt':args.append('-fPIC')
                args += [str(source),'-o',str(temp)]
                p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                if p.returncode:raise RuntimeError(p.stderr.decode('utf8','replace')[-1000:])
                os.replace(str(temp),str(output))
            lib=ctypes.CDLL(str(output));lib.scan_regions4.restype=ctypes.c_int32
            lib.scan_regions4.argtypes=[ctypes.c_void_p,ctypes.c_void_p]+[ctypes.c_int32]*3+[ctypes.c_void_p]*4
            _LIB=lib
        except (OSError,RuntimeError,subprocess.TimeoutExpired) as exc:
            _ERROR='%s: %s'%(type(exc).__name__,exc)
        _TRIED=True
        return _LIB

def regions(mask,strong):
    global _CALLS,_NATIVE_S,_ERROR
    if not isinstance(mask,np.ndarray) or not isinstance(strong,np.ndarray):return None
    if mask.dtype!=np.bool_ or strong.dtype!=np.bool_ or mask.ndim!=2 or mask.shape!=strong.shape:return None
    h,w=mask.shape
    if h>480 or w>800:return None
    lib=_load()
    if lib is None:return None
    started=time.monotonic();m=np.ascontiguousarray(mask);s=np.ascontiguousarray(strong)
    capacity=h*((w+1)//2)
    runs=np.empty((capacity,4),np.int32);labels=np.empty(capacity,np.int32)
    area=np.empty(capacity,np.int32);strength=np.empty(capacity,np.int32)
    n=int(lib.scan_regions4(m.ctypes.data,s.ctypes.data,h,w,capacity,
        runs.ctypes.data,labels.ctypes.data,area.ctypes.data,strength.ctypes.data))
    if n<0:
        _ERROR='integer binary labelling returned %d; Python reference used'%n
        return None
    result=([tuple(map(int,row)) for row in runs[:n]],labels[:n].astype(int),
        area[:n].astype(int),strength[:n].astype(int))
    _CALLS+=1;_NATIVE_S+=time.monotonic()-started
    return result

def fast_zip_entry(entry):
    """CPython 3.6-compatible level-1 DEFLATE on this still-empty entry only.

    Does not replace a process-global compressor or alter NPY bytes. Unknown
    ZIP implementations keep their own default compressor unchanged.
    """
    import zipfile,zlib
    if (isinstance(entry,getattr(zipfile,'_ZipWriteFile',())) and
        getattr(entry,'_file_size',None)==0 and getattr(entry,'_compress_size',None)==0 and
        getattr(getattr(entry,'_zinfo',None),'compress_type',None)==zipfile.ZIP_DEFLATED and
        hasattr(entry,'_compressor')):
        entry._compressor=zlib.compressobj(1,zlib.DEFLATED,-15)
        return True
    return False

def save_performance(directory,state,wall_s,cpu_s,self_test):
    """Read back the actual local source fingerprints; never a board-speed claim."""
    if directory is None:return
    import json,sys
    here=Path(__file__).resolve().parent
    names=('pl_mask.py','competitive_colour.py','subject_evidence.py','exact_scan_accel.py','exact_scan_accel.c')
    record=dict(version=VERSION,state=state,elapsed_s=wall_s,cpu_s=cpu_s,
        platform=platform.platform(),machine=platform.machine(),python=sys.version,
        numpy=np.__version__,native=status(),pl_device_self_test_passed=self_test,
        same_output_validation_scope='See delivered offline reports; this record alone is not shape or physical validation',
        sources={name:dict(path=str(here/name),sha256=hashlib.sha256((here/name).read_bytes()).hexdigest()) for name in names})
    path=Path(directory)/'1026_segmentation_performance_v89.json'
    temp=path.with_name(path.name+'.part')
    temp.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    os.replace(str(temp),str(path))
