"""One UI-owner thread: calibration, result button, drag, zoom and return.

No browser, kernel driver, additional notebook cell, camera capture or re-scan.
"""
from pathlib import Path
import json
import math
import time
import numpy as np
from tft_touch import TouchDisplay

VERSION=87
SOFTWARE_VERSION=95
VIEW=(414,8,622,56)
BACK=(8,8,132,56)
MINUS=(548,8,654,56)
PLUS=(674,8,790,56)
TARGETS=np.asarray(((80,110),(720,110),(720,410),(80,410)),float)

def inside(point,rect):
    return rect[0]<=point[0]<rect[2] and rect[1]<=point[1]<rect[3]

def fit_calibration(raw):
    raw=np.asarray(raw,float)
    if raw.shape!=(4,2) or not np.isfinite(raw).all():raise ValueError('触控校准点无效')
    a=np.column_stack((raw,np.ones(4)))
    matrix,_,rank,_=np.linalg.lstsq(a,TARGETS,rcond=-1)
    errors=np.linalg.norm(a.dot(matrix)-TARGETS,axis=1)
    if rank!=3 or errors.max()>12 or abs(np.linalg.det(matrix[:2]))<1e-6:
        raise ValueError('四点校准不一致，请按十字中心重新点击')
    return matrix

def button(canvas,rect,text):
    x0,y0,x1,y1=rect
    canvas.rect(x0,y0,x1,y1,3);canvas.rect(x0+2,y0+2,x1-2,y1-2,2)
    canvas.text(x0+10,y0+9,text,24,1,x1-x0-16)

