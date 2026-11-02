"""Validated GT911 snapshot + owned double-buffer submission. Python 3.6."""
import time
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np

VERSION=87
SOFTWARE_VERSION=88
BASE=0x43c10000
MAGIC=0x54563837
FRAME_WORDS=800*480
# Keep unresolved DMA ownership alive even when a new module is imported.
if '_HELD_BUFFERS' not in globals():_HELD_BUFFERS=[]

def mapping(overlay):
    check=getattr(overlay,'is_loaded',False)
    if not (check() if callable(check) else check):raise RuntimeError('Overlay未加载')
    info=getattr(overlay,'ip_dict',{}).get('touch_viewer_0')
    def integer(v):return int(v,0) if isinstance(v,str) else int(v)
    if isinstance(info,dict):
        if info.get('phys_addr') is not None and integer(info['phys_addr'])!=BASE:
            raise RuntimeError('触屏寄存器地址与硬件描述不一致')
        if info.get('phys_addr') is not None and integer(info.get('addr_range',0))>=4096:return BASE,4096
    # Old PYNQ can expose module_ref as Hierarchy. Prove the exact current
    # bit's MEMRANGE, then use MMIO; never call Hierarchy.read or guess a base.
    bit=getattr(overlay,'bitfile_name',None)
    hwh=Path(bit).with_suffix('.hwh') if bit else None
    if hwh is not None and hwh.is_file():
        root=ET.parse(str(hwh)).getroot()
        entries=[e for e in root.iter('MEMRANGE') if e.get('INSTANCE')=='touch_viewer_0'
            and integer(e.get('BASEVALUE','-1'))==BASE
            and integer(e.get('HIGHVALUE','-1'))+1>=BASE+4096]
        if len(entries)==1:return BASE,4096
    raise RuntimeError('当前bit没有经元数据确认的触屏模型显示硬件')

