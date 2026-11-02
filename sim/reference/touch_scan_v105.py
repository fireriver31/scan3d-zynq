"""One notebook launch; the existing eight cells are driven by TFT taps.

The scan-screen thread remains the only normal UI writer. Sensor/PL/model
algorithms stay in the user's existing notebook and Python modules.
Python 3.6; touch_viewer ABI87 and scan_screen software103 or later.
"""
from pathlib import Path
import ast
import hashlib
import importlib
import json
import math
import queue
import re
import sys
import threading
import time
import traceback
import numpy as np

VERSION = 105
# The existing status strip owns y=0..63. Keep an eight-pixel gap below it.
PANEL = (640,72,800,480)
VIEWPORT = (0,64,632,480)
TIP = (648,78,792,181)
NEXT = (648,188,792,238)
PREVIEW = (648,246,792,296)
RETRY = (648,304,792,354)
BACK = (648,362,792,412)
EXIT = (648,420,792,470)
MODEL = (414,8,622,56)
PROMPTS = {
    1:'移走整个转台，保持相机和光照不变',
    2:'只放回空转台，不放物体',
    3:'保持空转台不动，执行转台标定',
    4:'确认标定线后放入物体，检查单张掩模',
    5:'开启转台，保持黑色标记可见',
    6:'保持转台匀速，准备采集24张照片',
    7:'照片已保存，准备生成整圈掩模',
    8:'确认整圈掩模后，开始三维重建',
}
REVIEW = {
    1:'核对背景清晰；放回空转台后点下一步',
    2:'核对空台照片；下一步进行标定',
    3:'核对标定线；放入物体后点下一步',
    4:'核对原图与掩模；开启转台后点下一步',
    5:'转速已获得；保持匀速，下一步采集',
    6:'24张照片已保存；下一步生成掩模',
    7:'核对整圈掩模；下一步重建',
    8:'模型和体积已保存，可点击查看模型',
}
OUTPUTS = {
    1:('camera_settings','exposure_bank','bg565'),
    2:('tt565','background_sigma','background_info'),
    3:('cal',),4:('preview','preview_clean','preview565'),
    5:('period_s','speed_info'),6:('color_scan',),7:('scan',),
    8:('report','notes','result_dir','memory_info'),
}


def inside(point, rect):
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def wrapped_lines(canvas, text, size=18, width=144, max_lines=5):
    """Use the same glyph advance as BitmapCanvas.text; never spill a panel."""
    lines, line, used = [], '', 0
    for char in str(text):
        if char=='\n':
            lines.append(line)
            line, used = '', 0
            continue
        advance = max(4,size//3) if char==' ' else canvas.glyph(char,size).shape[1]+max(1,size//12)
        if line and used+advance>width:
            lines.append(line)
            line, used = '', 0
        line += char
        used += advance
    if line:
        lines.append(line)
    if len(lines)>max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        def measure(value):
            return sum(max(4,size//3) if c==' ' else canvas.glyph(c,size).shape[1]+max(1,size//12)
                       for c in value)
        while last and measure(last+'...')>width:
            last = last[:-1]
        lines[-1] = last+'...'
    return lines


def load_cells(path):
    raw = Path(path).read_bytes()
    notebook = json.loads(raw.decode('utf-8-sig'))
    cells = [''.join(cell['source']) for cell in notebook['cells'] if cell.get('cell_type')=='code']
    if len(cells) != 9:
        raise ValueError('触屏入口需要现有九格彩色Notebook；不自动猜测其它流程')
    for index, source in enumerate(cells):
        tree = ast.parse(source)
        if index and not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                and node.func.attr=='step' and node.args and isinstance(node.args[0],ast.Constant)
                and node.args[0].value==index for node in ast.walk(tree)):
            # Python 3.6 AST uses Num rather than Constant for numeric literals.
            if not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
                    and node.func.attr=='step' and node.args and getattr(node.args[0],'n',None)==index
                    for node in ast.walk(tree)):
                raise ValueError('第%d格不是已知的scan_screen步骤，停止自动执行'%index)
        if any(isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='input'
               for node in ast.walk(tree)):
            raise ValueError('Notebook有未映射到触屏的直接input调用')
    initial = ast.parse(cells[0])
    guards = [node.lineno for node in ast.walk(initial) if isinstance(node,ast.Call)
              and isinstance(node.func,ast.Name) and node.func.id=='require_framebuffer_reservation']
    downloads = [node.lineno for node in ast.walk(initial) if isinstance(node,ast.Call)
                 and isinstance(node.func,ast.Name) and node.func.id=='Overlay']
    if not guards or not downloads or min(guards)>=min(downloads):
        raise ValueError('初始化格缺少加载Overlay之前的DDR保留检查')
    # v95's top-level detach was for refreshing an old viewer. attach() now
    # performs that refresh during startup; do not destroy the flow at step8.
    tree = ast.parse(cells[8])
    lines = cells[8].splitlines(True)
    for node in tree.body:
        if (isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                and isinstance(node.value.func,ast.Attribute) and node.value.func.attr=='detach'
                and isinstance(node.value.func.value,ast.Name) and node.value.func.value.id=='scan_screen'):
            lines[node.lineno-1] = '# Viewer was safely refreshed during touch-flow startup.\n'
    cells[8] = ''.join(lines)
    return cells, hashlib.sha256(raw).hexdigest()


