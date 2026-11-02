"""Read the existing local mesh; derive a bounded display mesh, never overwrite it.

Python 3.6 / NumPy 1.13. Windows DLLs are for offline tests only. The board
compiles its own ARM .so with its own gcc; no OpenGL/desktop/browser required.
"""
from pathlib import Path
import ctypes
import hashlib
import math
import os
import platform
import subprocess
import numpy as np

VERSION=87
MAX_SOURCE_VERTICES=4000000
MAX_SOURCE_FACES=4000000

def load_mesh(path):
    path=Path(path)
    with path.open('rb') as f:
        lines=[]
        while True:
            line=f.readline(1024)
            if not line or len(lines)>100:raise ValueError('PLY头不完整')
            text=line.decode('ascii').strip();lines.append(text)
            if text=='end_header':break
        if lines[:2]!=['ply','format binary_little_endian 1.0']:
            raise ValueError('实体屏查看器需要本流程输出的二进制三角PLY')
        vertex_count=face_count=0;props=[];element=None
        for line in lines[2:]:
            fields=line.split()
            if not fields:continue
            if fields[0]=='element':
                element=fields[1]
                if element=='vertex':vertex_count=int(fields[2])
                elif element=='face':face_count=int(fields[2])
                else:raise ValueError('不支持该PLY元素')
            elif fields[0]=='property':props.append((element,' '.join(fields[1:])))
        if not 0<vertex_count<=MAX_SOURCE_VERTICES or not 0<face_count<=MAX_SOURCE_FACES:
            raise ValueError('PLY面数或点数超出显示读取范围')
        vp=[s for e,s in props if e=='vertex'];fp=[s for e,s in props if e=='face']
        has_color=vp==['float x','float y','float z','uchar red','uchar green','uchar blue']
        if not has_color and vp!=['float x','float y','float z']:
            raise ValueError('不支持该PLY顶点格式')
        if fp!=['list uchar int vertex_indices']:raise ValueError('不支持该PLY面格式')
        dt=np.dtype([('p','<f4',(3,))]+([('rgb','u1',(3,))] if has_color else []))
        vertices=np.fromfile(f,dtype=dt,count=vertex_count)
        faces=np.fromfile(f,dtype=[('n','u1'),('i','<i4',(3,))],count=face_count)
        if len(vertices)!=vertex_count or len(faces)!=face_count or f.read(1):
            raise ValueError('PLY正文长度不匹配')
    p=np.ascontiguousarray(vertices['p']);t=np.ascontiguousarray(faces['i'])
    if not np.isfinite(p).all() or not (faces['n']==3).all() or t.min()<0 or t.max()>=len(p):
        raise ValueError('PLY坐标或三角索引无效')
    rgb=np.ascontiguousarray(vertices['rgb']) if has_color else np.full(p.shape,180,np.uint8)
    return p,t,rgb

def display_mesh(p,t,rgb,max_faces=22000):
    """Only an independent display copy is clustered. Never random-delete faces.

    Original corners within one small cell share their mean position/color.
    Small gaps/features can change in the lightweight view; full PLY is retained.
    """
    p=np.asarray(p,np.float32);t=np.asarray(t,np.int32);rgb=np.asarray(rgb,np.uint8)
    lower=p.min(0);extent=p.max(0)-lower
    radius=float(np.max(extent))*.5
    if not math.isfinite(radius) or radius<=0:raise ValueError('模型范围无效')
    src_faces=len(t);cell_mm=0.
    if len(t)>max_faces:
        # Start near the surface-face budget rather than repeatedly sorting
        # the full source at unnecessarily fine 160/128/... grids on Cortex A9.
        first=int(min(96,max(8,math.sqrt(max_faces/7.))))
        grids=sorted(set(max(8,int(round(first*k))) for k in (1.,.85,.70,.55,.40,.25)),reverse=True)
        grids+=[] if 8 in grids else [8]
        for divisions in grids:
            cell_mm=2*radius/divisions
            key=np.floor((p-lower)/cell_mm).astype(np.int64)
            stride=divisions+2
            linear=key[:,0]+stride*(key[:,1]+stride*key[:,2])
            _,inv=np.unique(linear,return_inverse=True)
            n=int(inv.max())+1
            clustered=np.zeros((n,3),np.float64);col=np.zeros((n,3),np.float64)
            count=np.bincount(inv,minlength=n)
            for c in range(3):
                clustered[:,c]=np.bincount(inv,weights=p[:,c],minlength=n)/count
                col[:,c]=np.bincount(inv,weights=rgb[:,c],minlength=n)/count
            face=inv[t]
            good=(face[:,0]!=face[:,1])&(face[:,1]!=face[:,2])&(face[:,0]!=face[:,2])
            face=face[good]
            # Preserve one representative's winding when removing duplicates.
            _,ix=np.unique(np.sort(face,axis=1),axis=0,return_index=True)
            face=face[np.sort(ix)]
            if len(face)<=max_faces:
                p=clustered.astype(np.float32);t=face.astype(np.int32)
                rgb=np.clip(np.rint(col),0,255).astype(np.uint8);break
        else:raise ValueError('无法在显示面数预算内生成网格')
    if not len(t):raise ValueError('显示简化后没有有效三角面')
    center=(p.max(0)+p.min(0))*.5
    p=(p-center)/radius
    return (np.ascontiguousarray(p,np.float32),np.ascontiguousarray(t,np.int32),
            np.ascontiguousarray(rgb,np.uint8),
            dict(source_faces=src_faces,display_faces=len(t),display_cell_mm=cell_mm,
                 source_file_modified=False,volume_modified=False,display_only=True))

def native_library(compiler='gcc'):
    here=Path(__file__).resolve().parent;source=here/'tft_raster.c'
    suffix='.dll' if os.name=='nt' else '.so'
    tag=hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    output=here/('1026_tft_raster_v87_%s_%s%s'%(platform.machine(),tag,suffix))
    if not output.is_file():
        temp=output.with_name(output.name+'.building')
        args=[compiler,'-O3','-std=c99','-shared']
        if os.name!='nt':args+=['-fPIC']
        args += [str(source),'-o',str(temp),'-lm']
        try:
            result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40)
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise RuntimeError('模型渲染内核编译失败，请确认板上gcc可用：%s'%exc)
        if result.returncode:
            raise RuntimeError('渲染内核编译失败：'+result.stderr.decode('utf8','replace')[-1200:])
        os.replace(str(temp),str(output))
    lib=ctypes.CDLL(str(output))
    lib.tv_render.restype=ctypes.c_int
    lib.tv_render.argtypes=[ctypes.c_void_p]*3+[ctypes.c_int]*2+[ctypes.c_float]*3+[
        ctypes.c_void_p,ctypes.c_int,ctypes.c_int]
    lib.tv_memory_barrier.restype=None
    lib.tv_memory_barrier.argtypes=[]
    return lib

class Renderer:
    def __init__(self,path,compiler='gcc',max_faces=22000):
        self.source=Path(path);self.mesh=display_mesh(*load_mesh(path),max_faces=max_faces)
        self.p,self.t,self.rgb,self.info=self.mesh
        self.lib=native_library(compiler)
    def render(self,yaw=0.,pitch=.12,zoom=1.,width=400,height=208):
        out=np.empty((height,width),np.uint16)
        code=self.lib.tv_render(self.p.ctypes.data,self.t.ctypes.data,self.rgb.ctypes.data,
            len(self.p),len(self.t),float(yaw),float(pitch),float(zoom),out.ctypes.data,width,height)
        if code:raise RuntimeError('渲染失败 code=%d'%code)
        return out