class TouchDisplay:
    def __init__(self,overlay,mmio=None,allocator=None,clock=time.monotonic,sleeper=time.sleep):
        self.overlay=overlay;self.clock=clock;self.sleep=sleeper
        self.buffers=[];self.front=None;self.allocator=None;self.closed=False
        self.ownership_unconfirmed=False
        self._fence_library=None
        self.base,self.span=mapping(overlay)
        if mmio is None:
            from pynq import MMIO
            mmio=MMIO(self.base,self.span)
        self.mmio=mmio
        if mmio.read(0)!=MAGIC or mmio.read(4)!=VERSION:
            raise RuntimeError('触屏硬件MAGIC/版本不匹配，不写显示地址')
        if allocator is None:
            try:
                from pynq import allocate
                allocator=lambda:allocate(shape=(FRAME_WORDS,),dtype=np.uint16)
            except ImportError:
                from pynq import Xlnk
                self.allocator=Xlnk()
                allocator=lambda:self.allocator.cma_array(shape=(FRAME_WORDS,),dtype=np.uint16,cacheable=0)
        self.allocate=allocator
        # Adopt an existing display transaction by explicitly returning to camera
        # before allocating/reusing anything. No reset or Overlay re-download.
        self._wait_idle(1.)
        if int(self.mmio.read(0x2c))!=0:self._flip(0)

    def _loaded(self):
        check=getattr(self.overlay,'is_loaded',False)
        if not (check() if callable(check) else check):raise RuntimeError('Overlay已更换')

    def _wait_idle(self,timeout=1.):
        start=self.clock()
        while self.mmio.read(0x24)!=self.mmio.read(0x28):
            self._loaded()
            if self.clock()-start>timeout:raise RuntimeError('等待显示帧边界超时，保留缓存不释放')
            self.sleep(.002)

    def _flip(self,address,check_fault=True):
        if self.closed:raise RuntimeError('显示会话已经关闭，不再写换帧寄存器')
        self._loaded();self._wait_idle()
        expected=(int(self.mmio.read(0x24))+1)&0xffffffff
        self.mmio.write(0x20,int(address));self.mmio.write(0x24,1)
        if int(self.mmio.read(0x24))!=expected:
            raise RuntimeError('显示换帧请求未被硬件接受，保留缓存不释放')
        self._wait_idle()
        if int(self.mmio.read(0x28))!=expected:
            raise RuntimeError('显示换帧确认序号不一致')
        if int(self.mmio.read(0x2c))!=int(address):raise RuntimeError('显示缓存确认地址不一致')
        if check_fault and self.mmio.read(0x30)&2:raise RuntimeError('显示AXI读取错误，请查看硬件诊断')

    def snapshot(self):
        self._loaded()
        if self.front is not None and self.mmio.read(0x30)&2:
            raise RuntimeError('显示AXI读取错误，停止模型提交')
        for _ in range(5):
            seq=self.mmio.read(0x10);st=self.mmio.read(8);xy=self.mmio.read(0xc)
            ranges=self.mmio.read(0x18)
            if seq==self.mmio.read(0x10):
                if st&2:
                    mx,my=ranges&65535,(ranges>>16)&65535
                    if not (16<=mx<=8192 and 16<=my<=8192) or ((st&1) and
                        ((xy&65535)>mx or (xy>>16)>my)):
                        raise RuntimeError('GT911坐标或配置范围无效，不采用该次触点')
                return dict(sequence=int(seq),down=bool(st&1),online=bool(st&2),
                    points=(st>>2)&15,x=xy&65535,y=(xy>>16)&65535,
                    max_x=ranges&65535,max_y=(ranges>>16)&65535,
                    product=int(self.mmio.read(0x14)),errors=int(self.mmio.read(0x1c)))
        return None

    def ensure_buffers(self):
        if self.buffers:return
        for _ in range(2):
            b=self.allocate();self.buffers.append(b)
            address=int(b.physical_address)
            if (b.dtype!=np.uint16 or b.size!=FRAME_WORDS or address%128 or address<0x100000 or
                address+FRAME_WORDS*2>0x20000000 or
                not (address+FRAME_WORDS*2<=0x1800000 or address>=0x18bb800)):
                raise RuntimeError('分配到的显示缓存范围或对齐不安全')
        a,c=[int(b.physical_address) for b in self.buffers]
        if abs(a-c)<FRAME_WORDS*2:raise RuntimeError('两个显示缓存物理范围重叠')

    def present(self,pixels):
        if self.closed:raise RuntimeError('显示会话已经关闭')
        self._loaded();self._wait_idle()
        pixels=np.asarray(pixels)
        if pixels.shape!=(480,800) or pixels.dtype!=np.uint16:
            raise ValueError('模型画面必须为480x800 RGB565')
        self.ensure_buffers()
        target=0 if self.front!=0 else 1;b=self.buffers[target]
        # Existing 64->16 FIFO emits its high halfword first. This is spatial
        # packing, never an RGB565 byte swap; camera entry correction is intact.
        b.reshape(-1,4)[:]=pixels.reshape(-1,4)[:,::-1]
        flush=getattr(b,'flush',None)
        if callable(flush):flush()
        elif bool(getattr(b,'cacheable',False)):
            raise RuntimeError('缓存可缓存却没有flush接口，拒绝提交旧画面')
        # ARM must order normal/CMA memory stores before the device MMIO flip,
        # including the old Xlnk non-cacheable path where flush can be a no-op.
        if self._fence_library is None:
            from tft_model import native_library
            self._fence_library=native_library()
        self._fence_library.tv_memory_barrier()
        self._flip(int(b.physical_address));self.front=target

    def camera(self):
        # A previous sticky read fault is not DMA ownership. Only the new
        # acknowledged request to camera proves the old CMA frame is drained.
        self._flip(0,check_fault=False);self.front=None

    def close(self):
        if self.closed:
            if self.ownership_unconfirmed:
                raise RuntimeError('旧显示读事务尚未确认结束，继续保留缓存和屏幕所有权')
            return
        try:
            self.camera()
        except Exception:
            # Never free a frame while PL can still read it.
            _HELD_BUFFERS.append((self.overlay,list(self.buffers),self.allocator))
            self.buffers=[];self.closed=True;self.ownership_unconfirmed=True
            raise
        for b in self.buffers:
            free=getattr(b,'freebuffer',None)
            if callable(free):free()
            else:
                close=getattr(b,'close',None)
                if callable(close):close()
        self.buffers=[];self.closed=True