class Controller:
    def __init__(self, session, screen, clock=time.monotonic):
        if session.viewer is None:
            raise RuntimeError('触屏显示接口未绑定，请先完成已有触屏验收')
        self.session, self.screen, self.clock = session, screen, clock
        self.viewer = session.viewer
        self.original_tick = self.viewer.tick
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=1)
        self.phase = 'ready'
        self.step = 1
        self.epoch = 0
        self.active = True
        self.images = []
        self.image_index = 0
        self.image_version = 0
        self.preview_active = False
        self.preview_wanted = False
        self.last_draw_key = None
        self.last_sequence = None
        self.down = None
        self.travel = 0.
        self.need_up = True
        self.dialog = None
        self.error = None
        self.cal_points = []
        self.cal_samples = []
        self.cal_last_down = False
        self.cal_started = False
        self.cal_signature = None

    def install(self):
        with self.session.viewer_lock:
            if getattr(self.session,'workflow',None) is not None:
                raise RuntimeError('旧触屏流程尚未结束')
            self.session.workflow = self
            self.viewer.tick = self.tick
        return self

    def detach(self):
        with self.session.viewer_lock:
            if self.preview_active:
                self.viewer.hardware.camera()
                self.preview_active = False
            self.viewer.tick = self.original_tick
            self.session.workflow = None
            self.session.last_render_state = None
            self.active = False

    def set_phase(self, phase, step=None, error=None):
        with self.lock:
            self.phase = phase
            if step is not None:
                self.step = step
            self.error = error
            self.epoch += 1
            self.down = None
            self.need_up = True
            self.last_draw_key = None
            if phase in ('ready','busy'):
                self.preview_wanted = False
                self.images = []
                self.image_version += 1
            elif phase in ('review','failed'):
                self.preview_wanted = bool(self.images)

    def display(self, value, *args, **kwargs):
        from PIL import Image
        if not isinstance(value,Image.Image):
            return
        image = value.convert('RGB').copy()
        with self.lock:
            self.images.append(image)
            self.images = self.images[-2:]
            self.image_index = len(self.images)-1
            self.image_version += 1

    def _camera(self):
        if self.preview_active:
            self.viewer.hardware.camera()
            self.preview_active = False

    def _preview(self):
        from PIL import Image
        image = self.images[self.image_index]
        image = image.copy()
        x0,y0,x1,y1 = VIEWPORT
        image.thumbnail((x1-x0,y1-y0),Image.BILINEAR)
        frame = np.full((480,800),0x1082,np.uint16)
        rgb = np.asarray(image,np.uint16)
        packed = ((rgb[:,:,0]>>3)<<11)|((rgb[:,:,1]>>2)<<5)|(rgb[:,:,2]>>3)
        y = y0+(y1-y0-image.height)//2
        x = x0+(x1-x0-image.width)//2
        frame[y:y+image.height,x:x+image.width] = packed
        self.viewer.hardware.present(frame)
        self.preview_active = True

    def _draw_controls(self, canvas, online):
        canvas.rect(*PANEL,colour=2)
        canvas.rect(PANEL[0],PANEL[1],PANEL[0]+1,PANEL[3],3)
        if not online:
            text = '触屏未连接，等待恢复'
        elif self.phase=='prompt':
            text = self.dialog['text']
        elif self.phase=='failed':
            text = '本步未完成，可重试或返回上一步'
        elif self.phase=='review' and self.step==8 and self.error:
            text = '模型暂未打开，可重试；原结果已保存'
        elif self.phase=='busy':
            text = '正在处理，请等候；步骤按钮已锁定'
        else:
            text = PROMPTS[self.step] if self.phase=='ready' else REVIEW[self.step]
        for index,line in enumerate(wrapped_lines(canvas,text)):
            canvas.text(TIP[0],TIP[1]+index*21,line,18,1,TIP[2]-TIP[0])
        from tft_viewer import button
        if self.phase=='prompt' and online:
            for rect,label,answer in self.dialog['choices']:
                button(canvas,rect,label)
        elif self.phase!='busy' and online:
            button(canvas,EXIT,'结束')
            if self.step>1:
                button(canvas,BACK,'上一步')
            if self.images:
                button(canvas,PREVIEW,'预览')
            button(canvas,RETRY,'重试')
            label = '查看模型' if self.phase=='review' and self.step==8 else '下一步' if self.phase=='review' else '执行此步'
            button(canvas,NEXT,label)

    def _queue(self, action, step=None, answer=None):
        if self.commands.full():
            return
        self._camera()  # Acknowledge live-camera display before CPU work starts.
        self.preview_wanted = False
        self.phase = 'busy'
        self.epoch += 1
        self.commands.put_nowait(dict(action=action,step=step,answer=answer))
        self.need_up = True
        self.down = None
        self.last_draw_key = None

    def _tap(self, first, last):
        if self.phase == 'busy':
            return
        if self.phase == 'prompt':
            for rect, label, answer in self.dialog['choices']:
                if inside(first,rect) and inside(last,rect):
                    self._queue('answer',answer=answer)
                    return
            return
        if inside(first,PREVIEW) and inside(last,PREVIEW) and self.images:
            self.image_index = (self.image_index+1)%len(self.images) if self.preview_wanted else self.image_index
            self.preview_wanted = True
            self.image_version += 1
            return
        if inside(first,EXIT) and inside(last,EXIT):
            self._queue('exit')
        elif inside(first,BACK) and inside(last,BACK) and self.step>1:
            self._queue('prepare',max(1,self.step-1))
        elif inside(first,RETRY) and inside(last,RETRY):
            self._queue('prepare',self.step)
        elif ((inside(first,NEXT) and inside(last,NEXT)) or
              (self.phase=='review' and self.step==8 and inside(first,MODEL) and inside(last,MODEL))):
            if self.phase == 'review' and self.step == 8:
                path = self.session.snapshot().get('model_path')
                if not path or not Path(path).is_file():
                    self.error = '本次模型文件不可用'
                    return
                self._camera()
                self.viewer.reset()
                self.viewer.path = path
                try:
                    self.viewer._prepare(self.session.canvas,self.session.writer,self.session.stop)
                except Exception as error:
                    self.viewer.mode = 'result'
                    self.error = str(error)
                    self.last_draw_key = None
                return
            target = self.step+1 if self.phase=='review' else self.step
            self._queue('execute',target)

    def _calibrate(self, touch, canvas, writer):
        from tft_viewer import TARGETS, fit_calibration, VERSION as calibration_version
        signature = [touch['product'],touch['max_x'],touch['max_y']]
        if not self.cal_started or self.cal_signature != signature:
            self.cal_started = True
            self.cal_signature = signature
            self.cal_points = []
            self.cal_samples = []
            self.cal_last_down = False
        down = bool(touch['down'] and touch['points']==1)
        if touch['points']>1:
            self.cal_samples = []
            self.need_up = True
        if self.need_up:
            down = False
            if touch['points']==0:
                self.need_up = False
        if down:
            self.cal_samples.append((touch['x'],touch['y']))
        if self.cal_last_down and not down and self.cal_samples:
            self.cal_samples.append((touch['x'],touch['y']))
            samples = np.asarray(self.cal_samples,float)
            self.cal_samples = []
            if np.ptp(samples,axis=0).max()<40:
                self.cal_points.append(np.median(samples,axis=0).tolist())
            if len(self.cal_points)==4:
                try:
                    self.viewer.matrix = fit_calibration(self.cal_points)
                    self.viewer.cal_signature = [touch['product'],touch['max_x'],touch['max_y']]
                except ValueError:
                    self.cal_points = []
                else:
                    self.cal_started = False
                    self.need_up = True
                    try:
                        target = self.viewer.cal_path
                        temporary = target.with_suffix('.tmp')
                        temporary.write_text(json.dumps(dict(version=calibration_version,
                            matrix=self.viewer.matrix.tolist(),signature=self.viewer.cal_signature)),encoding='utf8')
                        temporary.replace(target)
                    except OSError:
                        pass  # Calibration still works for this session.
                    self.last_draw_key = None
                    return
        self.cal_last_down = down
        canvas.pixels.fill(2)
        canvas.text(100,20,'请依次点击十字中心，完成触屏校准',24,1,700)
        canvas.text(260,58,'校准 %d/4'%(len(self.cal_points)+1),24,3,300)
        x,y = TARGETS[len(self.cal_points)].astype(int)
        canvas.rect(x-24,y-2,x+25,y+3,3)
        canvas.rect(x-2,y-24,x+3,y+25,3)
        writer.flush(canvas.pixels)

    def tick(self, canvas, writer, state, stop):
        if self.viewer.mode=='view':
            owned = self.original_tick(canvas,writer,state,stop)
            if owned:
                return True
            self.last_draw_key = None
            self.need_up = True
        touch = self.viewer.hardware.snapshot()
        with self.lock:
            self.viewer.touch = touch
            online = bool(touch and touch.get('online'))
            if online and touch['points']==0:
                self.need_up = False
            if not online:
                self.down = None
                self.need_up = True
                self.cal_samples = []
                self.cal_last_down = False
            elif touch['sequence'] != self.last_sequence:
                self.last_sequence = touch['sequence']
                signature = [touch['product'],touch['max_x'],touch['max_y']]
                if self.viewer.matrix is not None and self.viewer.cal_signature != signature:
                    self.viewer.matrix = None
                if self.viewer.matrix is None:
                    self._calibrate(touch,canvas,writer)
                    return True
                down = bool(touch['down'] and touch['points']==1)
                if touch['points']>1:
                    self.down = None
                    self.need_up = True
                if self.need_up:
                    if touch['points']==0:
                        self.need_up = False
                    down = False
                point = self.viewer._point(touch)
                if down and self.down is None:
                    self.down = (point,self.epoch)
                    self.travel = 0.
                elif down and self.down is not None:
                    self.travel = max(self.travel,math.hypot(point[0]-self.down[0][0],point[1]-self.down[0][1]))
                elif not down and self.down is not None:
                    first, epoch = self.down
                    self.travel = max(self.travel,math.hypot(point[0]-first[0],point[1]-first[1]))
                    self.down = None
                    if touch['points']==0 and epoch==self.epoch and self.travel<24:
                        self._tap(first,point)
            if self.viewer.mode=='view':
                return True
            if online and self.viewer.matrix is None:
                return True
            state = dict(state,touch_online=online)
            if self.phase=='ready':
                state.update(step=self.step,status='waiting',stage='等待触屏操作',
                             detail='请按右侧提示准备后执行',frame='',volumes=[],model_path=None)
            elif self.phase=='failed' and self.error:
                state.update(status='failed',frame='',detail='本步未完成：'+str(self.error))
            key = (self.phase,self.step,self.epoch,self.image_version,online,int(self.clock()),
                   state.get('stage'),state.get('frame'),str(state.get('volumes')),self.preview_wanted)
            if key != self.last_draw_key:
                if self.preview_wanted and self.images:
                    if getattr(self,'_shown_image_version',None) != self.image_version:
                        self._preview()
                        self._shown_image_version = self.image_version
                else:
                    self._camera()
                self.screen.render(canvas,state,self.clock())
                self._draw_controls(canvas,online)
                writer.flush(canvas.pixels)
                self.last_draw_key = key
        return True

    def wait(self):
        while True:
            if self.session.stop.is_set() or self.session.viewer is None:
                raise RuntimeError('实体屏线程已停止，流程未自动跳过；请查看保存的错误')
            try:
                return self.commands.get(timeout=.1)
            except queue.Empty:
                pass

    def ask(self, prompt='', reader=None):
        text = str(prompt)
        if '请输入r、m或q' in text or 'm使用原手动计时' in text:
            choices = [(EXIT,'取消测速','q'),(PREVIEW,'手动计时','m'),(NEXT,'重试自动','r')]
            title = '自动测速未通过，请选择处理方式'
        elif '再经过同一位置' in text:
            choices = [(EXIT,'取消测速','q'),(RETRY,'重新计时','r'),(NEXT,'结束计时','')]
            count = re.search(r'同一位置\s*(\d+)\s*次',text)
            title = '标记再经过同一位置%s次后，点击结束计时'%(count.group(1) if count else '规定')
        elif '指定位置' in text and '开始' in text:
            choices = [(EXIT,'取消测速','q'),(NEXT,'开始计时','')]
            title = '标记到指定位置时，点击开始计时'
        else:
            raise RuntimeError('有尚未映射到触屏的输入提示：'+text)
        with self.lock:
            self.dialog = dict(text=title,choices=choices)
        self.set_phase('prompt')
        command = self.wait()
        if command['action'] != 'answer':
            raise RuntimeError('触屏输入状态不一致')
        self.set_phase('busy')
        return command['answer']