class Viewer:
    def __init__(self,overlay,directory=None,hardware=None,renderer_factory=None):
        self.hardware=hardware or TouchDisplay(overlay)
        self.directory=Path(directory) if directory else None
        self.cal_path=Path(__file__).resolve().parent/'1026_touch_calibration_v87.json'
        self.renderer_factory=renderer_factory
        self.path=None;self.renderer=None;self.mode='result';self.error=None
        self.matrix=None;self.cal_signature=None;self.cal_points=[];self.samples=[]
        self.last_sequence=None;self.last_down=False;self.down_at=None;self.last_pos=None
        self.max_touch_travel=0.
        self.yaw=0.;self.pitch=.12;self.zoom=1.;self.dirty=False
        self.last_render=0.;self.last_poll=0.;self.last_saved=0.;self.touch=None
        self.visible=False;self.view_ready=False;self.multi_cancel=False
        try:
            c=json.loads(self.cal_path.read_text(encoding='utf8'))
            matrix=np.asarray(c['matrix'],float)
            if (c['version']==VERSION and matrix.shape==(3,2) and np.isfinite(matrix).all()
                and abs(np.linalg.det(matrix[:2]))>=1e-6):
                self.matrix=matrix;self.cal_signature=c['signature']
        except (OSError,ValueError,KeyError,TypeError):pass

    def snapshot(self):
        return dict(version=VERSION,software_version=SOFTWARE_VERSION,mode=self.mode,model_path=str(self.path) if self.path else None,
            error=self.error,touch=self.touch,calibrated=self.matrix is not None,
            yaw=self.yaw,pitch=self.pitch,zoom=self.zoom,
            display_info=self.renderer.info if self.renderer else None,
            board_display_confirmed=False)

    def reset(self):
        # Caller serializes this with tick. Do this before any new scan/capture.
        if self.mode=='view' or self.hardware.front is not None:self.hardware.camera()
        self.path=None;self.renderer=None;self.mode='result';self.error=None
        self.visible=False;self.last_down=False;self.down_at=None;self.last_pos=None
        self.view_ready=False;self.cal_points=[];self.samples=[];self.multi_cancel=True
        self.last_sequence=None
        self.max_touch_travel=0.

    def close(self):self.hardware.close()

    def _save(self):
        if self.directory is None:return
        try:
            path=self.directory/'1026_touch_viewer_status_v87.json'
            temp=path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.snapshot(),ensure_ascii=False,indent=2),encoding='utf8')
            temp.replace(path)
        except OSError:pass

    def _header(self,canvas,writer):
        canvas.pixels.fill(0);canvas.rect(0,0,800,64,2)
        button(canvas,BACK,'返回');button(canvas,MINUS,'缩小');button(canvas,PLUS,'放大')
        canvas.text(148,8,'三维模型',24,3,200)
        canvas.text(148,36,'旋转模型',18,1,300)
        writer.flush(canvas.pixels)

    def _cal_draw(self,canvas,writer):
        canvas.pixels.fill(2)
        canvas.text(170,12,'请依次按 + 中心',28,1,620)
        canvas.text(240,48,'校准 %d/4'%(len(self.cal_points)+1),24,3,400)
        x,y=TARGETS[len(self.cal_points)].astype(int)
        canvas.rect(x-24,y-2,x+25,y+3,3);canvas.rect(x-2,y-24,x+3,y+25,3)
        if self.error:canvas.text(80,450,'校准失败，请重试',18,1,640)
        writer.flush(canvas.pixels)

    def _point(self,touch):
        p=np.asarray((touch['x'],touch['y'],1.)).dot(self.matrix)
        return tuple(float(v) for v in p)

    def _prepare(self,canvas,writer,stop):
        canvas.pixels.fill(2);canvas.text(180,200,'正在加载模型，请等候',28,1,600)
        writer.flush(canvas.pixels)
        if self.renderer is None:
            factory=self.renderer_factory
            if factory is None:
                from tft_model import Renderer
                factory=Renderer
            self.renderer=factory(self.path)
        if stop.is_set():return
        self.yaw=0.;self.pitch=.12;self.zoom=1.;self.mode='view';self.dirty=True
        self._header(canvas,writer)

    def _render(self,stop):
        frame=self.renderer.render(self.yaw,self.pitch,self.zoom)
        if stop.is_set():return
        full=np.full((480,800),0x1082,np.uint16)
        full[64:]=frame.repeat(2,axis=0).repeat(2,axis=1)
        self.hardware.present(full)
        self.dirty=False;self.last_render=time.monotonic()

    def tick(self,canvas,writer,state,stop):
        """Return True while this object owns the full screen drawing."""
        now=time.monotonic()
        path=state.get('model_path') if state.get('status')=='completed' else None
        if path!=self.path:
            self.reset();self.path=path
        if not self.path:return False
        if now-self.last_poll<.025:return self.mode in ('view','calibrate')
        self.last_poll=now
        touch=self.hardware.snapshot()
        if touch is None:
            # Five inconsistent seqlock reads are not an offline GT911.
            # Cancel the incomplete gesture, retain current screen ownership.
            self.last_down=False;self.down_at=None;self.last_pos=None
            self.samples=[];self.multi_cancel=True
            return self.mode in ('view','calibrate')
        self.touch=touch
        if not touch['online']:
            self.last_down=False;self.down_at=None;self.last_pos=None
            if self.mode=='view':
                self.hardware.camera();self.mode='result';self.visible=False
            if self.mode=='calibrate':self.mode='result';self.visible=False
            return False
        signature=[touch['product'],touch['max_x'],touch['max_y']]
        if self.matrix is not None and self.cal_signature!=signature:
            self.matrix=None
        if self.matrix is None and self.mode!='calibrate':
            self.mode='calibrate';self.cal_points=[];self.samples=[]
            self.last_down=False;self.multi_cancel=True;self._cal_draw(canvas,writer)
        if touch['sequence']!=self.last_sequence:
            self.last_sequence=touch['sequence']
            down=touch['down'] and touch['points']==1
            if touch['points']>1:
                self.down_at=None;self.last_pos=None;self.multi_cancel=True;self.samples=[]
            if self.multi_cancel:
                down=False
                if touch['points']==0:self.multi_cancel=False
            if self.mode=='calibrate':
                if down:self.samples.append((touch['x'],touch['y']))
                if self.last_down and not down and self.samples:
                    self.samples.append((touch['x'],touch['y']))
                    raw=np.asarray(self.samples,float);self.samples=[]
                    if np.ptp(raw,axis=0).max()<40:
                        self.cal_points.append(np.median(raw,axis=0).tolist())
                    if len(self.cal_points)==4:
                        try:
                            self.matrix=fit_calibration(self.cal_points);self.cal_signature=signature
                        except ValueError as exc:
                            self.error=str(exc);self.cal_points=[];self.matrix=None
                        else:
                            self.mode='result';self.visible=False;self.error=None
                            try:
                                temporary=self.cal_path.with_suffix('.tmp')
                                temporary.write_text(json.dumps(dict(version=VERSION,
                                    matrix=self.matrix.tolist(),signature=signature),indent=2),encoding='utf8')
                                temporary.replace(self.cal_path)
                            except OSError as exc:
                                self.error='校准已临时生效，保存失败；下次需重新校准：%s'%exc
                                print(self.error,flush=True)
                    if self.mode=='calibrate':self._cal_draw(canvas,writer)
            else:
                point=self._point(touch)
                if down and not self.last_down:
                    self.down_at=point;self.last_pos=point;self.max_touch_travel=0.
                elif down and self.last_pos is not None:
                    if self.down_at is not None:
                        self.max_touch_travel=max(self.max_touch_travel,
                            math.hypot(point[0]-self.down_at[0],point[1]-self.down_at[1]))
                    if self.mode=='view' and self.down_at is not None and self.down_at[1]>=64:
                        dx=point[0]-self.last_pos[0];dy=point[1]-self.last_pos[1]
                        self.yaw=(self.yaw-dx*.009)% (2*math.pi)
                        self.pitch=float(np.clip(self.pitch+dy*.008,-1.52,1.52));self.dirty=True
                    self.last_pos=point
                elif self.last_down and not down and self.down_at is not None:
                    # GT911 retains the last valid contact coordinate on up.
                    # A complete move can occur between our two polls.
                    first=self.down_at;last=point
                    self.max_touch_travel=max(self.max_touch_travel,
                        math.hypot(last[0]-first[0],last[1]-first[1]))
                    if self.mode=='view' and first[1]>=64 and self.last_pos is not None:
                        dx=last[0]-self.last_pos[0];dy=last[1]-self.last_pos[1]
                        self.yaw=(self.yaw-dx*.009)%(2*math.pi)
                        self.pitch=float(np.clip(self.pitch+dy*.008,-1.52,1.52))
                        self.dirty=self.dirty or dx!=0 or dy!=0
                    tap=self.max_touch_travel<24
                    if tap and self.mode=='result' and inside(first,VIEW) and inside(last,VIEW):
                        self._prepare(canvas,writer,stop)
                    elif tap and self.mode=='view':
                        if inside(first,BACK) and inside(last,BACK):
                            self.hardware.camera();self.mode='result';self.visible=False
                        elif inside(first,PLUS) and inside(last,PLUS):self.zoom=min(3.,self.zoom*1.25);self.dirty=True
                        elif inside(first,MINUS) and inside(last,MINUS):self.zoom=max(.3,self.zoom/1.25);self.dirty=True
                    self.down_at=None;self.last_pos=None
            self.last_down=down
        if self.mode=='view' and self.dirty and now-self.last_render>.06:
            self._render(stop)
        if now-self.last_saved>2:self.last_saved=now;self._save()
        return self.mode in ('view','calibrate')
