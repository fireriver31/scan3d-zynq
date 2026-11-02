"""Read-only DS99 display observations. Python 3.6; no sensor or DMA writes."""
VERSION=99
MAGIC=0x44533939

def snapshot(mmio):
    if int(mmio.read(0x78))!=MAGIC:
        raise RuntimeError('当前bit没有v99显示观察器；请上传配套bit和hwh')
    for _ in range(8):
        frame=int(mmio.read(0x7c))
        raw=[int(mmio.read(a)) for a in (0x80,0x84,0x88,0x8c,0x90)]
        if frame==int(mmio.read(0x7c)):
            return dict(version=VERSION,frame_sequence=frame,
                last_frame_underflows=raw[0],total_underflows=raw[1],
                samples_y100_rgb565=dict(x32=raw[2]>>16,x96=raw[2]&65535,
                    x400=raw[3]>>16,x768=raw[3]&65535),
                last_underflow_request_xy=dict(x=raw[4]&4095,y=(raw[4]>>16)&4095),
                display_fifo_pixels=4096,
                note='FPGA观察值；颜色采样在UI叠加前，不代表相机白平衡或实体屏观测')
    raise RuntimeError('显示观察记录跨帧变化，未采用混合记录')

def observe(overlay,directory=None,mmio=None,clocks=None,sleeper=None):
    import time as _time
    import json as _json
    from pathlib import Path as _Path
    if mmio is None:
        from tft_touch import mapping as _mapping
        from pynq import MMIO as _MMIO
        base,span=_mapping(overlay)
        mmio=_MMIO(base,span)
    if clocks is None:
        from pynq import Clocks as _Clocks
        clocks=_Clocks
    sleeper=sleeper or _time.sleep
    records=[]
    for i in range(4):
        if i:sleeper(.05)
        records.append(snapshot(mmio))
    record=records[-1]
    record.update(fclk0_register_mhz=float(clocks.fclk0_mhz),
        bitfile=str(getattr(overlay,'bitfile_name','')),observations=records[:-1],
        screen_image_confirmed=False)
    if directory is not None:
        path=_Path(directory)/'1028_display_status_v99.json'
        path.write_text(_json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    print('显示缓冲：4096像素；最近一帧空读：%d' % record['last_frame_underflows'],flush=True)
    return record

def colorbars(session=None,seconds=5,clocks=None,sleeper=None):
    """Optional existing-kernel diagnostic. Restore camera and OSD even on failure."""
    import numpy as _np
    import time as _time
    import json as _json
    from pathlib import Path as _Path
    if session is None:
        import scan_screen as _screen
        session=_screen._SESSION
    if session is None or session.viewer is None:
        raise RuntimeError('实体屏显示接口未绑定')
    pause=sleeper or _time.sleep
    with session.viewer_lock:
        with session.lock:
            hardware=session.viewer.hardware
            if hardware.front is not None:
                raise RuntimeError('请先返回实时画面，再执行彩条检查')
            previous=session.canvas.pixels.copy()
            picture=_np.zeros((480,800),dtype=_np.uint16)
            picture[:,:267]=0xf800;picture[:,267:534]=0x07e0;picture[:,534:]=0x001f
            picture[:8,:]=picture[-8:,:]=0xffff
            picture[:,:8]=picture[:,-8:]=0xffff
            try:
                session.writer.flush(_np.zeros((480,800),dtype=_np.uint8))
                hardware.present(picture)
                pause(max(.1,float(seconds)))
                record=observe(session.overlay,None,hardware.mmio,clocks,pause)
                expected=dict(x32=0xf800,x96=0xf800,x400=0x07e0,x768=0x001f)
                record.update(expected_samples_rgb565=expected,
                    fpga_sample_colors_match=record['samples_y100_rgb565']==expected,
                    note='FPGA叠加前采样通过不等于实体屏通过，需同时观察白边与彩条')
                if session.directory is not None:
                    path=_Path(session.directory)/'1028_display_colorbars_v99.json'
                    path.write_text(_json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
                    print('彩条诊断文件：',path,flush=True)
                print('FPGA四处颜色采样：',record['fpga_sample_colors_match'],flush=True)
                return record
            finally:
                try:hardware.camera()
                finally:session.writer.flush(previous)