def invalidate(namespace, step):
    for index, names in OUTPUTS.items():
        if index>=step:
            for name in names:
                namespace.pop(name,None)


def validate_result(namespace, step):
    if step==5 and namespace.get('period_s') is None:
        raise RuntimeError('测速已取消，本步尚未完成')
    expected = {1:'bg565',2:'tt565',3:'cal',4:'preview',6:'color_scan',7:'scan',8:'report'}
    key = expected.get(step)
    if key and namespace.get(key) is None:
        raise RuntimeError('本步骤没有产生本次有效结果：'+key)
    if step==8 and 'volume_cm3' not in namespace['report']:
        raise RuntimeError('本次没有有效体积结果')


def run(scope=None, notebook=None):
    """Blocking main-thread workflow; its [*] means screen-controlled session."""
    scope = scope if scope is not None else {}
    root = Path(__file__).resolve().parent
    notebook = Path(notebook) if notebook else root/'1014_彩色原图辅助整圈扫描_v1.ipynb'
    if notebook.resolve().parent != root:
        raise ValueError('触屏入口与原Notebook必须放在同一板上目录')
    cells, digest = load_cells(notebook)
    current = sys.modules.get('scan_screen')
    if current is not None and getattr(getattr(current,'_SESSION',None),'workflow',None) is not None:
        raise RuntimeError('现有触屏流程尚未结束，不重新加载硬件')
    namespace = {'__name__':'__main__'}
    previous_scope = scope.get('touch_flow_scope')
    previous_device = previous_scope.get('pl_device') if isinstance(previous_scope,dict) else None
    if previous_device is None:
        previous_device = scope.get('pl_device')
    if previous_device is not None:
        namespace['pl_device'] = previous_device
    scope['touch_flow_scope'] = namespace
    exec(compile(cells[0],str(notebook)+'#cell0','exec'),namespace)
    screen = namespace['scan_screen']
    if getattr(screen,'VERSION',0)<103:
        raise RuntimeError('请配套上传v103的scan_screen.py与scan_screen_font.py')
    controller = Controller(screen._SESSION,screen).install()
    original_ask = screen.ask
    original_display = namespace['display']
    screen.ask = controller.ask
    namespace['display'] = controller.display
    directory = Path(namespace['RUN_DIR'])
    history = []
    def record(event, step, error=None):
        history.append(dict(event=event,step=step,error=error,at_monotonic_s=time.monotonic()))
        path = directory/'1030_touch_flow_v103.json'
        temporary = path.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(dict(version=VERSION,notebook_path=str(notebook),
                original_notebook_sha256=digest,events=history),ensure_ascii=False,indent=2),encoding='utf8')
            temporary.replace(path)
        except OSError as error:
            print('触屏流程记录暂未保存：%s'%error,flush=True)
    print('触屏流程已启动。本格[*]表示正在等待屏幕操作，请勿重复运行。',flush=True)
    record('ready',1)
    try:
        while True:
            command = controller.wait()
            if command['action']=='exit':
                record('ended',controller.step)
                break
            step = int(command['step'])
            if command['action']=='prepare':
                invalidate(namespace,step)
                controller.set_phase('ready',step)
                record('prepare',step)
                continue
            if command['action']!='execute' or not 1<=step<=8:
                raise RuntimeError('流程步骤无效')
            invalidate(namespace,step)
            controller.set_phase('busy',step)
            record('started',step)
            try:
                exec(compile(cells[step],str(notebook)+'#cell%d'%step,'exec'),namespace)
                validate_result(namespace,step)
            except Exception as error:
                # Keep the real failed-save trace for v102's guarded recovery.
                sys.last_traceback = error.__traceback__
                log = directory/('1030_touch_step%d_failure_%d_v103.txt'%(step,len(history)))
                try:
                    log.write_text(traceback.format_exc(),encoding='utf8')
                except OSError as save_error:
                    print('本次错误记录暂未保存：%s'%save_error,flush=True)
                controller.set_phase('failed',step,str(error))
                record('failed',step,str(error))
                print('第%d步未完成：%s；可在屏幕重试或返回。'%(step,error),flush=True)
            else:
                controller.set_phase('review',step)
                record('completed',step)
    except KeyboardInterrupt:
        record('interrupted',controller.step)
        raise
    except Exception as error:
        record('flow_failed',controller.step,str(error))
        raise
    finally:
        screen.ask = original_ask
        namespace['display'] = original_display
        controller.detach()
        for name in ('DATA_DIR','COLOR_BIT','RUN_DIR','pl_device','ol_color','rtl_mask_demo','grab_raw',
            'camera_control','scan_pipeline','pl_mask','adaptive_scan','measure_period','save_color_frame'):
            if name in namespace:
                scope[name] = namespace[name]
        for names in OUTPUTS.values():
            for name in names:
                if name in namespace:
                    scope[name] = namespace[name]
                else:
                    scope.pop(name,None)
    return namespace
